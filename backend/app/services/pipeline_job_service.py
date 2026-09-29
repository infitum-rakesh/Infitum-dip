"""
pipeline_job_service — CRUD + query helpers for PipelineJob/PipelineJobLog.

Real execution lives in execution/pipeline_job_executor.py; this module
only creates the row and hands off — same fire-and-forget shape as
execution/pipeline_executor.py (create_job() does NOT await the run; the
caller starts it via pipeline_job_executor.start_job() and the frontend
polls status/logs).
"""
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal

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
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


def calculate_job_metrics(job: PipelineJob) -> dict:
    stats = job.node_stats or {}
    metrics = stats.get("__metrics__") or {}

    stage = metrics.get("stage")
    if not stage:
        if job.status.value in ("SUCCEEDED", "COMPLETED"):
            stage = "COMPLETED"
        elif job.status.value == "FAILED":
            stage = "FAILED"
        elif job.status.value == "CANCELLED":
            stage = "ABORTED"
        elif job.status.value == "PENDING":
            stage = "PREPARING"
        else:
            stage = "PROCESSING"

    started_at = job.started_at
    completed_at = job.completed_at
    if completed_at and started_at:
        s_at = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=timezone.utc)
        c_at = completed_at if completed_at.tzinfo is not None else completed_at.replace(tzinfo=timezone.utc)
        elapsed_seconds = max(0.1, round((c_at - s_at).total_seconds(), 1))
    elif started_at:
        now = datetime.now(timezone.utc)
        s_at = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=timezone.utc)
        elapsed_seconds = max(0.1, round((now - s_at).total_seconds(), 1))
    else:
        elapsed_seconds = 0.0

    extracted = metrics.get("extracted")
    if extracted is None:
        extracted = sum(
            s.get("rows_out", 0) for s in stats.values()
            if isinstance(s, dict) and s.get("type") == "source"
        )

    processed = metrics.get("processed")
    if processed is None:
        transform_rows = [
            s.get("rows_out", 0) for s in stats.values()
            if isinstance(s, dict) and s.get("type") not in ("source", "target") and "type" in s
        ]
        processed = transform_rows[-1] if transform_rows else extracted

    loaded = metrics.get("loaded")
    if loaded is None:
        if job.records_out is not None:
            loaded = job.records_out
        else:
            loaded = sum(
                s.get("rows_out", 0) for s in stats.values()
                if isinstance(s, dict) and s.get("type") == "target"
            )

    rejected = metrics.get("rejected")
    if rejected is None:
        rejected = sum(
            s.get("rows_rejected", 0) for s in stats.values()
            if isinstance(s, dict) and s.get("type") == "target"
        )

    if elapsed_seconds > 0:
        load_rate = round(loaded / elapsed_seconds, 1)
        extraction_rate = round(extracted / elapsed_seconds, 1)
        processing_rate = round(processed / elapsed_seconds, 1)
    else:
        load_rate = 0.0
        extraction_rate = 0.0
        processing_rate = 0.0

    rows_per_minute = round(load_rate * 60.0, 1)

    total_rows = metrics.get("total_rows")
    if job.status.value in ("SUCCEEDED", "COMPLETED"):
        progress_pct = 100.0
    elif total_rows is not None and total_rows > 0:
        progress_pct = min(100.0, round((loaded / total_rows) * 100.0, 2))
    else:
        progress_pct = None

    if job.status.value == "RUNNING" and total_rows is not None and total_rows > loaded and load_rate > 0:
        remaining_rows = total_rows - loaded
        eta_seconds = round(remaining_rows / load_rate, 1)
    else:
        eta_seconds = None

    bulk_api = {
        "current_chunk": metrics.get("current_chunk", 0),
        "total_chunks": metrics.get("total_chunks"),
        "current_chunk_rows": metrics.get("current_chunk_rows", 0),
        "chunks_processed": metrics.get("chunks_processed", 0),
    }

    return {
        "stage": stage,
        "progress": {
            "percentage": progress_pct,
            "processed": loaded,
            "total": total_rows,
        },
        "throughput": {
            "rows_per_second": load_rate,
            "extraction_rate": extraction_rate,
            "load_rate": load_rate,
            "processing_rate": processing_rate,
            "rows_per_minute": rows_per_minute,
        },
        "timing": {
            "started_at": started_at,
            "completed_at": completed_at,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
        },
        "bulk_api": bulk_api,
        "records": {
            "extracted": extracted,
            "processed": processed,
            "loaded": loaded,
            "rejected": rejected,
        },
    }


def job_to_out(job: PipelineJob, name_map: dict[int, str]) -> dict:
    m = calculate_job_metrics(job)
    return {
        "id": job.id, "pipeline_id": job.pipeline_id, "pipeline_version": job.pipeline_version,
        "pipeline_name": job.pipeline_name, "status": job.status.value, "stage": m["stage"],
        "trigger": job.trigger, "node_stats": job.node_stats or {}, "records_in": job.records_in,
        "records_out": job.records_out, "error_message": job.error_message, "started_at": job.started_at,
        "completed_at": job.completed_at, "created_by": job.created_by,
        "created_by_name": name_map.get(job.created_by), "created_at": job.created_at,
        "progress": m["progress"], "throughput": m["throughput"], "timing": m["timing"],
        "bulk_api": m["bulk_api"], "records": m["records"],
    }
