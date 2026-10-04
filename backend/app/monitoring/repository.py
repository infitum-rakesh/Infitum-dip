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

from app.models.anomaly import AnomalyExecution, OverallStatus
from app.models.incident import Incident, IncidentStatus
from app.models.invoice_intelligence.core import ApInvoice
from app.models.invoice_intelligence.erp import ApInvoiceErpStatus, ApInvoiceErpTransaction, ApInvoiceReconciliation, ApInvoiceReconciliationStatus
from app.models.invoice_intelligence.learning import ApInvoiceAuditEvent, ApInvoiceAuditEventType
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

    anomalies_critical = await _count(AnomalyExecution, AnomalyExecution.overall_status == OverallStatus.CRITICAL)
    anomalies_warning = await _count(AnomalyExecution, AnomalyExecution.overall_status == OverallStatus.WARNING)

    # INV12 — same window-scoped count pattern as anomalies above.
    invoices_dead_letter = await _count(ApInvoiceErpTransaction, ApInvoiceErpTransaction.status == ApInvoiceErpStatus.DEAD_LETTER)
    invoices_reconciliation_mismatch = await _count(ApInvoiceReconciliation, ApInvoiceReconciliation.status == ApInvoiceReconciliationStatus.MISMATCH)

    records_stmt = select(func.coalesce(func.sum(PipelineJob.records_out), 0)).where(PipelineJob.created_at >= since)
    if organization_id is not None:
        records_stmt = records_stmt.where(PipelineJob.organization_id == organization_id)
    records_processed = (await db.execute(records_stmt)).scalar_one()

    return {
        "jobs_total": jobs_total, "jobs_succeeded": jobs_succeeded, "jobs_failed": jobs_failed, "jobs_running": jobs_running,
        "loads_total": loads_total, "loads_failed": loads_failed,
        "validations_failed": validations_failed, "reconciliations_failed": reconciliations_failed,
        "incidents_open": incidents_open, "records_processed": int(records_processed or 0),
        "anomalies_critical": anomalies_critical, "anomalies_warning": anomalies_warning,
        "invoices_dead_letter": invoices_dead_letter, "invoices_reconciliation_mismatch": invoices_reconciliation_mismatch,
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


async def list_anomaly_executions(db: AsyncSession, organization_id: int | None, limit: int = 50, offset: int = 0) -> tuple[list[AnomalyExecution], int]:
    stmt = select(AnomalyExecution)
    count_stmt = select(func.count()).select_from(AnomalyExecution)
    if organization_id is not None:
        stmt = stmt.where(AnomalyExecution.organization_id == organization_id)
        count_stmt = count_stmt.where(AnomalyExecution.organization_id == organization_id)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(AnomalyExecution.id.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all()), total


# INV12 — Invoice Intelligence rollup into the same Data Quality tab.
# Joins ApInvoice for invoice_number purely for display (the transaction/
# reconciliation row itself is the real record; the join never filters).

async def list_invoice_erp_dead_letters(db: AsyncSession, organization_id: int | None, limit: int = 50, offset: int = 0) -> tuple[list[tuple[ApInvoiceErpTransaction, str | None]], int]:
    stmt = select(ApInvoiceErpTransaction, ApInvoice.invoice_number).join(ApInvoice, ApInvoice.id == ApInvoiceErpTransaction.invoice_id).where(
        ApInvoiceErpTransaction.status == ApInvoiceErpStatus.DEAD_LETTER
    )
    count_stmt = select(func.count()).select_from(ApInvoiceErpTransaction).where(ApInvoiceErpTransaction.status == ApInvoiceErpStatus.DEAD_LETTER)
    if organization_id is not None:
        stmt = stmt.where(ApInvoiceErpTransaction.organization_id == organization_id)
        count_stmt = count_stmt.where(ApInvoiceErpTransaction.organization_id == organization_id)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(ApInvoiceErpTransaction.id.desc()).offset(offset).limit(limit)
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()], total


