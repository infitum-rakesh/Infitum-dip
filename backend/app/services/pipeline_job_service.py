"""
pipeline_job_service — CRUD + query helpers for PipelineJob/PipelineJobLog.

Real execution lives in execution/pipeline_job_executor.py; this module
only creates the row and hands off — same fire-and-forget shape as
execution/pipeline_executor.py (create_job() does NOT await the run; the
caller starts it via pipeline_job_executor.start_job() and the frontend
polls status/logs).
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline import Pipeline
from app.models.pipeline_job import PipelineJob, PipelineJobLog
from app.models.user import User


async def create_job(
    db: AsyncSession, *, pipeline: Pipeline, organization_id: int | None,
    user_id: int | None, trigger: str = "Manual",
) -> PipelineJob:
    job = PipelineJob(
        organization_id=organization_id, pipeline_id=pipeline.id, pipeline_version=pipeline.current_version,
        pipeline_name=pipeline.name, trigger=trigger, created_by=user_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def get_job(db: AsyncSession, job_id: int, organization_id: int | None) -> PipelineJob | None:
    stmt = select(PipelineJob).where(PipelineJob.id == job_id)
    if organization_id is not None:
        stmt = stmt.where(PipelineJob.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_jobs(
    db: AsyncSession, *, organization_id: int | None, pipeline_id: int | None = None,
    page: int = 1, page_size: int = 20,
) -> tuple[list[PipelineJob], int]:
    stmt = select(PipelineJob)
    if organization_id is not None:
        stmt = stmt.where(PipelineJob.organization_id == organization_id)
    if pipeline_id is not None:
        stmt = stmt.where(PipelineJob.pipeline_id == pipeline_id)
    result = await db.execute(stmt)
    jobs = sorted(result.scalars().all(), key=lambda j: j.created_at, reverse=True)
    total = len(jobs)
    start = (page - 1) * page_size
    return jobs[start:start + page_size], total


async def get_logs(db: AsyncSession, job_id: int, since_id: int | None = None) -> list[PipelineJobLog]:
    stmt = select(PipelineJobLog).where(PipelineJobLog.job_id == job_id)
    if since_id:
        stmt = stmt.where(PipelineJobLog.id > since_id)
    stmt = stmt.order_by(PipelineJobLog.id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def user_name_map(db: AsyncSession, ids: set[int | None]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    result = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row.id: row.full_name for row in result.all()}


def job_to_out(job: PipelineJob, name_map: dict[int, str]) -> dict:
    return {
        "id": job.id, "pipeline_id": job.pipeline_id, "pipeline_version": job.pipeline_version,
        "pipeline_name": job.pipeline_name, "status": job.status.value, "trigger": job.trigger,
        "node_stats": job.node_stats or {}, "records_in": job.records_in, "records_out": job.records_out,
        "error_message": job.error_message, "started_at": job.started_at, "completed_at": job.completed_at,
        "created_by": job.created_by, "created_by_name": name_map.get(job.created_by),
        "created_at": job.created_at,
    }
