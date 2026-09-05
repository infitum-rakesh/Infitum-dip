"""
dashboard_service — real aggregation for the main org Dashboard.

Every number here comes from a live query against real tables (documents,
pipeline_jobs, validation_rule_results, incidents) — no hardcoded/mocked
values. Where a truthful trend can't be computed from real historical
data (e.g. a rate with no meaningful "yesterday" baseline), the field is
returned as None rather than a fabricated arrow, and the frontend must
render that as "—", not invent a direction.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document, DocumentStatus
from app.models.document_schedule import DocumentScheduleConfig, DocumentSourceRegistry, DocumentSourceStatus
from app.models.incident import Incident, IncidentStatus
from app.models.pipeline_job import JobStatus, PipelineJob
from app.models.schedule import Schedule, ScheduleStatus
from app.models.validation import ValidationRuleResult

ACTIVE_INCIDENT_STATUSES = (IncidentStatus.OPEN, IncidentStatus.INVESTIGATING, IncidentStatus.MITIGATED)
TERMINAL_JOB_STATUSES = (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


def _day_bounds(now: datetime) -> tuple[datetime, datetime, datetime]:
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    return yesterday_start, today_start, now


def _pct_change(today: int, yesterday: int) -> float | None:
    if yesterday == 0:
        return None  # no baseline — a truthful trend can't be computed, not "0% change"
    return round((today - yesterday) / yesterday * 100, 1)


async def get_dashboard(db: AsyncSession, organization_id: int | None) -> dict:
    now = datetime.now(timezone.utc)
    yesterday_start, today_start, _ = _day_bounds(now)

    def scoped(stmt, model):
        if organization_id is not None:
            return stmt.where(model.organization_id == organization_id)
        return stmt

    async def count(stmt) -> int:
        r = await db.execute(select(func.count()).select_from(stmt.subquery()))
        return r.scalar_one() or 0

    # ── Documents ────────────────────────────────────────────
    documents_processed = await count(scoped(select(Document).where(Document.status == DocumentStatus.COMPLETED), Document))
    docs_today = await count(scoped(select(Document).where(Document.status == DocumentStatus.COMPLETED, Document.uploaded_at >= today_start), Document))
    docs_yesterday = await count(scoped(select(Document).where(Document.status == DocumentStatus.COMPLETED, Document.uploaded_at >= yesterday_start, Document.uploaded_at < today_start), Document))

    ocr_avg_result = await db.execute(scoped(select(func.avg(Document.ocr_percent)).where(Document.ocr_percent.is_not(None)), Document))
    ocr_avg = ocr_avg_result.scalar_one()
    ocr_success_rate = round(ocr_avg, 1) if ocr_avg is not None else None

    # ── Validation ───────────────────────────────────────────
    # ValidationRuleResult has no organization_id of its own — scope via
    # its parent run, same pattern validation_service.py uses elsewhere.
    from app.models.validation import ValidationRun
    val_stmt = select(func.avg(ValidationRuleResult.pass_rate)).join(ValidationRun, ValidationRun.id == ValidationRuleResult.run_id)
    if organization_id is not None:
        val_stmt = val_stmt.where(ValidationRun.organization_id == organization_id)
    val_avg_result = await db.execute(val_stmt)
    val_avg = val_avg_result.scalar_one()
    validation_pass_rate = round(val_avg, 1) if val_avg is not None else None

    # ── Incidents ────────────────────────────────────────────
    active_incidents = await count(scoped(select(Incident).where(Incident.status.in_(ACTIVE_INCIDENT_STATUSES)), Incident))

    # ── Pipeline jobs ────────────────────────────────────────
    pipeline_runs_today = await count(scoped(select(PipelineJob).where(PipelineJob.created_at >= today_start), PipelineJob))
    pipeline_runs_yesterday = await count(scoped(select(PipelineJob).where(PipelineJob.created_at >= yesterday_start, PipelineJob.created_at < today_start), PipelineJob))

    terminal_stmt = scoped(select(PipelineJob).where(PipelineJob.status.in_(TERMINAL_JOB_STATUSES)), PipelineJob)
    terminal_total = await count(terminal_stmt)
    succeeded_total = await count(scoped(select(PipelineJob).where(PipelineJob.status == JobStatus.SUCCEEDED), PipelineJob))
    pipeline_success_rate = round(succeeded_total / terminal_total * 100, 1) if terminal_total else None

    # ── Scheduled Document Intelligence Workflow ─────────────
    # Real counts off DocumentScheduleConfig/DocumentSourceRegistry — the
    # same tables schedule_service.py's document-schedule branch and
    # document_schedule_service.py write to. No separate rollup table.
    active_document_schedules = await count(
        scoped(select(Schedule).join(DocumentScheduleConfig, DocumentScheduleConfig.schedule_id == Schedule.id)
               .where(Schedule.status == ScheduleStatus.ACTIVE), Schedule)
    )
    scheduled_docs_today = await count(
        scoped(select(DocumentSourceRegistry).where(
            DocumentSourceRegistry.status == DocumentSourceStatus.PROCESSED,
            DocumentSourceRegistry.last_processed_at >= today_start,
        ), DocumentSourceRegistry)
    )
    scheduled_docs_review_required = await count(
        scoped(select(DocumentSourceRegistry).where(DocumentSourceRegistry.status == DocumentSourceStatus.REVIEW_REQUIRED), DocumentSourceRegistry)
    )
    scheduled_docs_failed_today = await count(
        scoped(select(DocumentSourceRegistry).where(
            DocumentSourceRegistry.status == DocumentSourceStatus.FAILED,
            DocumentSourceRegistry.last_processed_at >= today_start,
        ), DocumentSourceRegistry)
    )

    # ── Recent runs (last 5, newest first) ──────────────────
    recent_stmt = scoped(select(PipelineJob), PipelineJob).order_by(PipelineJob.created_at.desc()).limit(5)
    recent_result = await db.execute(recent_stmt)
    recent_jobs = list(recent_result.scalars().all())
    recent_runs = [
        {
            "job_id": j.id, "pipeline_name": j.pipeline_name, "status": j.status.value,
            "records_out": j.records_out, "trigger": j.trigger,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "duration_sec": (j.completed_at - j.started_at).total_seconds() if j.started_at and j.completed_at else None,
        }
        for j in recent_jobs
    ]

    return {
        "metrics": {
            "documents_processed": documents_processed,
            "documents_processed_trend_pct": _pct_change(docs_today, docs_yesterday),
            "ocr_success_rate": ocr_success_rate,
            "validation_pass_rate": validation_pass_rate,
            "active_incidents": active_incidents,
            "pipeline_runs_today": pipeline_runs_today,
            "pipeline_runs_today_trend_pct": _pct_change(pipeline_runs_today, pipeline_runs_yesterday),
            "pipeline_success_rate": pipeline_success_rate,
            "active_document_schedules": active_document_schedules,
            "scheduled_docs_today": scheduled_docs_today,
            "scheduled_docs_review_required": scheduled_docs_review_required,
            "scheduled_docs_failed_today": scheduled_docs_failed_today,
        },
        "recent_runs": recent_runs,
    }