async def list_invoice_reconciliation_mismatches(db: AsyncSession, organization_id: int | None, limit: int = 50, offset: int = 0) -> tuple[list[tuple[ApInvoiceReconciliation, str | None]], int]:
    stmt = select(ApInvoiceReconciliation, ApInvoice.invoice_number).join(ApInvoice, ApInvoice.id == ApInvoiceReconciliation.invoice_id).where(
        ApInvoiceReconciliation.status == ApInvoiceReconciliationStatus.MISMATCH
    )
    count_stmt = select(func.count()).select_from(ApInvoiceReconciliation).where(ApInvoiceReconciliation.status == ApInvoiceReconciliationStatus.MISMATCH)
    if organization_id is not None:
        stmt = stmt.where(ApInvoiceReconciliation.organization_id == organization_id)
        count_stmt = count_stmt.where(ApInvoiceReconciliation.organization_id == organization_id)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(ApInvoiceReconciliation.id.desc()).offset(offset).limit(limit)
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()], total


# ── Invoice Processing (INV21 — spec §114) ──────────────────────────
# Real rollup over data this build already records: ApInvoiceAuditEvent
# (the narrow lifecycle log Invoice Detail's own "Lifecycle Events" card
# reads — DECISION_MADE/ERP_SUBMITTED/ERP_SUCCEEDED/RECONCILIATION_RUN,
# see models/invoice_intelligence/learning.py), ApInvoiceErpTransaction
# (one row per real ERP HTTP attempt), and ReviewQueueItem (reused,
# unmodified). "Worker activity" has no persistent worker-pool concept in
# this codebase (ERP posting is a plain asyncio background task, not a
# worker with an id) — reported honestly here as "in-flight ERP attempts"
# rather than inventing worker identities that don't exist.

