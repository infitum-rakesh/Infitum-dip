"""Reconciliation router — /api/reconciliation/... (Phase 7).

Real source-vs-target comparison — see reconciliation/engine.py for how
each comparison type (COUNT/CHECKSUM/KEY_MATCH/FIELD_LEVEL) actually
runs. Nothing here is mocked.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.schemas.reconciliation import (
    ExceptionListOut,
    ExceptionOut,
    JobCreateRequest,
    JobListOut,
    JobOut,
    JobUpdateRequest,
    RunOut,
)
from app.services import reconciliation_service as svc

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])

_view = require_action("VIEW", module="RECONCILIATION")
_create = require_action("CREATE", module="RECONCILIATION")
_edit = require_action("EDIT", module="RECONCILIATION")
_delete = require_action("DELETE", module="RECONCILIATION")
_execute = require_action("EXECUTE", module="RECONCILIATION")


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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reconciliation job not found")
    return row


# ── Job CRUD ─────────────────────────────────────────────────────

@router.get("/jobs", response_model=JobListOut)
async def list_jobs(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    rows, total = await svc.list_jobs(db, _effective_org_id(user))
    items = [await _to_out(db, r) for r in rows]
    return JobListOut(items=items, total=total)


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(body: JobCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    try:
        row = await svc.create_job(
            db, organization_id=user.organization_id, name=body.name,
            source_connection_id=body.source_connection_id, source_schema=body.source_schema, source_object=body.source_object,
            source_uploaded_file_id=body.source_uploaded_file_id,
            target_connection_id=body.target_connection_id, target_schema=body.target_schema, target_object=body.target_object,
            target_uploaded_file_id=body.target_uploaded_file_id,
            key_columns=body.key_columns, compare_columns=body.compare_columns, comparison_types=body.comparison_types,
            column_mapping=body.column_mapping, created_by=user.id,
        )
    except svc.ReconciliationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, row)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_job_or_404(db, job_id, user)
    return await _to_out(db, row)


@router.put("/jobs/{job_id}", response_model=JobOut)
async def update_job(job_id: int, body: JobUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_job_or_404(db, job_id, user)
    try:
        row = await svc.update_job(
            db, row, name=body.name, key_columns=body.key_columns, compare_columns=body.compare_columns,
            comparison_types=body.comparison_types, column_mapping=body.column_mapping, updated_by=user.id,
        )
    except svc.ReconciliationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, row)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_job_or_404(db, job_id, user)
    await svc.delete_job(db, row)


# ── Run ──────────────────────────────────────────────────────────

@router.post("/jobs/{job_id}/run", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def run_job(job_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    job = await _get_job_or_404(db, job_id, user)
    try:
        run = await svc.run_job(db, organization_id=user.organization_id, job=job, created_by=user.id)
    except svc.ReconciliationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return run


@router.get("/jobs/{job_id}/runs", response_model=list[RunOut])
async def list_runs(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    runs, _ = await svc.list_runs(db, job_id, _effective_org_id(user))
    return runs


async def _get_run_or_404(db: AsyncSession, run_id: int, user: User):
    run = await svc.get_run(db, run_id, _effective_org_id(user))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reconciliation run not found")
    return run


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    return await _get_run_or_404(db, run_id, user)


@router.get("/runs/{run_id}/exceptions", response_model=ExceptionListOut)
async def list_exceptions(
    run_id: int, exception_type: str | None = Query(None),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    await _get_run_or_404(db, run_id, user)  # 404s / org-scopes access before listing
    rows, total = await svc.list_exceptions(db, run_id, exception_type)
    return ExceptionListOut(items=[ExceptionOut.model_validate(r, from_attributes=True) for r in rows], total=total)
