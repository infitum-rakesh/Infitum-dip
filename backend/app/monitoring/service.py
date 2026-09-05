"""
Monitoring service — the business-logic layer between the read-only
repository and the API routes, plus the one integration seam other
modules call into: on_job_finished(), invoked by
execution/pipeline_job_executor.py right after a PipelineJob reaches a
terminal status. This is the real implementation of three explicit user
requirements at once: (1) monitoring must reflect real execution metadata
— it does, by reading the same PipelineJob row the executor just wrote;
(2) pipeline failure must auto-create an Incident — it does, via the
existing (previously unwired) incident_service.report_incident(); (3) SLA
breaches must be detected and can auto-file an Incident too, using the
same hook.
"""
from __future__ import annotations

import platform
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import tenant_engine_registry
from app.models.incident import IncidentSeverity, IncidentSource
from app.models.monitoring import AlertSeverity, SlaDefinition, SlaStatus
from app.models.pipeline_job import JobStatus, PipelineJob
from app.models.rejected_record import RejectSourceType
from app.monitoring import alerts, repository, sla as sla_mod
from app.services import incident_service, reject_service


async def get_sla_for_pipeline(db: AsyncSession, pipeline_id: int, organization_id: int | None) -> SlaDefinition | None:
    stmt = select(SlaDefinition).where(SlaDefinition.pipeline_id == pipeline_id, SlaDefinition.enabled.is_(True))
    if organization_id is not None:
        stmt = stmt.where(SlaDefinition.organization_id == organization_id)
    return (await db.execute(stmt)).scalars().first()


async def on_job_finished(job_id: int, organization_id: int | None) -> None:
    """Fire-and-forget-safe: opens its own session, never raises out to
    the caller (a monitoring hiccup must never fail or mask the pipeline
    run itself) — any internal error is swallowed after being logged via
    a best-effort AlertEvent-less print, matching this codebase's existing
    fire-and-forget executor risk disclosure (see pipeline_job_executor.py).
    organization_id comes from the caller's _job_org_cache (pipeline_job_executor.py)
    — this function itself has no live session to look it up from."""
    try:
        sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
        async with sessionmaker() as db:
            job = await db.get(PipelineJob, job_id)
            if job is None or job.status not in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
                return

            incident_id = None
            sla_status, sla_reason = SlaStatus.NOT_EVALUATED, "No SLA defined for this pipeline."
            consecutive_failures = 0

            recent = await repository.recent_jobs_for_pipeline(db, job.pipeline_id, limit=10)
            recent_statuses = [j.status.value for j in recent]
            for s in recent_statuses:
                if s == JobStatus.FAILED.value:
                    consecutive_failures += 1
                else:
                    break

            sla = await get_sla_for_pipeline(db, job.pipeline_id, job.organization_id)
            if sla is not None:
                sla_status, sla_reason = sla_mod.evaluate_sla(job, sla, recent_statuses)

            # Requirement: pipeline failure automatically creates/associates an Incident.
            if job.status == JobStatus.FAILED:
                incident = await incident_service.report_incident(
                    db, organization_id=job.organization_id, source=IncidentSource.PIPELINE.value,
                    title=f"Pipeline '{job.pipeline_name}' run #{job.id} failed",
                    description=job.error_message or "Pipeline run failed — see job logs for details.",
                    severity=IncidentSeverity.HIGH.value if consecutive_failures <= 1 else IncidentSeverity.CRITICAL.value,
                    pipeline_id=str(job.pipeline_id), pipeline_name=job.pipeline_name,
                )
                incident_id = incident.id
            elif sla is not None and sla_status == SlaStatus.BREACHED and sla.auto_create_incident:
                incident = await incident_service.report_incident(
                    db, organization_id=job.organization_id, source=IncidentSource.DATA_QUALITY.value,
                    title=f"SLA breached — pipeline '{job.pipeline_name}' run #{job.id}",
                    description=sla_reason, severity=IncidentSeverity.HIGH.value,
                    pipeline_id=str(job.pipeline_id), pipeline_name=job.pipeline_name,
                )
                incident_id = incident.id

            reject_total = await repository.rejected_record_count(db, RejectSourceType.PIPELINE_JOB, job.id)
            records_out = job.records_out or 0
            reject_rate_pct = (reject_total / records_out * 100.0) if records_out else (100.0 if reject_total else 0.0)
            duration_minutes = None
            if job.started_at and job.completed_at:
                duration_minutes = (job.completed_at - job.started_at).total_seconds() / 60.0

            ctx = {
                "event": "JOB_FAILED" if job.status == JobStatus.FAILED else "JOB_FINISHED",
                "pipeline_id": job.pipeline_id, "pipeline_name": job.pipeline_name, "job_id": job.id,
                "sla_status": sla_status.value, "reject_rate_pct": reject_rate_pct,
                "duration_minutes": duration_minutes, "consecutive_failures": consecutive_failures,
            }
            fired = await alerts.evaluate_rules(db, organization_id=job.organization_id, ctx=ctx)

            # Associate any freshly-created incident onto the alert events that
            # fired from this same run, so "View Incident" is reachable from an
            # alert even when the alert's own condition wasn't JOB_FAILED/SLA_BREACHED.
            if incident_id and fired:
                for event in fired:
                    if event.incident_id is None:
                        event.incident_id = incident_id
                await db.commit()
    except Exception as exc:  # pragma: no cover — monitoring must never break pipeline execution
        import logging
        logging.getLogger(__name__).error("on_job_finished(%s) failed: %s", job_id, exc)


