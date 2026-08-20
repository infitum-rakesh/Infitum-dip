"""Pipeline Jobs router — /api/pipeline-jobs/...

Runs a FULL saved pipeline for real (see execution/pipeline_job_executor.py)
and tracks it. This is what Pipeline Builder's Run/Abort actions and its
live log view are backed by — separate from the narrower, single-source-
node extraction runs under /api/executions (models/pipeline_run.py).
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.models.rejected_record import RejectSourceType
from app.models.user import User, UserRole
from app.schemas.pipeline_jobs import PipelineJobListOut, PipelineJobLogsOut, PipelineJobOut
from app.schemas.rejected_records import RejectedRecordListOut, RejectedRecordOut
from app.services import pipeline_job_service as svc
from app.services import pipeline_service, reject_service
from app.execution import pipeline_job_executor
from app.execution.pipeline_job_executor import OUTPUT_BASE_PATH

router = APIRouter(prefix="/pipeline-jobs", tags=["pipeline-jobs"])

_view   = require_action("VIEW", module=None)
_create = require_action("CREATE", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


@router.post("", response_model=PipelineJobOut, status_code=status.HTTP_201_CREATED)
async def run_pipeline(pipeline_id: int, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
    pipeline = await pipeline_service.get_pipeline(db, pipeline_id, _effective_org_id(user))
    if pipeline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline not found")
    job = await svc.create_job(db, pipeline=pipeline, organization_id=user.organization_id, user_id=user.id, trigger="Manual")
    pipeline_job_executor.start_job(job.id)
    name_map = await svc.user_name_map(db, {job.created_by})
    return svc.job_to_out(job, name_map)


@router.get("", response_model=PipelineJobListOut)
async def list_jobs(
    pipeline_id: int | None = Query(None), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    jobs, total = await svc.list_jobs(db, organization_id=_effective_org_id(user), pipeline_id=pipeline_id, page=page, page_size=page_size)
    name_map = await svc.user_name_map(db, {j.created_by for j in jobs})
    return PipelineJobListOut(items=[svc.job_to_out(j, name_map) for j in jobs], total=total)


@router.get("/{job_id}", response_model=PipelineJobOut)
async def get_job(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    job = await svc.get_job(db, job_id, _effective_org_id(user))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    name_map = await svc.user_name_map(db, {job.created_by})
    return svc.job_to_out(job, name_map)


@router.get("/{job_id}/logs", response_model=PipelineJobLogsOut)
async def get_logs(job_id: int, since_id: int | None = Query(None), user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    job = await svc.get_job(db, job_id, _effective_org_id(user))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    logs = await svc.get_logs(db, job_id, since_id)
    return PipelineJobLogsOut(items=logs, job_status=job.status.value)


@router.get("/{job_id}/nodes/{node_id}/download")
async def download_output_file(job_id: int, node_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    """Downloads the real file a File-target node wrote during this
    specific job run — the exact path recorded onto the job's node_stats
    by pipeline_job_executor.py's _run_target() (no separate "latest file
    in the directory" guessing, so an old file from an earlier run of the
    same node is never served instead of this run's). 404s if the job
    doesn't belong to the caller's organization, the node isn't a file
    target, or the file has since been deleted from disk."""
    job = await svc.get_job(db, job_id, _effective_org_id(user))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    node_stat = (job.node_stats or {}).get(node_id)
    rel_path = node_stat.get("output_file") if node_stat else None
    if not rel_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This node didn't produce a downloadable output file in this run.")

    base = Path(OUTPUT_BASE_PATH).resolve()
    resolved = (base / rel_path).resolve()
    if base not in resolved.parents or not resolved.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The output file is no longer available on disk.")
    return FileResponse(resolved, filename=resolved.name)


@router.post("/{job_id}/cancel", response_model=PipelineJobOut)
async def cancel_job(job_id: int, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
    job = await svc.get_job(db, job_id, _effective_org_id(user))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.status.value not in ("PENDING", "RUNNING"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Job is already {job.status.value.lower()} — nothing to cancel.")
    requested = pipeline_job_executor.request_cancel(job_id)
    if not requested:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Job is not currently running in this process (it may have been interrupted by a server restart) — it cannot be cancelled, only re-run.")
    name_map = await svc.user_name_map(db, {job.created_by})
    return svc.job_to_out(job, name_map)


@router.get("/{job_id}/rejected-records", response_model=RejectedRecordListOut)
async def list_rejected_records(
    job_id: int, node_id: str | None = Query(None), page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    """Real per-row failures from this job's dataset target node(s) (KK2/
    KK3) — each one's actual row data and the real error the target gave.
    A bulk-insert failure on a batch falls back to row-by-row so good rows
    still land; the ones that still fail end up here, tagged with which
    target node rejected them (node_id) since one job can have more than
    one target — pass node_id to narrow to just one."""
    job = await svc.get_job(db, job_id, _effective_org_id(user))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    items, total = await reject_service.list_rejects(
        db, source_type=RejectSourceType.PIPELINE_JOB, source_id=job_id, node_id=node_id,
        organization_id=_effective_org_id(user), limit=page_size, offset=(page - 1) * page_size,
    )
    return RejectedRecordListOut(items=[RejectedRecordOut.model_validate(r) for r in items], total=total)
