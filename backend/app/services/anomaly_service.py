"""
anomaly_service — the API-facing layer for Anomaly Detection. AD1 covers
AnomalyJob CRUD; AD3 adds run_job() (a thin wrapper around anomaly/
engine.py's run_execution — this file stays a CRUD/status-transition
layer, same division of labor validation_service/reconciliation_service
already use versus their own engine packages) plus the execution/
column-result/record read paths the Results screens need.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pathlib import Path

from app.anomaly import baseline as anomaly_baseline
from app.anomaly import engine as anomaly_engine
from app.anomaly import profiler as anomaly_profiler
from app.connectors.base import ConnectorError
from app.database.connection import AsyncSessionLocal
from app.execution import anomaly_export_executor
from app.execution.anomaly_export_executor import ANOMALY_REPORT_BASE_PATH
from app.models.audit_log import record_event
from app.models.anomaly import (
    AnomalyBaseline,
    AnomalyColumnResult,
    AnomalyExecution,
    AnomalyExecutionStatus,
    AnomalyFeedback,
    AnomalyJob,
    AnomalyRecord,
    AnomalyReport,
    FeedbackStatus,
    OverallStatus,
    ReportFormat,
    ReportStatus,
)
from app.models.incident import IncidentSeverity, IncidentSource
from app.models.user import User
from app.monitoring import alerts
from app.services import connection_service, incident_service, uploaded_file_service

FILE_BACKED_SENTINEL = 0


class AnomalyServiceError(Exception):
    """Bad request — a referenced connection/object doesn't exist, or the
    job can't be saved as given."""


# ── CRUD ─────────────────────────────────────────────────────────

async def _verify_connection_owned(db: AsyncSession, connection_id: int, organization_id: int | None) -> None:
    """A connection_id in a request body can't be trusted as-is — verify
    it actually belongs to this org before ever storing it. Without this,
    any user could point an anomaly job at another tenant's connection and
    run real queries against it. Mirrors validation_service's identical
    check."""
    if connection_id == FILE_BACKED_SENTINEL or organization_id is None:
        return
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise AnomalyServiceError(f"Connection {connection_id} was not found.")


async def _verify_uploaded_file_owned(db: AsyncSession, uploaded_file_id: int | None, organization_id: int | None) -> None:
    if uploaded_file_id is None:
        return
    record = await uploaded_file_service.get_by_id(db, uploaded_file_id, organization_id)
    if record is None:
        raise AnomalyServiceError(f"Uploaded file {uploaded_file_id} was not found.")


async def _assert_name_available(
    db: AsyncSession, *, organization_id: int | None, name: str, exclude_id: int | None = None,
) -> None:
    """Job names only have to be unique within one organization (or the
    platform-wide/super-admin scope, when organization_id is None) —
    case-insensitive/trimmed, same convention validation_service uses."""
    stmt = select(AnomalyJob.id).where(func.lower(func.trim(AnomalyJob.name)) == name.strip().lower())
    stmt = stmt.where(AnomalyJob.organization_id.is_(None)) if organization_id is None \
        else stmt.where(AnomalyJob.organization_id == organization_id)
    if exclude_id is not None:
        stmt = stmt.where(AnomalyJob.id != exclude_id)
    existing = (await db.execute(stmt)).first()
    if existing is not None:
        raise AnomalyServiceError(f'An anomaly job named "{name.strip()}" already exists.')