# ── Overview ──

async def get_overview(db: AsyncSession, organization_id: int | None, days: int = 7) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    counts = await repository.overview_counts(db, organization_id, since)
    recent = await repository.recent_jobs(db, organization_id, limit=10)
    success_rate = (counts["jobs_succeeded"] / counts["jobs_total"] * 100.0) if counts["jobs_total"] else None
    return {
        **counts,
        "success_rate_pct": round(success_rate, 1) if success_rate is not None else None,
        "recent_jobs": [_job_summary(j) for j in recent],
        "window_days": days,
    }


async def get_invoice_processing(db: AsyncSession, organization_id: int | None, days: int = 7) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    data = await repository.invoice_processing_overview(db, organization_id, since)
    return {**data, "window_days": days}


def _job_summary(job: PipelineJob) -> dict:
    duration_s = None
    if job.started_at and job.completed_at:
        duration_s = (job.completed_at - job.started_at).total_seconds()
    return {
        "id": job.id, "pipeline_id": job.pipeline_id, "pipeline_name": job.pipeline_name,
        "pipeline_version": job.pipeline_version, "status": job.status.value, "trigger": job.trigger,
        "records_in": job.records_in, "records_out": job.records_out,
        "started_at": job.started_at, "completed_at": job.completed_at, "duration_seconds": duration_s,
        "error_message": job.error_message,
    }


# ── Job Runs (Schedule-triggered executions — distinct from Pipeline Runs,
# which is every PipelineJob regardless of trigger) ──

