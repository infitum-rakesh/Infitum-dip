"""Schedules router — /api/schedules/...

Automates execution of existing pipelines. See services/schedule_service.py
for the integration boundary — this router never runs pipeline logic itself.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.schemas.document_mapping import RetryScheduledMappingResponse
from app.schemas.schedules import (
    DocumentRegistryListOut,
    DocumentRegistryRowOut,
    ScheduleCreateRequest,
    ScheduleListOut,
    ScheduleOut,
    ScheduleRunDetailOut,
    ScheduleRunListOut,
    ScheduleRunOut,
    ScheduleStatsOut,
    ScheduleUpdateRequest,
)
from app.services import document_mapping_service as doc_mapping_svc
from app.services import document_schedule_service as doc_sched_svc
from app.services import schedule_service as svc
from app.services.document_mapping_service import DocumentMappingError

router = APIRouter(prefix="/schedules", tags=["schedules"])

# Schedules is a foundational, always-available module — no per-module
# license gate (module=None), just role-tier action checks, same as
# Connections. See app.core.deps.require_action.
_view = require_action("VIEW", module=None)
_create = require_action("CREATE", module=None)
_edit = require_action("EDIT", module=None)
_execute = require_action("EXECUTE", module=None)
_delete = require_action("DELETE", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")


async def _get_or_404(db: AsyncSession, schedule_id: int, user: User):
    schedule = await svc.get_schedule(db, schedule_id, _effective_org_id(user))
    if schedule is None:
        raise _not_found()
    return schedule


async def _to_out(db: AsyncSession, schedule, owner_name: str | None) -> dict:
    """svc.to_out() needs the schedule's DocumentScheduleConfig/
    TallyReconciliationScheduleConfig/IndiaVerificationScheduleConfig row
    (if any) to include `document_config`/`tally_config`/
    `india_verification_config` in the response — fetched once here so
    every route below stays a one-liner instead of repeating the lookup."""
    document_config = await svc.get_document_config(db, schedule.id)
    tally_config = await svc.get_tally_config(db, schedule.id)
    india_verification_config = await svc.get_india_verification_config(db, schedule.id)
    return svc.to_out(schedule, owner_name, document_config, tally_config, india_verification_config)


# ── KPIs ──────────────────────────────────────────────────────────
# NOTE: literal-path /stats must stay registered before /{schedule_id} —
# Starlette matches by position, not type.

@router.get("/stats", response_model=ScheduleStatsOut)
async def stats(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    return await svc.get_stats(db, _effective_org_id(user))


# ── List / get ────────────────────────────────────────────────────

@router.get("", response_model=ScheduleListOut)
async def list_schedules(
    q: str = Query("", description="Search name, pipeline, status, environment, frequency, owner"),
    status_filter: str | None = Query(None, alias="status"),
    environment: str | None = Query(None),
    frequency: str | None = Query(None),
    anomaly_job_id: int | None = Query(None, description="Filter to the schedule(s) linked to one AnomalyJob"),
    tally_connection_id: int | None = Query(None, description="Filter to the schedule(s) linked to one Tally connection"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("next_run_at"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    schedules, total, name_map = await svc.list_schedules(
        db, organization_id=_effective_org_id(user), search=q, status_filter=status_filter,
        environment=environment, frequency=frequency, anomaly_job_id=anomaly_job_id,
        tally_connection_id=tally_connection_id, page=page, page_size=page_size,
        sort_by=sort_by, sort_dir=sort_dir,
    )
    items = [await _to_out(db, s, name_map.get(s.owner_id)) for s in schedules]
    return ScheduleListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/{schedule_id}", response_model=ScheduleOut)
async def get_schedule(schedule_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return await _to_out(db, schedule, name_map.get(schedule.owner_id))


# ── Create / update / delete ──────────────────────────────────────

@router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(body: ScheduleCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    try:
        schedule = await svc.create_schedule(db, organization_id=user.organization_id, owner_id=user.id, body=body)
    except svc.ScheduleValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, schedule, user.full_name)


@router.put("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(schedule_id: int, body: ScheduleUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    try:
        schedule = await svc.update_schedule(db, schedule, body)
    except svc.ScheduleValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return await _to_out(db, schedule, name_map.get(schedule.owner_id))


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    await svc.delete_schedule(db, schedule)


# ── Actions ───────────────────────────────────────────────────────

@router.post("/{schedule_id}/run", response_model=ScheduleOut)
async def run_now(schedule_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    try:
        await svc.execute_schedule(db, schedule, trigger="Manual")
    except svc.ScheduleAlreadyRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return await _to_out(db, schedule, name_map.get(schedule.owner_id))


@router.post("/{schedule_id}/pause", response_model=ScheduleOut)
async def pause_schedule(schedule_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    schedule = await svc.pause_schedule(db, schedule)
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return await _to_out(db, schedule, name_map.get(schedule.owner_id))


@router.post("/{schedule_id}/resume", response_model=ScheduleOut)
async def resume_schedule(schedule_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    schedule = await svc.resume_schedule(db, schedule)
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return await _to_out(db, schedule, name_map.get(schedule.owner_id))


@router.post("/{schedule_id}/retry-mapping", response_model=RetryScheduledMappingResponse)
async def retry_scheduled_mapping(schedule_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    """Section 7.5 — "allow retry after config correction": call this
    after fixing whatever made this Document Source schedule's mapping
    unresolvable (corrected its mapping profile, published a pending
    version, or fixed the organisation's default) to re-attempt mapping
    for every document this schedule has successfully extracted.
    See document_mapping_service.retry_scheduled_mapping()'s own
    docstring for why this reprocesses every eligible document rather
    than only the ones that previously failed, and why that's still
    idempotent."""
    await _get_or_404(db, schedule_id, user)
    try:
        counts = await doc_mapping_svc.retry_scheduled_mapping(db, schedule_id=schedule_id, organization_id=_effective_org_id(user))
    except DocumentMappingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return RetryScheduledMappingResponse(**counts)


@router.post("/{schedule_id}/clone", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def clone_schedule(schedule_id: int, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    clone = await svc.clone_schedule(db, schedule, user.id)
    # A document-source schedule's config isn't cloned yet (spec section
    # 12's Duplicate action still works for the schedule shell; the clone
    # simply has no source configured until the user fills it in via Edit
    # — same "starts PAUSED so it can't silently double-run" caution
    # clone_schedule() already applies to the schedule itself).
    return await _to_out(db, clone, user.full_name)


# ── Execution history ─────────────────────────────────────────────

@router.get("/{schedule_id}/runs", response_model=ScheduleRunListOut)
async def list_runs(
    schedule_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_or_404(db, schedule_id, user)
    runs, total = await svc.get_runs(db, schedule_id, page=page, page_size=page_size)
    return ScheduleRunListOut(items=[ScheduleRunOut(**svc.run_to_out(r)) for r in runs], total=total)


@router.get("/{schedule_id}/runs/{run_id}", response_model=ScheduleRunDetailOut)
async def get_run(schedule_id: int, run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    await _get_or_404(db, schedule_id, user)
    run = await svc.get_run(db, schedule_id, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return ScheduleRunDetailOut(**svc.run_to_out(run))


@router.post("/{schedule_id}/runs/{run_id}/rerun", response_model=ScheduleRunDetailOut, status_code=status.HTTP_201_CREATED)
async def rerun(schedule_id: int, run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    original = await svc.get_run(db, schedule_id, run_id)
    if original is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    try:
        new_run = await svc.execute_schedule(db, schedule, trigger="Rerun")
    except svc.ScheduleAlreadyRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return ScheduleRunDetailOut(**svc.run_to_out(new_run))


@router.post("/{schedule_id}/runs/{run_id}/cancel", response_model=ScheduleRunDetailOut)
async def cancel_run(schedule_id: int, run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    """Spec section 81 — cooperative cancellation, meaningful only for a
    RUNNING document-schedule run (see schedule_service._bounded()'s
    between-files checkpoint); a harmless no-op flag-set on anything else."""
    await _get_or_404(db, schedule_id, user)
    run = await svc.get_run(db, schedule_id, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    run = await svc.request_cancel(db, run)
    return ScheduleRunDetailOut(**svc.run_to_out(run))


@router.get("/{schedule_id}/runs/{run_id}/documents", response_model=DocumentRegistryListOut)
async def list_run_documents(
    schedule_id: int, run_id: int,
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    """Per-file breakdown for a document-schedule run (spec section 15/44)
    — the Run Detail screen's drilldown table."""
    await _get_or_404(db, schedule_id, user)
    run = await svc.get_run(db, schedule_id, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    rows, total = await doc_sched_svc.list_registry_for_run(db, run_id, page=page, page_size=page_size)
    return DocumentRegistryListOut(items=[DocumentRegistryRowOut.model_validate(r) for r in rows], total=total)