async def create_job(
    db: AsyncSession, *, organization_id: int | None, name: str, description: str | None,
    connection_id: int, schema_name: str | None, object_name: str, uploaded_file_id: int | None,
    columns: list[dict], detection_config: dict, baseline_config: dict, threshold_config: dict,
    on_critical: dict, schedule_frequency: str | None, schedule_cron: str | None, enabled: bool,
    retention_days: int | None = None, created_by: int | None = None,
) -> AnomalyJob:
    await _assert_name_available(db, organization_id=organization_id, name=name)
    await _verify_connection_owned(db, connection_id, organization_id)
    await _verify_uploaded_file_owned(db, uploaded_file_id, organization_id)
    row = AnomalyJob(
        organization_id=organization_id, name=name, description=description, connection_id=connection_id,
        schema_name=schema_name, object_name=object_name, uploaded_file_id=uploaded_file_id, columns=columns,
        detection_config=detection_config, baseline_config=baseline_config, threshold_config=threshold_config,
        on_critical=on_critical, schedule_frequency=schedule_frequency, schedule_cron=schedule_cron,
        enabled=enabled, retention_days=retention_days, created_by=created_by, updated_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await record_event(
        db, organization_id=organization_id, actor_id=created_by, action="Anomaly Job Created",
        module="Anomaly Detection", resource=row.name, resource_id=str(row.id),
        details=f"connection_id={connection_id} object={schema_name or ''}.{object_name}",
    )
    return row


async def update_job(db: AsyncSession, row: AnomalyJob, *, patch: dict, updated_by: int | None) -> AnomalyJob:
    before = {
        "name": row.name, "detection_config": row.detection_config, "baseline_config": row.baseline_config,
        "threshold_config": row.threshold_config, "enabled": row.enabled, "retention_days": row.retention_days,
    }
    if "name" in patch and patch["name"] and patch["name"].strip() != row.name:
        await _assert_name_available(db, organization_id=row.organization_id, name=patch["name"], exclude_id=row.id)
        row.name = patch["name"]
    for field in (
        "description", "columns", "detection_config", "baseline_config", "threshold_config",
        "on_critical", "schedule_frequency", "schedule_cron", "enabled", "retention_days",
    ):
        if field in patch and patch[field] is not None:
            setattr(row, field, patch[field])
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    after = {
        "name": row.name, "detection_config": row.detection_config, "baseline_config": row.baseline_config,
        "threshold_config": row.threshold_config, "enabled": row.enabled, "retention_days": row.retention_days,
    }
    await record_event(
        db, organization_id=row.organization_id, actor_id=updated_by, action="Anomaly Job Updated",
        module="Anomaly Detection", resource=row.name, resource_id=str(row.id),
        before_value=before, after_value=after,
    )
    return row


async def delete_job(db: AsyncSession, row: AnomalyJob, *, deleted_by: int | None = None) -> None:
    # Explicit cascade — same reasoning as validation_service.delete_rule_set/
    # reconciliation_service.delete_job: no ORM/DB-level cascade is declared,
    # and this app's control-plane DB is SQLite (recycles integer PKs on
    # delete), so a later job could otherwise inherit a deleted one's entire
    # execution history through nothing but a matching job_id.
    execution_ids = (await db.execute(select(AnomalyExecution.id).where(AnomalyExecution.job_id == row.id))).scalars().all()
    if execution_ids:
        column_result_ids = (
            await db.execute(select(AnomalyColumnResult.id).where(AnomalyColumnResult.execution_id.in_(execution_ids)))
        ).scalars().all()
        if column_result_ids:
            record_ids = (await db.execute(select(AnomalyRecord.id).where(AnomalyRecord.column_result_id.in_(column_result_ids)))).scalars().all()
            if record_ids:
                await db.execute(delete(AnomalyFeedback).where(AnomalyFeedback.record_id.in_(record_ids)))
            await db.execute(delete(AnomalyRecord).where(AnomalyRecord.column_result_id.in_(column_result_ids)))
        await db.execute(delete(AnomalyColumnResult).where(AnomalyColumnResult.execution_id.in_(execution_ids)))
        await db.execute(delete(AnomalyExecution).where(AnomalyExecution.job_id == row.id))
    await db.execute(delete(AnomalyBaseline).where(AnomalyBaseline.job_id == row.id))
    job_id, job_name, organization_id = row.id, row.name, row.organization_id
    await db.delete(row)
    await db.commit()
    await record_event(
        db, organization_id=organization_id, actor_id=deleted_by, action="Anomaly Job Deleted",
        module="Anomaly Detection", resource=job_name, resource_id=str(job_id),
    )


async def preview_profile(
    db: AsyncSession, *, organization_id: int | None, connection_id: int, schema_name: str | None,
    object_name: str, uploaded_file_id: int | None, columns: list[dict],
) -> dict:
    """On-demand profiling for the job editor (AD7) — the SAME real
    anomaly/profiler.py two-tier profiler a run uses, called directly
    against a live RowSource without creating any AnomalyExecution/
    AnomalyColumnResult rows. Lets a user see real column stats (null %,
    distinct count, mean/stddev, category breakdown...) while still
    configuring a job, not just after the first real run — genuine
    profiling, not a guess, just not persisted."""
    await _verify_connection_owned(db, connection_id, organization_id)
    await _verify_uploaded_file_owned(db, uploaded_file_id, organization_id)
    row_source, _connector = await anomaly_engine._build_row_source(
        db, connection_id=connection_id, schema=schema_name, object_name=object_name,
        uploaded_file_id=uploaded_file_id, organization_id=organization_id,
    )
    return anomaly_profiler.profile_columns(row_source, schema_name, object_name, columns)


async def get_job(db: AsyncSession, job_id: int, organization_id: int | None) -> AnomalyJob | None:
    stmt = select(AnomalyJob).where(AnomalyJob.id == job_id)
    if organization_id is not None:
        stmt = stmt.where(AnomalyJob.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_jobs(db: AsyncSession, organization_id: int | None) -> tuple[list[AnomalyJob], int]:
    stmt = select(AnomalyJob)
    if organization_id is not None:
        stmt = stmt.where(AnomalyJob.organization_id == organization_id)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.updated_at, reverse=True)
    return rows, len(rows)


async def user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


# ── Run ──────────────────────────────────────────────────────────

async def run_job(
    db: AsyncSession, *, organization_id: int | None, job: AnomalyJob, created_by: int | None, trigger: str = "Manual",
) -> AnomalyExecution:
    """Creates the AnomalyExecution row, delegates the real profile ->
    baseline -> detect -> score -> persist pipeline to anomaly/engine.py,
    and translates any failure into a FAILED execution rather than
    letting an exception propagate past a half-written run — mirrors
    reconciliation_service.run_job()'s shape."""
    execution = AnomalyExecution(
        job_id=job.id, organization_id=organization_id, status=AnomalyExecutionStatus.RUNNING,
        trigger=trigger, created_by=created_by,
    )
    db.add(execution)
    await db.flush()  # need execution.id before the engine writes AnomalyColumnResult/AnomalyRecord children

    try:
        await anomaly_engine.run_execution(db, job=job, execution=execution, organization_id=organization_id)
    except (anomaly_engine.AnomalyEngineError, ConnectorError) as exc:
        execution.status = AnomalyExecutionStatus.FAILED
        execution.error_message = str(exc)
        execution.completed_at = datetime.now(timezone.utc)
    except Exception as exc:  # a genuinely unexpected bug shouldn't leave the execution stuck RUNNING forever
        execution.status = AnomalyExecutionStatus.FAILED
        execution.error_message = f"Unexpected error: {exc}"
        execution.completed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(execution)

    # AD11 — standalone-job on-critical incident + alert wiring (the
    # pipeline-embedded path has its own copy in pipeline_job_executor.py's
    # _run_anomaly_detection, added in AD10 — see that function's docstring
    # for why this isn't shared: they read from different config shapes,
    # job.on_critical here vs. the node's own cfg["onCritical"] there).
    if execution.overall_status in (OverallStatus.WARNING, OverallStatus.CRITICAL):
        try:
            on_critical = job.on_critical or {}
            incident_id = None
            if execution.overall_status == OverallStatus.CRITICAL and on_critical.get("create_incident"):
                incident = await incident_service.report_incident(
                    db, organization_id=organization_id, source=IncidentSource.ANOMALY_DETECTION.value,
                    title=f"Critical anomalies — {job.name}",
                    description=f"{execution.anomalies_found} anomalies found across {execution.columns_scanned or 0} column(s) (execution #{execution.id}).",
                    severity=IncidentSeverity.CRITICAL.value,
                )
                incident_id = incident.id
            fired = await alerts.evaluate_rules(db, organization_id=organization_id, ctx={
                # No "job_id" key here on purpose: AlertEvent.job_id is an FK
                # into pipeline_jobs, and this is an AnomalyJob — reusing the
                # same key would silently point the alert at an unrelated
                # (or nonexistent) PipelineJob row.
                "event": "ANOMALY_DETECTED",
                "message": f"Anomaly job '{job.name}' — {execution.anomalies_found} anomalies, overall {execution.overall_status.value}.",
            })
            # Same association monitoring/service.py's on_job_finished() does:
            # any incident created above gets linked onto alerts fired from
            # this same run, so "View Incident" is reachable from the alert.
            if incident_id and fired:
                for event in fired:
                    if event.incident_id is None:
                        event.incident_id = incident_id
                await db.commit()
        except Exception as exc:  # monitoring/incident wiring must never fail a completed run
            import logging
            logging.getLogger(__name__).error("anomaly_service.run_job(%s) post-run hooks failed: %s", execution.id, exc)

    if job.retention_days:
        await purge_expired_executions(db, job)

    return execution


async def purge_expired_executions(db: AsyncSession, job: AnomalyJob) -> int:
    """Deletes AnomalyExecution rows (and their cascaded column results/
    records/feedback) older than job.retention_days — called automatically
    at the end of every run_job(), so retention_days is a real, enforced
    policy rather than a config value nothing reads. A None/0
    retention_days means "keep forever" and this is a no-op. Uses the
    same explicit-cascade ordering as delete_job() (this app's tables
    declare no ORM/DB cascade)."""
    if not job.retention_days:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=job.retention_days)
    expired_ids = (
        await db.execute(
            select(AnomalyExecution.id).where(AnomalyExecution.job_id == job.id, AnomalyExecution.created_at < cutoff)
        )
    ).scalars().all()
    if not expired_ids:
        return 0

    column_result_ids = (
        await db.execute(select(AnomalyColumnResult.id).where(AnomalyColumnResult.execution_id.in_(expired_ids)))
    ).scalars().all()
    if column_result_ids:
        record_ids = (
            await db.execute(select(AnomalyRecord.id).where(AnomalyRecord.column_result_id.in_(column_result_ids)))
        ).scalars().all()
        if record_ids:
            await db.execute(delete(AnomalyFeedback).where(AnomalyFeedback.record_id.in_(record_ids)))
        await db.execute(delete(AnomalyRecord).where(AnomalyRecord.column_result_id.in_(column_result_ids)))
    await db.execute(delete(AnomalyColumnResult).where(AnomalyColumnResult.execution_id.in_(expired_ids)))
    await db.execute(delete(AnomalyExecution).where(AnomalyExecution.id.in_(expired_ids)))
    await db.commit()
    return len(expired_ids)


