"""Schedules router — /api/schedules/...

Automates execution of existing pipelines. See services/schedule_service.py
for the integration boundary — this router never runs pipeline logic itself.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.schemas.schedules import (
    ScheduleCreateRequest,
    ScheduleListOut,
    ScheduleOut,
    ScheduleRunDetailOut,
    ScheduleRunListOut,
    ScheduleRunOut,
    ScheduleStatsOut,
    ScheduleUpdateRequest,
)
from app.services import schedule_service as svc

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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("next_run_at"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    schedules, total, name_map = await svc.list_schedules(
        db, organization_id=_effective_org_id(user), search=q, status_filter=status_filter,
        environment=environment, frequency=frequency, page=page, page_size=page_size,
        sort_by=sort_by, sort_dir=sort_dir,
    )
    items = [svc.to_out(s, name_map.get(s.owner_id)) for s in schedules]
    return ScheduleListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/{schedule_id}", response_model=ScheduleOut)
async def get_schedule(schedule_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return svc.to_out(schedule, name_map.get(schedule.owner_id))


# ── Create / update / delete ──────────────────────────────────────

@router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(body: ScheduleCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    try:
        schedule = await svc.create_schedule(db, organization_id=user.organization_id, owner_id=user.id, body=body)
    except svc.ScheduleValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return svc.to_out(schedule, user.full_name)


@router.put("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(schedule_id: int, body: ScheduleUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    try:
        schedule = await svc.update_schedule(db, schedule, body)
    except svc.ScheduleValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return svc.to_out(schedule, name_map.get(schedule.owner_id))


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    await svc.delete_schedule(db, schedule)


# ── Actions ───────────────────────────────────────────────────────

@router.post("/{schedule_id}/run", response_model=ScheduleOut)
async def run_now(schedule_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    await svc.execute_schedule(db, schedule, trigger="Manual")
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return svc.to_out(schedule, name_map.get(schedule.owner_id))


@router.post("/{schedule_id}/pause", response_model=ScheduleOut)
async def pause_schedule(schedule_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    schedule = await svc.pause_schedule(db, schedule)
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return svc.to_out(schedule, name_map.get(schedule.owner_id))


@router.post("/{schedule_id}/resume", response_model=ScheduleOut)
async def resume_schedule(schedule_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    schedule = await svc.resume_schedule(db, schedule)
    name_map = await svc.owner_name_map(db, {schedule.owner_id} if schedule.owner_id else set())
    return svc.to_out(schedule, name_map.get(schedule.owner_id))


@router.post("/{schedule_id}/clone", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def clone_schedule(schedule_id: int, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    schedule = await _get_or_404(db, schedule_id, user)
    clone = await svc.clone_schedule(db, schedule, user.id)
    return svc.to_out(clone, user.full_name)


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
    new_run = await svc.execute_schedule(db, schedule, trigger="Rerun")
    return ScheduleRunDetailOut(**svc.run_to_out(new_run))
