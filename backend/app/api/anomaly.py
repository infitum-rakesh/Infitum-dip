"""Anomaly Detection router — /api/anomaly/...

AD1: AnomalyJob CRUD. AD3 adds run/execution/column-result/record routes
alongside the anomaly/ engine package. AD4 adds baseline-approval/feedback
routes. AD5 adds background report generation/download routes.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.anomaly import FeedbackStatus
from app.models.user import User, UserRole
from app.schemas.anomaly import (
    AnomalyBaselineOut,
    AnomalyColumnResultOut,
    AnomalyConfigurationOut,
    AnomalyConfigurationUpdateRequest,
    AnomalyExecutionOut,
    AnomalyFeedbackCreateRequest,
    AnomalyFeedbackOut,
    AnomalyJobCreateRequest,
    AnomalyJobListOut,
    AnomalyJobOut,
    AnomalyJobUpdateRequest,
    AnomalyProfilePreviewRequest,
    AnomalyRecordListOut,
    AnomalyRecordOut,
    AnomalyReportCreateRequest,
    AnomalyReportOut,
)
from app.anomaly.engine import AnomalyEngineError
from app.services import anomaly_service as svc

router = APIRouter(prefix="/anomaly", tags=["anomaly"])

_view = require_action("VIEW", module="ANOMALY_DETECTION")
_create = require_action("CREATE", module="ANOMALY_DETECTION")
_edit = require_action("EDIT", module="ANOMALY_DETECTION")
_delete = require_action("DELETE", module="ANOMALY_DETECTION")
_execute = require_action("EXECUTE", module="ANOMALY_DETECTION")


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _to_out(db: AsyncSession, row) -> dict:
    names = await svc.user_name_map(db, {i for i in (row.created_by, row.updated_by) if i})
    return {
        **{c.name: getattr(row, c.name) for c in row.__table__.columns},
        "created_by_name": names.get(row.created_by),
        "updated_by_name": names.get(row.updated_by),
    }


async def _get_job_or_404(db: AsyncSession, job_id: int, user: User):
    row = await svc.get_job(db, job_id, _effective_org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly job not found")
    return row


# ── Job CRUD ─────────────────────────────────────────────────────

@router.get("/jobs", response_model=AnomalyJobListOut)
async def list_jobs(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    rows, total = await svc.list_jobs(db, _effective_org_id(user))
    items = [await _to_out(db, r) for r in rows]
    return AnomalyJobListOut(items=items, total=total)


@router.post("/jobs", response_model=AnomalyJobOut, status_code=status.HTTP_201_CREATED)
async def create_job(body: AnomalyJobCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    try:
        row = await svc.create_job(
            db, organization_id=user.organization_id, name=body.name, description=body.description,
            connection_id=body.connection_id, schema_name=body.schema_name, object_name=body.object_name,
            uploaded_file_id=body.uploaded_file_id, columns=body.columns, detection_config=body.detection_config,
            baseline_config=body.baseline_config, threshold_config=body.threshold_config,
            on_critical=body.on_critical, schedule_frequency=body.schedule_frequency,
            schedule_cron=body.schedule_cron, enabled=body.enabled, retention_days=body.retention_days,
            created_by=user.id,
        )
    except svc.AnomalyServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, row)


@router.get("/jobs/{job_id}", response_model=AnomalyJobOut)
async def get_job(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_job_or_404(db, job_id, user)
    return await _to_out(db, row)


@router.put("/jobs/{job_id}", response_model=AnomalyJobOut)
async def update_job(job_id: int, body: AnomalyJobUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_job_or_404(db, job_id, user)
    try:
        row = await svc.update_job(db, row, patch=body.model_dump(exclude_unset=True), updated_by=user.id)
    except svc.AnomalyServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, row)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_job_or_404(db, job_id, user)
    await svc.delete_job(db, row, deleted_by=user.id)


@router.post("/preview-profile")
async def preview_profile(body: AnomalyProfilePreviewRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    """On-demand column profiling for the job editor — see
    anomaly_service.preview_profile's docstring. No response_model: the
    profiler's output shape genuinely varies by column data type
    (numeric/string/date/boolean each carry different keys), so this
    returns the raw dict rather than forcing every shape through one
    Pydantic model."""
    try:
        return await svc.preview_profile(
            db, organization_id=_effective_org_id(user), connection_id=body.connection_id,
            schema_name=body.schema_name, object_name=body.object_name,
            uploaded_file_id=body.uploaded_file_id, columns=body.columns,
        )
    except (svc.AnomalyServiceError, AnomalyEngineError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# ── Run ──────────────────────────────────────────────────────────

@router.post("/jobs/{job_id}/run", response_model=AnomalyExecutionOut, status_code=status.HTTP_201_CREATED)
async def run_job(job_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    job = await _get_job_or_404(db, job_id, user)
    return await svc.run_job(db, organization_id=_effective_org_id(user), job=job, created_by=user.id)


@router.get("/jobs/{job_id}/executions", response_model=list[AnomalyExecutionOut])
async def list_executions(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    await _get_job_or_404(db, job_id, user)  # 404s / org-scopes access before listing
    rows, _ = await svc.list_executions(db, job_id, _effective_org_id(user))
    return rows


async def _get_execution_or_404(db: AsyncSession, execution_id: int, user: User):
    row = await svc.get_execution(db, execution_id, _effective_org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly execution not found")
    return row


@router.get("/executions/{execution_id}", response_model=AnomalyExecutionOut)
async def get_execution(execution_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    return await _get_execution_or_404(db, execution_id, user)


@router.get("/executions/{execution_id}/columns", response_model=list[AnomalyColumnResultOut])
async def list_column_results(execution_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    await _get_execution_or_404(db, execution_id, user)
    return await svc.list_column_results(db, execution_id)


async def _get_column_result_or_404(db: AsyncSession, column_result_id: int, user: User):
    row = await svc.get_column_result(db, column_result_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly column result not found")
    await _get_execution_or_404(db, row.execution_id, user)  # org-scopes access via the parent execution
    return row


@router.get("/column-results/{column_result_id}/records", response_model=AnomalyRecordListOut)
async def list_records(
    column_result_id: int, severity: str | None = Query(None), anomaly_type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    await _get_column_result_or_404(db, column_result_id, user)
    rows, total = await svc.list_records(db, column_result_id, severity=severity, anomaly_type=anomaly_type, limit=limit, offset=offset)
    return AnomalyRecordListOut(items=[AnomalyRecordOut.model_validate(r, from_attributes=True) for r in rows], total=total)


# ── Baseline approval ────────────────────────────────────────────
# Uses _edit (not a separate ACTION.APPROVE tier) — approving a baseline
# is a configuration change to what future runs compare against, the same
# permission tier as editing the job's own thresholds.

@router.get("/jobs/{job_id}/baselines", response_model=list[AnomalyBaselineOut])
async def list_baselines(
    job_id: int, column_name: str | None = Query(None),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    await _get_job_or_404(db, job_id, user)
    rows = await svc.list_baselines(db, job_id, column_name)
    names = await svc.user_name_map(db, {r.approved_by for r in rows if r.approved_by})
    return [
        AnomalyBaselineOut(
            **{c.name: getattr(r, c.name) for c in r.__table__.columns if c.name != "source"},
            source=r.source.value, approved_by_name=names.get(r.approved_by),
        )
        for r in rows
    ]


@router.post("/baselines/{baseline_id}/approve", response_model=AnomalyBaselineOut)
async def approve_baseline(baseline_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    baseline = await svc.get_baseline(db, baseline_id)
    if baseline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly baseline not found")
    await _get_job_or_404(db, baseline.job_id, user)  # org-scopes access via the parent job
    baseline = await svc.approve_baseline(db, baseline, approved_by=user.id, organization_id=_effective_org_id(user))
    names = await svc.user_name_map(db, {baseline.approved_by} if baseline.approved_by else set())
    return AnomalyBaselineOut(
        **{c.name: getattr(baseline, c.name) for c in baseline.__table__.columns if c.name != "source"},
        source=baseline.source.value, approved_by_name=names.get(baseline.approved_by),
    )


# ── Feedback ─────────────────────────────────────────────────────
# create_feedback uses _view, not _edit/_create — marking a record's
# feedback (True Anomaly/False Positive/Expected/Ignored) is a routine
# analyst action on the Results screen, not a job-configuration change,
# and every ROLE_ACTIONS tier that can VIEW results can also react to
# them. See services/anomaly_service.py's create_feedback docstring for
# why this never auto-suppresses future anomalies.

async def _get_record_or_404(db: AsyncSession, record_id: int, user: User):
    record = await svc.get_record(db, record_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly record not found")
    await _get_execution_or_404(db, record.execution_id, user)  # org-scopes access via the parent execution
    return record


@router.post("/records/{record_id}/feedback", response_model=AnomalyFeedbackOut, status_code=status.HTTP_201_CREATED)
async def create_feedback(
    record_id: int, body: AnomalyFeedbackCreateRequest,
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    record = await _get_record_or_404(db, record_id, user)
    try:
        feedback_status = FeedbackStatus(body.status)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid feedback status: {body.status}")
    row = await svc.create_feedback(
        db, record=record, status=feedback_status, comment=body.comment,
        created_by=user.id, organization_id=_effective_org_id(user),
    )
    names = await svc.user_name_map(db, {row.created_by} if row.created_by else set())
    return AnomalyFeedbackOut(
        **{c.name: getattr(row, c.name) for c in row.__table__.columns if c.name != "status"},
        status=row.status.value, created_by_name=names.get(row.created_by),
    )


@router.get("/records/{record_id}/feedback", response_model=list[AnomalyFeedbackOut])
async def list_feedback(record_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    await _get_record_or_404(db, record_id, user)
    rows = await svc.list_feedback(db, record_id)
    names = await svc.user_name_map(db, {r.created_by for r in rows if r.created_by})
    return [
        AnomalyFeedbackOut(
            **{c.name: getattr(r, c.name) for c in r.__table__.columns if c.name != "status"},
            status=r.status.value, created_by_name=names.get(r.created_by),
        )
        for r in rows
    ]


# ── Reports ──────────────────────────────────────────────────────
# Always a background job — see anomaly/reporting.py's module docstring
# ("Do not generate files synchronously"). create_report only inserts the
# PENDING row and fires execution/anomaly_export_executor.py's real
# asyncio background task; the frontend polls GET /reports/{id}.

def _report_to_out(report, names: dict) -> AnomalyReportOut:
    return AnomalyReportOut(
        **{c.name: getattr(report, c.name) for c in report.__table__.columns if c.name not in ("format", "status")},
        format=report.format.value, status=report.status.value, created_by_name=names.get(report.created_by),
    )


@router.post("/executions/{execution_id}/reports", response_model=AnomalyReportOut, status_code=status.HTTP_201_CREATED)
async def create_report(
    execution_id: int, body: AnomalyReportCreateRequest,
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    execution = await _get_execution_or_404(db, execution_id, user)
    try:
        report = await svc.create_report(db, execution=execution, report_format=body.format, created_by=user.id)
    except svc.AnomalyReportError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    names = await svc.user_name_map(db, {user.id})
    return _report_to_out(report, names)


@router.get("/executions/{execution_id}/reports", response_model=list[AnomalyReportOut])
async def list_reports(execution_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    await _get_execution_or_404(db, execution_id, user)
    rows = await svc.list_reports(db, execution_id)
    names = await svc.user_name_map(db, {r.created_by for r in rows if r.created_by})
    return [_report_to_out(r, names) for r in rows]


async def _get_report_or_404(db: AsyncSession, report_id: int, user: User):
    report = await svc.get_report(db, report_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly report not found")
    await _get_execution_or_404(db, report.execution_id, user)  # org-scopes access via the parent execution
    return report


@router.get("/reports/{report_id}", response_model=AnomalyReportOut)
async def get_report(report_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    report = await _get_report_or_404(db, report_id, user)
    names = await svc.user_name_map(db, {report.created_by} if report.created_by else set())
    return _report_to_out(report, names)


@router.get("/reports/{report_id}/download")
async def download_report(report_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    report = await _get_report_or_404(db, report_id, user)
    resolved = svc.resolve_download_path(report)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This report isn't downloadable — it hasn't succeeded, or the file is no longer on disk.")
    return FileResponse(resolved, filename=resolved.name)


# ── Org-level configuration (AD9) ────────────────────────────────
# Gated on ANOMALY_DETECTION permissions (not Settings) — see
# anomaly_service.get_configuration/update_configuration's docstring for
# why this reuses OrganizationSettings' JSON blob under its own route
# pair instead of going through /api/settings.

@router.get("/configuration", response_model=AnomalyConfigurationOut)
async def get_configuration(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Anomaly configuration is per-organization — log in as an organization user.")
    return await svc.get_configuration(db, org_id)


@router.put("/configuration", response_model=AnomalyConfigurationOut)
async def update_configuration(body: AnomalyConfigurationUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Anomaly configuration is per-organization — log in as an organization user.")
    return await svc.update_configuration(db, org_id, body.model_dump(exclude_unset=True), actor_id=user.id)
