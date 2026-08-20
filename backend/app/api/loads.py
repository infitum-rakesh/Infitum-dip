"""Loads router — /api/mappings/{id}/load, /api/loads/{id}/... (Phase 6).

Real backend execution for a target load — see execution/load_executor.py
module docstring for scope and the single-process limitation (same as
executions.py's Phase 3 routes). Route handlers here only create/read
TargetLoadRun rows and kick off the background asyncio task; the actual
streamed read + bulk write happens in that task, never inside a request.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.execution import load_executor
from app.models.rejected_record import RejectSourceType
from app.models.user import User, UserRole
from app.schemas.loads import LoadListOut, LoadMetricsOut, LoadRunOut, StartLoadRequest
from app.schemas.rejected_records import RejectedRecordListOut, RejectedRecordOut
from app.services import load_service as svc
from app.services import mapping_service, reject_service

router = APIRouter(tags=["loads"])

_view = require_action("VIEW", module="TRANSFORMATION")
_execute = require_action("EXECUTE", module="TRANSFORMATION")


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _get_run_or_404(db: AsyncSession, run_id: int, user: User):
    run = await svc.get_load(db, run_id, _effective_org_id(user))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Load run not found")
    return run


@router.post("/mappings/{mapping_id}/load", response_model=LoadRunOut, status_code=status.HTTP_201_CREATED)
async def start_load(mapping_id: int, body: StartLoadRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_db)):
    mapping = await mapping_service.get_mapping(db, mapping_id, _effective_org_id(user))
    if mapping is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mapping not found")
    try:
        run = await svc.create_load(
            db, organization_id=user.organization_id, mapping=mapping,
            write_mode=body.write_mode, chunk_size=body.chunk_size, created_by=user.id,
            create_table=body.create_table, recreate_table=body.recreate_table,
            worker_count=body.worker_count, partition_count=body.partition_count,
        )
    except svc.LoadValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await load_executor.start_load(run.id)
    return run


@router.get("/mappings/{mapping_id}/loads", response_model=LoadListOut)
async def list_loads(mapping_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    runs, total = await svc.list_loads(db, mapping_id, _effective_org_id(user))
    return LoadListOut(items=runs, total=total)


@router.get("/loads/{run_id}/status", response_model=LoadRunOut)
async def get_status(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await _get_run_or_404(db, run_id, user)


@router.get("/loads/{run_id}/metrics", response_model=LoadMetricsOut)
async def get_metrics(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    run = await _get_run_or_404(db, run_id, user)
    return LoadMetricsOut(**svc.compute_metrics(run))


@router.post("/loads/{run_id}/cancel", response_model=LoadRunOut)
async def cancel_load(run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_db)):
    run = await _get_run_or_404(db, run_id, user)
    return await svc.cancel_load(db, run)


@router.get("/loads/{run_id}/rejected-records", response_model=RejectedRecordListOut)
async def list_rejected_records(
    run_id: int, page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    """Real per-row failures from this load (KK2/KK3) — each one's actual
    row data and the real error the target gave, not a summary count.
    A bulk-insert failure on a batch falls back to row-by-row so good
    rows still land; the ones that still fail end up here so a user can
    see and fix them (see loading/writers/base.py's write_batch_with_fallback())."""
    await _get_run_or_404(db, run_id, user)  # 404s / org-scopes before touching rejects
    items, total = await reject_service.list_rejects(
        db, source_type=RejectSourceType.TARGET_LOAD, source_id=run_id,
        organization_id=_effective_org_id(user), limit=page_size, offset=(page - 1) * page_size,
    )
    return RejectedRecordListOut(items=[RejectedRecordOut.model_validate(r) for r in items], total=total)
