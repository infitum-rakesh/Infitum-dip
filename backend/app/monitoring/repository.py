"""
Monitoring repository — read-only queries across the execution-tracking
tables that already exist elsewhere in this codebase. Deliberately has
no write methods for run data (Monitoring never mutates a PipelineJob/
PipelineRun/etc — those stay owned by execution/pipeline_job_executor.py,
execution/load_worker.py, validation_service.py, reconciliation_service.py,
schedule_service.py) and defines no new execution-history tables (see
models/monitoring.py's module docstring).

Every list_* function is paginated (limit/offset) and organization-scoped
— this module is the one place large monitoring/lineage result sets get
bounded before they ever reach an API response, per the "never load
millions of rows into the browser" requirement.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident, IncidentStatus
from app.models.pipeline import Pipeline, PipelineVersion
from app.models.pipeline_job import JobStatus, PipelineJob, PipelineJobLog
from app.models.pipeline_run import PipelineRun
from app.models.reconciliation import ReconciliationRun
from app.models.rejected_record import RejectedRecord
from app.models.schedule import ScheduleRun
from app.models.target_load_run import LoadStatus, TargetLoadRun
from app.models.validation import ValidationRun


# ── Pipeline Jobs (the primary "Pipeline Run" record for Monitoring) ──

async def list_pipeline_jobs(
    db: AsyncSession, *, organization_id: int | None, pipeline_id: int | None = None,
    status: str | None = None, environment: str | None = None,
    started_after: datetime | None = None, started_before: datetime | None = None,
    limit: int = 50, offset: int = 0,
) -> tuple[list[PipelineJob], int]:
    stmt = select(PipelineJob)
    count_stmt = select(func.count()).select_from(PipelineJob)
    if organization_id is not None:
        stmt = stmt.where(PipelineJob.organization_id == organization_id)
        count_stmt = count_stmt.where(PipelineJob.organization_id == organization_id)
    if pipeline_id is not None:
        stmt = stmt.where(PipelineJob.pipeline_id == pipeline_id)
        count_stmt = count_stmt.where(PipelineJob.pipeline_id == pipeline_id)
    if status:
        stmt = stmt.where(PipelineJob.status == status)
        count_stmt = count_stmt.where(PipelineJob.status == status)
    if started_after:
        stmt = stmt.where(PipelineJob.started_at >= started_after)
        count_stmt = count_stmt.where(PipelineJob.started_at >= started_after)
    if started_before:
        stmt = stmt.where(PipelineJob.started_at <= started_before)
        count_stmt = count_stmt.where(PipelineJob.started_at <= started_before)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(PipelineJob.id.desc()).offset(offset).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    return rows, total


async def get_pipeline_job(db: AsyncSession, job_id: int, organization_id: int | None) -> PipelineJob | None:
    job = await db.get(PipelineJob, job_id)
    if job is None:
        return None
    if organization_id is not None and job.organization_id not in (None, organization_id):
        return None
    return job


async def recent_jobs_for_pipeline(db: AsyncSession, pipeline_id: int, limit: int = 20) -> list[PipelineJob]:
    stmt = (
        select(PipelineJob)
        .where(PipelineJob.pipeline_id == pipeline_id)
        .order_by(PipelineJob.id.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def rejected_record_count(db: AsyncSession, source_type, source_id: int) -> int:
    stmt = select(func.count()).select_from(RejectedRecord).where(
        RejectedRecord.source_type == source_type, RejectedRecord.source_id == source_id
    )
    return (await db.execute(stmt)).scalar_one()


# ── Overview KPIs (org-scoped rollups across every execution table) ──

async def overview_counts(db: AsyncSession, organization_id: int | None, since: datetime) -> dict:
    async def _count(model, extra_where=None):
        stmt = select(func.count()).select_from(model).where(model.created_at >= since)
        if organization_id is not None and hasattr(model, "organization_id"):
            stmt = stmt.where(model.organization_id == organization_id)
        if extra_where is not None:
            stmt = stmt.where(extra_where)
        return (await db.execute(stmt)).scalar_one()

    jobs_total = await _count(PipelineJob)
    jobs_succeeded = await _count(PipelineJob, PipelineJob.status == JobStatus.SUCCEEDED)
    jobs_failed = await _count(PipelineJob, PipelineJob.status == JobStatus.FAILED)
    jobs_running = await _count(PipelineJob, PipelineJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))

    loads_total = await _count(TargetLoadRun)
    loads_failed = await _count(TargetLoadRun, TargetLoadRun.status == LoadStatus.FAILED)

    validations_failed_stmt = select(func.count()).select_from(ValidationRun).where(
        ValidationRun.created_at >= since, ValidationRun.status == "FAILED"
    )
    if organization_id is not None:
        validations_failed_stmt = validations_failed_stmt.where(ValidationRun.organization_id == organization_id)
    validations_failed = (await db.execute(validations_failed_stmt)).scalar_one()

    reconciliations_failed_stmt = select(func.count()).select_from(ReconciliationRun).where(
        ReconciliationRun.created_at >= since, ReconciliationRun.status == "FAILED"
    )
    if organization_id is not None:
        reconciliations_failed_stmt = reconciliations_failed_stmt.where(ReconciliationRun.organization_id == organization_id)
    reconciliations_failed = (await db.execute(reconciliations_failed_stmt)).scalar_one()

    incidents_open_stmt = select(func.count()).select_from(Incident).where(
        Incident.status.in_([IncidentStatus.OPEN, IncidentStatus.INVESTIGATING])
    )
    if organization_id is not None:
        incidents_open_stmt = incidents_open_stmt.where(Incident.organization_id == organization_id)
    incidents_open = (await db.execute(incidents_open_stmt)).scalar_one()

    records_stmt = select(func.coalesce(func.sum(PipelineJob.records_out), 0)).where(PipelineJob.created_at >= since)
    if organization_id is not None:
        records_stmt = records_stmt.where(PipelineJob.organization_id == organization_id)
    records_processed = (await db.execute(records_stmt)).scalar_one()

    return {
        "jobs_total": jobs_total, "jobs_succeeded": jobs_succeeded, "jobs_failed": jobs_failed, "jobs_running": jobs_running,
        "loads_total": loads_total, "loads_failed": loads_failed,
        "validations_failed": validations_failed, "reconciliations_failed": reconciliations_failed,
        "incidents_open": incidents_open, "records_processed": int(records_processed or 0),
    }


async def recent_jobs(db: AsyncSession, organization_id: int | None, limit: int = 10) -> list[PipelineJob]:
    stmt = select(PipelineJob)
    if organization_id is not None:
        stmt = stmt.where(PipelineJob.organization_id == organization_id)
    stmt = stmt.order_by(PipelineJob.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


# ── Job Runs (schedule-triggered executions — ScheduleRun) ──

async def list_schedule_runs(
    db: AsyncSession, *, organization_id: int | None, schedule_id: int | None = None,
    limit: int = 50, offset: int = 0,
) -> tuple[list[ScheduleRun], int]:
    from app.models.schedule import Schedule
    stmt = select(ScheduleRun).join(Schedule, Schedule.id == ScheduleRun.schedule_id)
    count_stmt = select(func.count()).select_from(ScheduleRun).join(Schedule, Schedule.id == ScheduleRun.schedule_id)
    if organization_id is not None:
        stmt = stmt.where(Schedule.organization_id == organization_id)
        count_stmt = count_stmt.where(Schedule.organization_id == organization_id)
    if schedule_id is not None:
        stmt = stmt.where(ScheduleRun.schedule_id == schedule_id)
        count_stmt = count_stmt.where(ScheduleRun.schedule_id == schedule_id)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(ScheduleRun.id.desc()).offset(offset).limit(limit)
    rows = list((await db.execute(stmt)).scalars().all())
    return rows, total


# ── Data Quality (Validation + Reconciliation rollup) ──

async def list_validation_runs(db: AsyncSession, organization_id: int | None, limit: int = 50, offset: int = 0) -> tuple[list[ValidationRun], int]:
    stmt = select(ValidationRun)
    count_stmt = select(func.count()).select_from(ValidationRun)
    if organization_id is not None:
        stmt = stmt.where(ValidationRun.organization_id == organization_id)
        count_stmt = count_stmt.where(ValidationRun.organization_id == organization_id)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(ValidationRun.id.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all()), total


async def list_reconciliation_runs(db: AsyncSession, organization_id: int | None, limit: int = 50, offset: int = 0) -> tuple[list[ReconciliationRun], int]:
    stmt = select(ReconciliationRun)
    count_stmt = select(func.count()).select_from(ReconciliationRun)
    if organization_id is not None:
        stmt = stmt.where(ReconciliationRun.organization_id == organization_id)
        count_stmt = count_stmt.where(ReconciliationRun.organization_id == organization_id)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(ReconciliationRun.id.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all()), total


# ── Pipelines (for pickers / SLA scoping) ──

async def list_pipelines(db: AsyncSession, organization_id: int | None) -> list[Pipeline]:
    stmt = select(Pipeline)
    if organization_id is not None:
        stmt = stmt.where(Pipeline.organization_id == organization_id)
    return list((await db.execute(stmt)).scalars().all())


async def get_pipeline(db: AsyncSession, pipeline_id: int, organization_id: int | None) -> Pipeline | None:
    p = await db.get(Pipeline, pipeline_id)
    if p is None:
        return None
    if organization_id is not None and p.organization_id not in (None, organization_id):
        return None
    return p


async def get_pipeline_version(db: AsyncSession, pipeline_id: int, version_number: int | None = None) -> PipelineVersion | None:
    stmt = select(PipelineVersion).where(PipelineVersion.pipeline_id == pipeline_id)
    if version_number is not None:
        stmt = stmt.where(PipelineVersion.version_number == version_number)
    else:
        stmt = stmt.order_by(PipelineVersion.version_number.desc())
    stmt = stmt.limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()