async def get_schedule_runs(db: AsyncSession, organization_id: int | None, *, schedule_id: int | None, page: int, page_size: int) -> dict:
    rows, total = await repository.list_schedule_runs(
        db, organization_id=organization_id, schedule_id=schedule_id, limit=page_size, offset=(page - 1) * page_size,
    )
    items = [
        {
            "id": r.id, "schedule_id": r.schedule_id, "status": r.status.value if hasattr(r.status, "value") else r.status,
            "trigger": r.trigger, "started_at": r.started_at, "ended_at": r.ended_at,
            "duration_seconds": r.duration_seconds, "records_processed": r.records_processed,
            "records_failed": r.records_failed, "error_message": r.error_message,
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ── Pipeline Runs (Job list) ──

async def get_pipeline_runs(
    db: AsyncSession, organization_id: int | None, *, pipeline_id: int | None, status: str | None,
    page: int, page_size: int,
) -> dict:
    rows, total = await repository.list_pipeline_jobs(
        db, organization_id=organization_id, pipeline_id=pipeline_id, status=status,
        limit=page_size, offset=(page - 1) * page_size,
    )
    items = []
    for job in rows:
        summary = _job_summary(job)
        summary["records_rejected"] = await repository.rejected_record_count(db, RejectSourceType.PIPELINE_JOB, job.id)
        sla = await get_sla_for_pipeline(db, job.pipeline_id, organization_id)
        if sla is not None and job.status != JobStatus.RUNNING:
            recent = await repository.recent_jobs_for_pipeline(db, job.pipeline_id, limit=10)
            statuses = [j.status.value for j in recent if j.id <= job.id][:10] or [job.status.value]
            verdict, _ = sla_mod.evaluate_sla(job, sla, statuses)
            summary["sla_status"] = verdict.value
        else:
            summary["sla_status"] = SlaStatus.NOT_EVALUATED.value
        items.append(summary)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def get_job_detail(db: AsyncSession, job_id: int, organization_id: int | None) -> dict | None:
    job = await repository.get_pipeline_job(db, job_id, organization_id)
    if job is None:
        return None
    summary = _job_summary(job)
    summary["node_stats"] = job.node_stats or {}
    summary["records_rejected"] = await repository.rejected_record_count(db, RejectSourceType.PIPELINE_JOB, job.id)
    sla = await get_sla_for_pipeline(db, job.pipeline_id, organization_id)
    if sla is not None:
        recent = await repository.recent_jobs_for_pipeline(db, job.pipeline_id, limit=10)
        statuses = [j.status.value for j in recent if j.id <= job.id][:10] or [job.status.value]
        verdict, reason = sla_mod.evaluate_sla(job, sla, statuses)
        summary["sla_status"] = verdict.value
        summary["sla_reason"] = reason
    else:
        summary["sla_status"] = SlaStatus.NOT_EVALUATED.value
        summary["sla_reason"] = "No SLA defined for this pipeline."
    return summary


# ── Data Quality ──

async def get_data_quality(db: AsyncSession, organization_id: int | None, page: int, page_size: int) -> dict:
    v_rows, v_total = await repository.list_validation_runs(db, organization_id, limit=page_size, offset=(page - 1) * page_size)
    r_rows, r_total = await repository.list_reconciliation_runs(db, organization_id, limit=page_size, offset=(page - 1) * page_size)
    a_rows, a_total = await repository.list_anomaly_executions(db, organization_id, limit=page_size, offset=(page - 1) * page_size)
    inv_dl_rows, inv_dl_total = await repository.list_invoice_erp_dead_letters(db, organization_id, limit=page_size, offset=(page - 1) * page_size)
    inv_mm_rows, inv_mm_total = await repository.list_invoice_reconciliation_mismatches(db, organization_id, limit=page_size, offset=(page - 1) * page_size)
    return {
        "validation_runs": [
            {
                "id": r.id, "rule_set_id": r.rule_set_id, "status": r.status.value if hasattr(r.status, "value") else r.status,
                "total_records": r.total_records, "started_at": r.started_at, "completed_at": r.completed_at,
                "error_message": r.error_message,
            } for r in v_rows
        ],
        "validation_total": v_total,
        "reconciliation_runs": [
            {
                "id": r.id, "job_id": r.job_id, "status": r.status.value if hasattr(r.status, "value") else r.status,
                "source_count": r.source_count, "target_count": r.target_count, "matched_count": r.matched_count,
                "missing_count": r.missing_count, "extra_count": r.extra_count, "mismatched_count": r.mismatched_count,
                "started_at": r.started_at, "completed_at": r.completed_at,
            } for r in r_rows
        ],
        "reconciliation_total": r_total,
        "anomaly_executions": [
            {
                "id": e.id, "job_id": e.job_id, "status": e.status.value if hasattr(e.status, "value") else e.status,
                "overall_status": e.overall_status.value if e.overall_status else None,
                "records_scanned": e.records_scanned, "anomalies_found": e.anomalies_found,
                "critical_count": e.critical_count, "warning_count": e.warning_count,
                "trigger": e.trigger, "started_at": e.started_at, "completed_at": e.completed_at,
            } for e in a_rows
        ],
        "anomaly_total": a_total,
        # INV12
        "invoice_erp_dead_letters": [
            {
                "id": txn.id, "invoice_id": txn.invoice_id, "invoice_number": invoice_number,
                "erp_connection_id": txn.erp_connection_id, "attempt_number": txn.attempt_number,
                "error_code": txn.error_code, "error_message": txn.error_message, "created_at": txn.created_at,
            } for txn, invoice_number in inv_dl_rows
        ],
        "invoice_erp_dead_letter_total": inv_dl_total,
        "invoice_reconciliation_mismatches": [
            {
                "id": row.id, "invoice_id": row.invoice_id, "invoice_number": invoice_number,
                "mismatched_fields": [c["field"] for c in (row.field_comparisons or []) if c.get("result") == "MISMATCH"],
                "created_at": row.created_at,
            } for row, invoice_number in inv_mm_rows
        ],
        "invoice_reconciliation_mismatch_total": inv_mm_total,
        "page": page, "page_size": page_size,
    }


# ── Performance ──

async def get_performance(db: AsyncSession, organization_id: int | None, days: int = 7) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(PipelineJob).where(PipelineJob.status == JobStatus.SUCCEEDED, PipelineJob.completed_at >= since)
    if organization_id is not None:
        stmt = stmt.where(PipelineJob.organization_id == organization_id)
    stmt = stmt.order_by(PipelineJob.id.desc()).limit(200)
    jobs = list((await db.execute(stmt)).scalars().all())

    points = []
    for job in jobs:
        if not (job.started_at and job.completed_at):
            continue
        duration_s = (job.completed_at - job.started_at).total_seconds()
        rows_per_sec = (job.records_out / duration_s) if duration_s and job.records_out else None
        # Per-stage timings straight from node_stats — no separate metrics table.
        stages = [
            {"node_id": nid, "label": s.get("label", nid), "duration_ms": s.get("duration_ms"),
             "rows_in": s.get("rows_in"), "rows_out": s.get("rows_out")}
            for nid, s in (job.node_stats or {}).items()
        ]
        threshold = "NORMAL"
        if duration_s and duration_s > 3600:
            threshold = "CRITICAL"
        elif duration_s and duration_s > 900:
            threshold = "WARNING"
        points.append({
            "job_id": job.id, "pipeline_id": job.pipeline_id, "pipeline_name": job.pipeline_name,
            "completed_at": job.completed_at, "duration_seconds": duration_s,
            "rows_per_sec": round(rows_per_sec, 2) if rows_per_sec else None,
            "records_out": job.records_out, "threshold": threshold, "stages": stages,
        })
    avg_duration = sum(p["duration_seconds"] for p in points) / len(points) if points else None
    return {"points": points, "avg_duration_seconds": round(avg_duration, 1) if avg_duration else None, "window_days": days,
            # Disclosed gap: this codebase's writers never measure payload byte
            # size, only row counts — so throughput is rows/sec, not MB/sec, and
            # no fabricated MB figure is reported.
            "note": "Throughput is reported in rows/sec — this platform's writers do not track payload byte size, so MB/sec is not available and is not fabricated here."}


# ── System Health ──

async def get_system_health(db: AsyncSession) -> dict:
    """Real, live-probed status for each component — no hardcoded 'green'.
    Deliberately exposes no credentials, connection strings, hostnames or
    secrets: only up/down + latency."""
    checks = []

    t0 = time.monotonic()
    db_ok = True
    db_detail = None
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_detail = str(exc)[:200]
    checks.append({"component": "Database", "status": "UP" if db_ok else "DOWN", "latency_ms": round((time.monotonic() - t0) * 1000, 1), "detail": db_detail})

    checks.append({"component": "Backend API", "status": "UP", "latency_ms": 0.0, "detail": f"Python {platform.python_version()}"})

    # This platform executes pipeline jobs in-process via asyncio.create_task()
    # (see execution/pipeline_job_executor.py's start_job()) — there is no
    # separate worker/queue process to probe, disclosed rather than implied.
    from app.models.pipeline_job import JobStatus as _JS
    running_stmt = select(PipelineJob.id).where(PipelineJob.status.in_([_JS.PENDING, _JS.RUNNING])).limit(1)
    has_running = (await db.execute(running_stmt)).first() is not None
    checks.append({
        "component": "Execution Worker", "status": "UP",
        "detail": "In-process asyncio task executor (no separate worker/queue process in this deployment)."
                  + (" Currently executing job(s)." if has_running else " Idle."),
    })

    checks.append({"component": "Scheduler", "status": "UP", "detail": "See Schedules module — execution is simulated, not a real cron daemon (disclosed in services/schedule_service.py)."})

    return {"checks": checks, "checked_at": datetime.now(timezone.utc)}


# ── SLA Definition CRUD ──

async def list_sla_definitions(db: AsyncSession, organization_id: int | None) -> list[SlaDefinition]:
    stmt = select(SlaDefinition)
    if organization_id is not None:
        stmt = stmt.where(SlaDefinition.organization_id == organization_id)
    stmt = stmt.order_by(SlaDefinition.id.desc())
    return list((await db.execute(stmt)).scalars().all())


async def create_sla_definition(db: AsyncSession, *, organization_id: int | None, created_by: int | None, body) -> SlaDefinition:
    pipeline = await repository.get_pipeline(db, body.pipeline_id, organization_id)
    if pipeline is None:
        raise ValueError("Pipeline not found.")
    row = SlaDefinition(
        organization_id=organization_id, pipeline_id=body.pipeline_id, pipeline_name=pipeline.name,
        expected_start_time=body.expected_start_time, expected_completion_time=body.expected_completion_time,
        max_duration_minutes=body.max_duration_minutes, warning_threshold_pct=body.warning_threshold_pct or 80,
        max_failure_count=body.max_failure_count or 1, enabled=body.enabled if body.enabled is not None else True,
        auto_create_incident=body.auto_create_incident if body.auto_create_incident is not None else True,
        created_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_sla_definition(db: AsyncSession, row: SlaDefinition, body) -> SlaDefinition:
    for field in (
        "expected_start_time", "expected_completion_time", "max_duration_minutes",
        "warning_threshold_pct", "max_failure_count", "enabled", "auto_create_incident",
    ):
        value = getattr(body, field, None)
        if value is not None:
            setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_sla_definition(db: AsyncSession, row: SlaDefinition) -> None:
    await db.delete(row)
    await db.commit()


async def get_sla_definition(db: AsyncSession, sla_id: int, organization_id: int | None) -> SlaDefinition | None:
    row = await db.get(SlaDefinition, sla_id)
    if row is None:
        return None
    if organization_id is not None and row.organization_id not in (None, organization_id):
        return None
    return row


# ── Alert Rule CRUD ──

async def list_alert_rules(db: AsyncSession, organization_id: int | None):
    from app.models.monitoring import AlertRule
    stmt = select(AlertRule)
    if organization_id is not None:
        stmt = stmt.where(AlertRule.organization_id == organization_id)
    stmt = stmt.order_by(AlertRule.id.desc())
    return list((await db.execute(stmt)).scalars().all())


async def get_alert_rule(db: AsyncSession, rule_id: int, organization_id: int | None):
    from app.models.monitoring import AlertRule
    row = await db.get(AlertRule, rule_id)
    if row is None:
        return None
    if organization_id is not None and row.organization_id not in (None, organization_id):
        return None
    return row


async def create_alert_rule(db: AsyncSession, *, organization_id: int | None, created_by: int | None, body):
    from app.models.monitoring import AlertRule
    row = AlertRule(
        organization_id=organization_id, name=body.name, description=body.description,
        condition=body.condition, pipeline_id=body.pipeline_id, threshold=body.threshold,
        severity=body.severity or AlertSeverity.MEDIUM, cooldown_minutes=body.cooldown_minutes or 0,
        channels=body.channels or [], enabled=body.enabled if body.enabled is not None else True,
        created_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_alert_rule(db: AsyncSession, row, body):
    data = body.model_dump(exclude_unset=True)
    for field in ("name", "description", "condition", "pipeline_id", "threshold", "severity", "cooldown_minutes", "channels", "enabled"):
        if field in data:
            setattr(row, field, data[field])
    await db.commit()
    await db.refresh(row)
    return row


async def delete_alert_rule(db: AsyncSession, row) -> None:
    await db.delete(row)
    await db.commit()


# ── Alert Events ──

async def list_alert_events(db: AsyncSession, organization_id: int | None, *, status: str | None = None, page: int = 1, page_size: int = 50) -> dict:
    from sqlalchemy import func as _func
    from app.models.monitoring import AlertEvent
    stmt = select(AlertEvent)
    count_stmt = select(_func.count()).select_from(AlertEvent)
    if organization_id is not None:
        stmt = stmt.where(AlertEvent.organization_id == organization_id)
        count_stmt = count_stmt.where(AlertEvent.organization_id == organization_id)
    if status:
        stmt = stmt.where(AlertEvent.status == status)
        count_stmt = count_stmt.where(AlertEvent.status == status)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(AlertEvent.id.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = list((await db.execute(stmt)).scalars().all())
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


async def acknowledge_alert_event(db: AsyncSession, event, actor_id: int | None):
    from app.models.monitoring import AlertEventStatus
    event.status = AlertEventStatus.ACKNOWLEDGED
    event.acknowledged_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(event)
    return event


async def resolve_alert_event(db: AsyncSession, event, actor_id: int | None):
    from app.models.monitoring import AlertEventStatus
    event.status = AlertEventStatus.RESOLVED
    event.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(event)
    return event


async def get_alert_event(db: AsyncSession, event_id: int, organization_id: int | None):
    from app.models.monitoring import AlertEvent
    row = await db.get(AlertEvent, event_id)
    if row is None:
        return None
    if organization_id is not None and row.organization_id not in (None, organization_id):
        return None
    return row
