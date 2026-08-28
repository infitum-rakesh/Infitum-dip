"""Executions router — /api/pipelines/{id}/run, /api/runs/{id}/...

Real backend execution for a single Source Dataset node's extraction —
see execution/pipeline_executor.py module docstring for the current scope
(extract-to-staging only) and its single-process limitation. The route
handlers here only create/read PipelineRun rows and kick off the
background asyncio task; the actual chunked reads happen in that task,
never inside a request.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.execution import pipeline_executor
from app.models.user import User, UserRole
from app.schemas.executions import RunListOut, RunMetricsOut, RunOut, RunStatusOut, StartRunRequest
from app.services import execution_service as svc
from app.services import pipeline_service

router = APIRouter(tags=["executions"])

_view = require_action("VIEW", module=None)
_execute = require_action("EXECUTE", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _get_run_or_404(db: AsyncSession, run_id: int, user: User):
    run = await svc.get_run(db, run_id, _effective_org_id(user))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run


@router.post("/pipelines/{pipeline_id}/run", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def start_run(pipeline_id: int, body: StartRunRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await pipeline_service.get_pipeline(db, pipeline_id, _effective_org_id(user))
    if pipeline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline not found")
    try:
        run = await svc.create_run(
            db, organization_id=user.organization_id, pipeline=pipeline, node_id=body.node_id,
            key_column=body.key_column, chunk_size=body.chunk_size, worker_count=body.worker_count,
            partition_count=body.partition_count, created_by=user.id,
            archive_dir=body.archive_dir, error_dir=body.error_dir,
        )
    except svc.RunValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await pipeline_executor.start_run(run.id, run.organization_id)
    return run


@router.get("/pipelines/{pipeline_id}/runs", response_model=RunListOut)
async def list_runs(pipeline_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    runs, total = await svc.list_runs(db, pipeline_id, _effective_org_id(user))
    return RunListOut(items=runs, total=total)


@router.get("/runs/{run_id}/status", response_model=RunStatusOut)
async def get_status(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    partitions = await svc.list_partitions(db, run_id)
    return RunStatusOut(run=run, partitions=partitions)


@router.get("/runs/{run_id}/metrics", response_model=RunMetricsOut)
async def get_metrics(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    partitions = await svc.list_partitions(db, run_id)
    return RunMetricsOut(**svc.compute_metrics(run, partitions))


@router.post("/runs/{run_id}/resume", response_model=RunOut)
async def resume_run(run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    try:
        run = await svc.resume_run(db, run)
    except svc.RunValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await pipeline_executor.start_run(run.id, run.organization_id)
    return run


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    return await svc.cancel_run(db, run)