async def invoice_processing_overview(db: AsyncSession, organization_id: int | None, since: datetime) -> dict:
    async def _count(model, extra_where=None, *, since_field: str | None = "created_at"):
        stmt = select(func.count()).select_from(model)
        if since_field is not None:
            stmt = stmt.where(getattr(model, since_field) >= since)
        if organization_id is not None and hasattr(model, "organization_id"):
            stmt = stmt.where(model.organization_id == organization_id)
        if extra_where is not None:
            stmt = stmt.where(extra_where)
        return (await db.execute(stmt)).scalar_one()

    # ── Decision outcomes — real DECISION_MADE lifecycle events in window.
    # The decision itself is read out of the event's own recorded details
    # (invoice_decision_service.decide() writes {"decision": ..., "scores": ...}
    # there) rather than ApInvoice.final_decision, since a later correction
    # can move final_decision forward while this window still wants "what
    # was actually decided during this period."
    decision_events_stmt = select(ApInvoiceAuditEvent.invoice_id, ApInvoiceAuditEvent.details, ApInvoiceAuditEvent.created_at).where(
        ApInvoiceAuditEvent.event_type == ApInvoiceAuditEventType.DECISION_MADE, ApInvoiceAuditEvent.created_at >= since,
    )
    if organization_id is not None:
        decision_events_stmt = decision_events_stmt.where(ApInvoiceAuditEvent.organization_id == organization_id)
    decision_rows = (await db.execute(decision_events_stmt)).all()
    decided_total = len(decision_rows)
    decision_counts = {"STRAIGHT_THROUGH": 0, "REVIEW_REQUIRED": 0, "REJECT": 0, "RETRY": 0}
    for _inv_id, details, _created_at in decision_rows:
        d = (details or {}).get("decision")
        if d in decision_counts:
            decision_counts[d] += 1
    straight_through_rate_pct = round(100.0 * decision_counts["STRAIGHT_THROUGH"] / decided_total, 1) if decided_total else None

    # ── ERP call outcomes — every ApInvoiceErpTransaction row IS one real
    # HTTP attempt (see invoice_erp_service.py's own docstring).
    erp_calls_total = await _count(ApInvoiceErpTransaction)
    erp_succeeded = await _count(ApInvoiceErpTransaction, ApInvoiceErpTransaction.status == ApInvoiceErpStatus.SUCCEEDED)
    erp_failed = await _count(ApInvoiceErpTransaction, ApInvoiceErpTransaction.status == ApInvoiceErpStatus.FAILED)
    erp_dead_letter = await _count(ApInvoiceErpTransaction, ApInvoiceErpTransaction.status == ApInvoiceErpStatus.DEAD_LETTER)
    erp_retry_attempts = await _count(ApInvoiceErpTransaction, ApInvoiceErpTransaction.attempt_number > 1)
    # In-flight right now (not window-scoped — a real current queue depth).
    erp_in_flight_stmt = select(func.count()).select_from(ApInvoiceErpTransaction).where(
        ApInvoiceErpTransaction.status.in_([ApInvoiceErpStatus.PENDING, ApInvoiceErpStatus.RETRYING])
    )
    if organization_id is not None:
        erp_in_flight_stmt = erp_in_flight_stmt.where(ApInvoiceErpTransaction.organization_id == organization_id)
    erp_in_flight = (await db.execute(erp_in_flight_stmt)).scalar_one()

    # ── Review queue depth — current open items for invoice-typed documents
    # (ReviewQueueItem is reused unmodified; "Invoice" is the exact string
    # review_queue_service._build_item() stamps for this document type).
    from app.models.review_queue import ReviewQueueItem, ReviewStatus
    review_depth_stmt = select(func.count()).select_from(ReviewQueueItem).where(
        ReviewQueueItem.document_type == "Invoice",
        ReviewQueueItem.status.in_([ReviewStatus.PENDING, ReviewStatus.IN_PROGRESS, ReviewStatus.ESCALATED]),
    )
    if organization_id is not None:
        review_depth_stmt = review_depth_stmt.where(ReviewQueueItem.organization_id == organization_id)
    review_queue_depth = (await db.execute(review_depth_stmt)).scalar_one()

    # ── Stage durations — real deltas between consecutive lifecycle-event
    # timestamps, averaged per invoice. Bounded by the same window; fetched
    # once and grouped in Python (SQLite has no reliable cross-dialect
    # window-function story worth depending on here).
    all_events_stmt = select(ApInvoiceAuditEvent.invoice_id, ApInvoiceAuditEvent.event_type, ApInvoiceAuditEvent.created_at).where(
        ApInvoiceAuditEvent.created_at >= since,
        ApInvoiceAuditEvent.event_type.in_([
            ApInvoiceAuditEventType.DECISION_MADE, ApInvoiceAuditEventType.ERP_SUBMITTED,
            ApInvoiceAuditEventType.ERP_SUCCEEDED, ApInvoiceAuditEventType.RECONCILIATION_RUN,
        ]),
    )
    if organization_id is not None:
        all_events_stmt = all_events_stmt.where(ApInvoiceAuditEvent.organization_id == organization_id)
    by_invoice: dict[int, dict] = {}
    for invoice_id, event_type, created_at in (await db.execute(all_events_stmt)).all():
        slot = by_invoice.setdefault(invoice_id, {})
        # Earliest occurrence of each stage — a retry loop fires ERP_SUBMITTED
        # once per attempt; the first one is when the invoice actually
        # entered the ERP posting stage, which is the duration that matters.
        if event_type.value not in slot or created_at < slot[event_type.value]:
            slot[event_type.value] = created_at

    def _avg_delta(from_key: str, to_key: str) -> float | None:
        deltas = [
            (slot[to_key] - slot[from_key]).total_seconds()
            for slot in by_invoice.values()
            if from_key in slot and to_key in slot and slot[to_key] >= slot[from_key]
        ]
        return round(sum(deltas) / len(deltas), 1) if deltas else None

    avg_decision_to_erp_submit_seconds = _avg_delta("DECISION_MADE", "ERP_SUBMITTED")
    avg_erp_submit_to_succeeded_seconds = _avg_delta("ERP_SUBMITTED", "ERP_SUCCEEDED")
    avg_succeeded_to_reconciled_seconds = _avg_delta("ERP_SUCCEEDED", "RECONCILIATION_RUN")

    # ── Anomaly findings — invoice_anomaly_service.evaluate_invoice_
    # anomalies() (INV23) runs the real Anomaly Detection engine (same
    # Z-SCORE detector the standalone module uses) but is fully ephemeral
    # w.r.t. that module's own tables: it never writes an AnomalyJob/
    # AnomalyExecution row (those are job/dataset-scoped, not per-record),
    # so this ANOMALY_DETECTED audit event is the ONLY place any of this
    # is visible anywhere in Monitoring. Read straight from what decide()
    # already records — no new persistence added for this rollup.
    anomaly_events_stmt = select(
        ApInvoiceAuditEvent.invoice_id, ApInvoiceAuditEvent.details,
        ApInvoiceAuditEvent.message, ApInvoiceAuditEvent.created_at,
    ).where(
        ApInvoiceAuditEvent.event_type == ApInvoiceAuditEventType.ANOMALY_DETECTED,
        ApInvoiceAuditEvent.created_at >= since,
    )
    if organization_id is not None:
        anomaly_events_stmt = anomaly_events_stmt.where(ApInvoiceAuditEvent.organization_id == organization_id)
    anomaly_events_stmt = anomaly_events_stmt.order_by(ApInvoiceAuditEvent.created_at.desc())
    anomaly_event_rows = (await db.execute(anomaly_events_stmt)).all()

    anomaly_checks_run = len(anomaly_event_rows)
    anomaly_findings_total = 0
    anomaly_critical_count = 0
    anomaly_downgraded_count = 0
    recent_anomaly_findings: list[dict] = []

    lookup_ids = {row[0] for row in anomaly_event_rows}
    invoice_number_map: dict[int, str | None] = {}
    if lookup_ids:
        inv_rows = (await db.execute(select(ApInvoice.id, ApInvoice.invoice_number).where(ApInvoice.id.in_(lookup_ids)))).all()
        invoice_number_map = {iid: num for iid, num in inv_rows}

    for invoice_id, details, message, created_at in anomaly_event_rows:
        findings = (details or {}).get("findings") or []
        anomaly_findings_total += len(findings)
        downgraded = "downgraded" in (message or "")
        if downgraded:
            anomaly_downgraded_count += 1
        anomaly_critical_count += sum(1 for f in findings if f.get("severity") == "CRITICAL")
        if findings and len(recent_anomaly_findings) < 20:
            recent_anomaly_findings.append({
                "invoice_id": invoice_id,
                "invoice_number": invoice_number_map.get(invoice_id),
                "downgraded_to_review": downgraded,
                "created_at": created_at,
                "findings": findings,
            })

    return {
        "decided_total": decided_total,
        "straight_through_count": decision_counts["STRAIGHT_THROUGH"],
        "review_required_count": decision_counts["REVIEW_REQUIRED"],
        "reject_count": decision_counts["REJECT"],
        "retry_count": decision_counts["RETRY"],
        "straight_through_rate_pct": straight_through_rate_pct,
        "erp_calls_total": erp_calls_total, "erp_succeeded": erp_succeeded, "erp_failed": erp_failed,
        "erp_dead_letter": erp_dead_letter, "erp_retry_attempts": erp_retry_attempts, "erp_in_flight": erp_in_flight,
        "review_queue_depth": review_queue_depth,
        "avg_decision_to_erp_submit_seconds": avg_decision_to_erp_submit_seconds,
        "avg_erp_submit_to_succeeded_seconds": avg_erp_submit_to_succeeded_seconds,
        "avg_succeeded_to_reconciled_seconds": avg_succeeded_to_reconciled_seconds,
        "anomaly_checks_run": anomaly_checks_run,
        "anomaly_findings_total": anomaly_findings_total,
        "anomaly_critical_count": anomaly_critical_count,
        "anomaly_downgraded_count": anomaly_downgraded_count,
        "recent_anomaly_findings": recent_anomaly_findings,
    }