async def list_executions(db: AsyncSession, job_id: int, organization_id: int | None) -> tuple[list[AnomalyExecution], int]:
    stmt = select(AnomalyExecution).where(AnomalyExecution.job_id == job_id)
    if organization_id is not None:
        stmt = stmt.where(AnomalyExecution.organization_id == organization_id)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return rows, len(rows)


async def get_execution(db: AsyncSession, execution_id: int, organization_id: int | None) -> AnomalyExecution | None:
    stmt = select(AnomalyExecution).where(AnomalyExecution.id == execution_id)
    if organization_id is not None:
        stmt = stmt.where(AnomalyExecution.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_column_results(db: AsyncSession, execution_id: int) -> list[AnomalyColumnResult]:
    """Every selected column — including PASS — see models/anomaly.py's
    module docstring on why this is never filtered down to "only the
    anomalous ones"."""
    result = await db.execute(
        select(AnomalyColumnResult).where(AnomalyColumnResult.execution_id == execution_id).order_by(AnomalyColumnResult.id)
    )
    return list(result.scalars().all())


async def get_column_result(db: AsyncSession, column_result_id: int) -> AnomalyColumnResult | None:
    result = await db.execute(select(AnomalyColumnResult).where(AnomalyColumnResult.id == column_result_id))
    return result.scalar_one_or_none()


async def list_records(
    db: AsyncSession, column_result_id: int, *, severity: str | None = None, anomaly_type: str | None = None,
    limit: int = 100, offset: int = 0,
) -> tuple[list[AnomalyRecord], int]:
    stmt = select(AnomalyRecord).where(AnomalyRecord.column_result_id == column_result_id)
    if severity:
        stmt = stmt.where(AnomalyRecord.severity == severity)
    if anomaly_type:
        stmt = stmt.where(AnomalyRecord.anomaly_type == anomaly_type)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(AnomalyRecord.detected_at.desc()).limit(limit).offset(offset)
    rows = list((await db.execute(stmt)).scalars().all())
    return rows, total


# ── Baseline approval ────────────────────────────────────────────

async def list_baselines(db: AsyncSession, job_id: int, column_name: str | None = None) -> list[AnomalyBaseline]:
    return await anomaly_baseline.list_baselines(db, job_id, column_name)


async def get_baseline(db: AsyncSession, baseline_id: int) -> AnomalyBaseline | None:
    return await anomaly_baseline.get_baseline_by_id(db, baseline_id)


async def approve_baseline(
    db: AsyncSession, baseline: AnomalyBaseline, *, approved_by: int | None, organization_id: int | None,
) -> AnomalyBaseline:
    """Manual approval — see anomaly/baseline.py's approve_baseline for
    why approving an older version doesn't retroactively unapprove a
    newer one."""
    await anomaly_baseline.approve_baseline(db, baseline, approved_by=approved_by)
    await db.commit()
    await db.refresh(baseline)
    await record_event(
        db, organization_id=organization_id, actor_id=approved_by, action="Anomaly Baseline Approved",
        module="Anomaly Detection", resource=f"{baseline.column_name} v{baseline.version}",
        resource_id=str(baseline.id), details=f"job_id={baseline.job_id} source={baseline.source.value}",
    )
    return baseline


# ── Feedback ─────────────────────────────────────────────────────
# Append-only — see models/anomaly.py's AnomalyFeedback docstring. Marking
# a record FALSE_POSITIVE/EXPECTED/IGNORED here is purely informational
# audit trail: it does NOT feed back into detection config or suppress
# future anomalies on this column, matching the spec's explicit "do not
# automatically suppress future anomalies without explicit configuration"
# — a user who wants that has to actually edit the job's detection_config
# (e.g. raise a threshold, exclude a column), a deliberate action, not an
# automatic side effect of feedback.

async def create_feedback(
    db: AsyncSession, *, record: AnomalyRecord, status: FeedbackStatus, comment: str | None,
    created_by: int | None, organization_id: int | None,
) -> AnomalyFeedback:
    row = AnomalyFeedback(record_id=record.id, status=status, comment=comment, created_by=created_by)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await record_event(
        db, organization_id=organization_id, actor_id=created_by, action="Anomaly Feedback Submitted",
        module="Anomaly Detection", resource=f"{record.anomaly_type} record", resource_id=str(record.id),
        details=f"status={status.value}" + (f" comment={comment}" if comment else ""),
    )
    return row


async def list_feedback(db: AsyncSession, record_id: int) -> list[AnomalyFeedback]:
    result = await db.execute(
        select(AnomalyFeedback).where(AnomalyFeedback.record_id == record_id).order_by(AnomalyFeedback.created_at.desc())
    )
    return list(result.scalars().all())


async def get_record(db: AsyncSession, record_id: int) -> AnomalyRecord | None:
    result = await db.execute(select(AnomalyRecord).where(AnomalyRecord.id == record_id))
    return result.scalar_one_or_none()


# ── Reports ──────────────────────────────────────────────────────
# Always a background job — see models/anomaly.py's AnomalyReport
# docstring and anomaly/reporting.py's module docstring: "Do not generate
# files synchronously" (spec). This function only creates the PENDING row
# and fires the real asyncio background task (execution/anomaly_export_
# executor.py); it never blocks on the actual write.

VALID_REPORT_FORMATS = {f.value for f in ReportFormat}


class AnomalyReportError(AnomalyServiceError):
    pass


async def create_report(
    db: AsyncSession, *, execution: AnomalyExecution, report_format: str, created_by: int | None,
) -> AnomalyReport:
    if report_format not in VALID_REPORT_FORMATS:
        raise AnomalyReportError(f"format must be one of {sorted(VALID_REPORT_FORMATS)}.")
    report = AnomalyReport(
        execution_id=execution.id, format=ReportFormat(report_format), status=ReportStatus.PENDING, created_by=created_by,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    anomaly_export_executor.start_report(report.id, execution.organization_id)
    await record_event(
        db, organization_id=execution.organization_id, actor_id=created_by, action="Anomaly Report Requested",
        module="Anomaly Detection", resource=f"execution {execution.id}", resource_id=str(report.id),
        details=f"format={report_format}",
    )
    return report


async def get_report(db: AsyncSession, report_id: int) -> AnomalyReport | None:
    result = await db.execute(select(AnomalyReport).where(AnomalyReport.id == report_id))
    return result.scalar_one_or_none()


async def list_reports(db: AsyncSession, execution_id: int) -> list[AnomalyReport]:
    result = await db.execute(
        select(AnomalyReport).where(AnomalyReport.execution_id == execution_id).order_by(AnomalyReport.created_at.desc())
    )
    return list(result.scalars().all())


def resolve_download_path(report: AnomalyReport) -> Path | None:
    """Path-sandboxed resolution — same defense-in-depth pattern as
    sql_export_service.resolve_download_path: resolve against the real
    base directory and refuse anything that would escape it, even though
    file_path is only ever set by the executor itself (never client
    input)."""
    if not report.file_path:
        return None
    base = Path(ANOMALY_REPORT_BASE_PATH).resolve()
    resolved = (base / report.file_path).resolve()
    if base not in resolved.parents or not resolved.is_file():
        return None
    return resolved


# ── Org-level configuration (AD9) ───────────────────────────────
# Reuses the platform's existing OrganizationSettings row/JSON-blob
# mechanism (services/settings_service.py, one row per org, lazy-created
# on first read) rather than a new table — same DRY choice this codebase
# already makes for Organization.licensed_modules/User.accessible_modules.
# Exposed here (not through /api/settings) so the permission gate stays
# ANOMALY_DETECTION, matching every other anomaly route, instead of
# requiring the separate Settings module permission an anomaly-detection
# editor may not have. Genuinely wired: AnomalyJobEditor.jsx's create-new-
# job path reads this to pre-fill threshold_config/baseline_config/
# retention_days/on_critical — see that file's docstring.

async def get_configuration(db: AsyncSession, organization_id: int) -> dict:
    from app.services import settings_service
    row = await settings_service.get_or_create(db, organization_id)
    return settings_service._effective_values(row)["anomaly_detection"]


async def update_configuration(db: AsyncSession, organization_id: int, patch: dict, *, actor_id: int | None) -> dict:
    from app.services import settings_service
    row = await settings_service.get_or_create(db, organization_id)
    current = settings_service._effective_values(row)
    new_section = settings_service._deep_merge(current["anomaly_detection"], patch)
    new_values = dict(current)
    new_values["anomaly_detection"] = new_section
    row.values = new_values
    row.updated_by = actor_id
    db.add(row)
    if patch:
        await record_event(
            db, organization_id=organization_id, actor_id=actor_id, action="Anomaly Configuration Updated",
            module="Anomaly Detection", resource="Configuration",
            details=f"Updated: {', '.join(patch.keys())}", commit=False,
        )
    await db.commit()
    await db.refresh(row)
    return new_section