# ── Tally Integration (TALLY-J) ──
# Same "read straight off the real tables, no new persistence" convention
# as invoice_processing_overview() above. Every count here comes from a
# table another Tally phase already owns (TALLY-B extraction runs,
# TALLY-E reconciliation runs/exceptions, TALLY-I write-back transactions)
# — Monitoring never mutates any of them.

async def tally_overview(db: AsyncSession, organization_id: int | None, since: datetime) -> dict:
    from app.models.tally_extraction import TallyExtractionRun, TallyExtractionStatus
    from app.models.tally_reconciliation import TallyExceptionType, TallyReconciliationException, TallyReconciliationRun, TallyReconciliationRunStatus
    from app.models.tally_writeback import TallyVoucherWriteBackTransaction, TallyWriteBackStatus
    from app.models.review_queue import ReviewQueueItem, ReviewStatus

    async def _count(model, extra_where=None, *, since_field: str | None = "created_at"):
        stmt = select(func.count()).select_from(model)
        if since_field is not None:
            stmt = stmt.where(getattr(model, since_field) >= since)
        if organization_id is not None and hasattr(model, "organization_id"):
            stmt = stmt.where(model.organization_id == organization_id)
        if extra_where is not None:
            stmt = stmt.where(extra_where)
        return (await db.execute(stmt)).scalar_one()

    # ── Extraction (TALLY-B) ──
    extraction_runs_total = await _count(TallyExtractionRun)
    extraction_completed = await _count(TallyExtractionRun, TallyExtractionRun.status.in_(
        [TallyExtractionStatus.COMPLETED, TallyExtractionStatus.COMPLETED_WITH_ERRORS]
    ))
    extraction_failed = await _count(TallyExtractionRun, TallyExtractionRun.status == TallyExtractionStatus.FAILED)
    extraction_records_stmt = select(func.coalesce(func.sum(TallyExtractionRun.records_fetched), 0)).where(TallyExtractionRun.created_at >= since)
    if organization_id is not None:
        extraction_records_stmt = extraction_records_stmt.where(TallyExtractionRun.organization_id == organization_id)
    extraction_records_fetched = (await db.execute(extraction_records_stmt)).scalar_one()
    extraction_in_flight = await _count(TallyExtractionRun, TallyExtractionRun.status == TallyExtractionStatus.RUNNING, since_field=None)

    # ── Reconciliation (TALLY-E) ──
    reconciliation_runs_total = await _count(TallyReconciliationRun)
    reconciliation_succeeded = await _count(TallyReconciliationRun, TallyReconciliationRun.status == TallyReconciliationRunStatus.SUCCEEDED)
    reconciliation_failed = await _count(TallyReconciliationRun, TallyReconciliationRun.status == TallyReconciliationRunStatus.FAILED)
    scheduled_reconciliation_runs = await _count(TallyReconciliationRun, TallyReconciliationRun.trigger == "Schedule")

    exception_counts_stmt = select(TallyReconciliationException.exception_type, func.count()).where(
        TallyReconciliationException.created_at >= since,
    ).group_by(TallyReconciliationException.exception_type)
    if organization_id is not None:
        exception_counts_stmt = exception_counts_stmt.where(TallyReconciliationException.organization_id == organization_id)
    exception_counts = {t.value if hasattr(t, "value") else t: c for t, c in (await db.execute(exception_counts_stmt)).all()}
    open_exceptions = sum(v for k, v in exception_counts.items() if k != TallyExceptionType.MATCHED.value)

    # Review Queue depth attributable to Tally reconciliation specifically —
    # via TallyReconciliationException.review_item_id (TALLY-F's real anchor
    # pointer), never by document_type (which would also match ordinary
    # invoice-validation review items anchored to the same Document type).
    review_depth_stmt = select(func.count()).select_from(ReviewQueueItem).join(
        TallyReconciliationException, TallyReconciliationException.review_item_id == ReviewQueueItem.id,
    ).where(ReviewQueueItem.status.in_([ReviewStatus.PENDING, ReviewStatus.IN_PROGRESS, ReviewStatus.ESCALATED]))
    if organization_id is not None:
        review_depth_stmt = review_depth_stmt.where(ReviewQueueItem.organization_id == organization_id)
    tally_review_queue_depth = (await db.execute(review_depth_stmt)).scalar_one()

    # ── Write-back (TALLY-I) ──
    writeback_total = await _count(TallyVoucherWriteBackTransaction)
    writeback_succeeded = await _count(TallyVoucherWriteBackTransaction, TallyVoucherWriteBackTransaction.status == TallyWriteBackStatus.SUCCEEDED)
    writeback_dead_letter = await _count(TallyVoucherWriteBackTransaction, TallyVoucherWriteBackTransaction.status == TallyWriteBackStatus.DEAD_LETTER)
    writeback_failed = await _count(TallyVoucherWriteBackTransaction, TallyVoucherWriteBackTransaction.status == TallyWriteBackStatus.FAILED)
    writeback_in_flight = await _count(
        TallyVoucherWriteBackTransaction, TallyVoucherWriteBackTransaction.status.in_([TallyWriteBackStatus.PENDING, TallyWriteBackStatus.RETRYING]),
        since_field=None,
    )

    return {
        "extraction_runs_total": extraction_runs_total, "extraction_completed": extraction_completed,
        "extraction_failed": extraction_failed, "extraction_records_fetched": extraction_records_fetched,
        "extraction_in_flight": extraction_in_flight,
        "reconciliation_runs_total": reconciliation_runs_total, "reconciliation_succeeded": reconciliation_succeeded,
        "reconciliation_failed": reconciliation_failed, "scheduled_reconciliation_runs": scheduled_reconciliation_runs,
        "open_exceptions": open_exceptions, "exception_counts": exception_counts,
        "tally_review_queue_depth": tally_review_queue_depth,
        "writeback_total": writeback_total, "writeback_succeeded": writeback_succeeded,
        "writeback_dead_letter": writeback_dead_letter, "writeback_failed": writeback_failed,
        "writeback_in_flight": writeback_in_flight,
    }


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
