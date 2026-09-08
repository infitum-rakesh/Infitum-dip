"""
ScheduleService — automates execution of existing pipelines.

Integration, not duplication: Schedules never re-implements what a pipeline
does (OCR / AI Extraction / Transformation / Validation / Reconciliation
etc. all already exist as their own modules — Document Intelligence's are
real, the rest are still frontend-only per Pipeline Builder). A Schedule
just references a pipeline by id/name (the same catalogue Pipeline Builder
already lists via pipelineApi.js) and records WHEN it should run.

A real poller (`app.services.scheduler_poller`, started from main.py's
lifespan alongside the billing scheduler) fires schedules automatically
once their `next_run_at` is due — see that module. Before that existed,
`next_run_at` was informational-only and execution happened only via
POST /schedules/{id}/run (still true — "Run Now" and the poller both just
call execute_schedule() below).

Because there's no second execution engine to call into for a generic
Pipeline Builder pipeline yet, execute_schedule() simulates a plausible run
(status/duration/records/logs) for that case, the same pattern used by
document_service.process_document() and connection_service.test_connection()
— EXCEPT for two real dispatch branches:
  - schedule.anomaly_job_id (AD12): a real, already-built execution engine
    (anomaly_service.run_job() -> anomaly/engine.py).
  - a schedule with a DocumentScheduleConfig row (Scheduled Document
    Intelligence Workflow): real file discovery + the real Document
    Intelligence pipeline (document_service.process_document()) — see
    _run_document_schedule_background() below. Unlike every other branch
    here, this one is genuinely long-running (real OCR + AI extraction per
    file), so it is NEVER awaited inline — execute_schedule() fires it via
    asyncio.create_task() and returns the RUNNING run immediately, the same
    fire-and-forget shape document_service.start_processing() uses for a
    single manual scan.
Every other schedule type (still pipeline_id-keyed, no second engine to
call) keeps simulating, completely unchanged.
"""
import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.database import tenant_engine_registry

from app.connectors.base import ConnectorError, FileInfo
from app.core.config import get_settings
from app.models.connection import Connection
from app.models.document import DocumentStatus
from app.models.document_schedule import DocumentScheduleConfig, DocumentSourceRegistry, DocumentSourceStatus
from app.models.schedule import RunStatus, Schedule, ScheduleFrequency, ScheduleRun, ScheduleStatus
from app.models.user import User
from app.services import document_schedule_service as doc_sched_svc
from app.services import document_service
from app.services import review_queue_service as review_svc
from app.services.connection_service import build_connector

# AD12 sentinel (originally ANOMALY_SCHEDULE_PIPELINE_ID, generalized here
# for the Scheduled Document Intelligence Workflow's identical need) —
# mirrors the connection_id=0 "file-backed, not a real connection"
# convention already used by Reconciliation/Anomaly: a schedule with no
# real Pipeline Builder catalogue entry (anomaly-linked OR
# document-source-linked) has pipeline_id set to this instead of leaving
# the column nullable (it stays NOT NULL for every other schedule type,
# and this keeps every existing pipeline_id-keyed list/search/display
# path working unmodified).
NON_PIPELINE_SCHEDULE_ID = "0"
ANOMALY_SCHEDULE_PIPELINE_ID = NON_PIPELINE_SCHEDULE_ID  # kept as an alias — pre-existing name, still used by AD12 call sites

settings = get_settings()

OPEN_RUN_STATUSES = {RunStatus.RUNNING}


class ScheduleValidationError(Exception):
    """Raised for bad frequency/cron/date-time input."""


class ScheduleAlreadyRunningError(Exception):
    """Concurrent-execution guard (spec section 9) — raised by
    execute_schedule() when the schedule's status is already RUNNING. The
    compare-and-swap is the schedule row's own `status` column: only a
    genuinely idle (ACTIVE/PAUSED/FAILED/COMPLETED/DISABLED) schedule can
    start a new run. This is a single-process DB-level lock, not a
    distributed one — correct for this deployment (one backend process);
    see docs/scheduled-document-processing.md for the multi-instance
    caveat, same disclosed-limitation pattern as every other "would need
    Redis for true multi-instance" note already in this codebase."""


async def get_document_config(db: AsyncSession, schedule_id: int) -> DocumentScheduleConfig | None:
    result = await db.execute(select(DocumentScheduleConfig).where(DocumentScheduleConfig.schedule_id == schedule_id))
    return result.scalar_one_or_none()


# ── Timezone / cron helpers ────────────────────────────────────────

def _tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip — every datetime read back from a
    Schedule/ScheduleRun row needs this before any .astimezone() call, or
    Python treats the naive value as local system time instead of the UTC
    it actually is (everything in this module is always stored as UTC)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_start_at(start_date: str, start_time: str, tz_name: str) -> datetime:
    try:
        naive = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ScheduleValidationError("start_date must be YYYY-MM-DD and start_time HH:MM.") from exc
    local = naive.replace(tzinfo=_tz(tz_name))
    return local.astimezone(timezone.utc)


def validate_cron(expr: str) -> None:
    if not croniter.is_valid(expr):
        raise ScheduleValidationError(f"'{expr}' is not a valid cron expression.")


def compute_next_run(schedule: Schedule, *, after: datetime | None = None) -> datetime | None:
    now = after or datetime.now(timezone.utc)
    tz = _tz(schedule.timezone)
    start = _as_utc(schedule.start_at)

    if schedule.frequency == ScheduleFrequency.ONCE:
        return start if start > now else None

    if schedule.frequency == ScheduleFrequency.CUSTOM_CRON:
        if not schedule.cron_expression:
            return None
        base = max(start, now).astimezone(tz)
        it = croniter(schedule.cron_expression, base)
        return it.get_next(datetime).astimezone(timezone.utc)

    if start > now:
        return start  # first fire hasn't happened yet

    local_start = start.astimezone(tz)
    minute, hour, dom = local_start.minute, local_start.hour, local_start.day
    cron_dow = (local_start.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> cron Sun=0..Sat=6

    expr = {
        ScheduleFrequency.HOURLY: f"{minute} * * * *",
        ScheduleFrequency.DAILY: f"{minute} {hour} * * *",
        ScheduleFrequency.WEEKLY: f"{minute} {hour} * * {cron_dow}",
        ScheduleFrequency.MONTHLY: f"{minute} {hour} {dom} * *",
    }.get(schedule.frequency)
    if expr is None:
        return None

    base = now.astimezone(tz)
    return croniter(expr, base).get_next(datetime).astimezone(timezone.utc)


def frequency_display(schedule: Schedule) -> str:
    tz = _tz(schedule.timezone)
    local = _as_utc(schedule.start_at).astimezone(tz)
    time_str = local.strftime("%I:%M %p")
    return {
        ScheduleFrequency.ONCE: f"Once on {local.strftime('%b %d, %Y')} {time_str}",
        ScheduleFrequency.HOURLY: f"Every Hour at :{local.minute:02d}",
        ScheduleFrequency.DAILY: f"Every Day {time_str}",
        ScheduleFrequency.WEEKLY: f"Every {local.strftime('%A')} {time_str}",
        ScheduleFrequency.MONTHLY: f"Monthly on Day {local.day} {time_str}",
        ScheduleFrequency.CUSTOM_CRON: schedule.cron_expression or "Custom",
    }.get(schedule.frequency, schedule.frequency.value)


# ── CRUD ─────────────────────────────────────────────────────────

def _apply_frequency(schedule: Schedule, *, frequency: str, start_date: str, start_time: str, timezone_name: str, cron_expression: str | None) -> None:
    try:
        freq = ScheduleFrequency(frequency)
    except ValueError as exc:
        raise ScheduleValidationError(f"Unknown frequency '{frequency}'.") from exc
    if freq == ScheduleFrequency.CUSTOM_CRON:
        if not cron_expression:
            raise ScheduleValidationError("cron_expression is required when frequency is CUSTOM_CRON.")
        validate_cron(cron_expression)
    schedule.frequency = freq
    schedule.timezone = timezone_name
    schedule.start_at = parse_start_at(start_date, start_time, timezone_name)
    schedule.cron_expression = cron_expression if freq == ScheduleFrequency.CUSTOM_CRON else None


async def _validate_and_apply_document_config(db: AsyncSession, schedule: Schedule, config_in) -> None:
    """Spec section 54 — server-side validation (never trust the frontend
    alone; DocumentScheduleConfigIn's pydantic validators already cover
    field-level checks like thresholds 0-100 — this covers the
    cross-cutting ones that need a DB lookup: the connection must be a
    real, STORAGE-category connection this org actually owns)."""
    conn = await db.get(Connection, config_in.source_connection_id)
    if conn is None or (schedule.organization_id is not None and conn.organization_id != schedule.organization_id):
        raise ScheduleValidationError(f"Connection {config_in.source_connection_id} was not found.")
    # PAY18 — a DATABASE-kind schedule's source_connection_id must
    # actually be a DATABASE-category connection (Postgres/MySQL/SQL
    # Server/...): picking a STORAGE/API/etc connection by mistake would
    # otherwise fail loudly but late, deep inside a background schedule
    # run's preview_sql() call instead of at save time — same principle
    # as the ERP-connection-type check below.
    if config_in.source_kind == "DATABASE" and conn.category.value != "DATABASE":
        raise ScheduleValidationError(f"Connection {config_in.source_connection_id} is not a database connection.")
    # PAY20 — same cross-cutting check for an API-kind schedule.
    if config_in.source_kind == "API" and conn.category.value != "API":
        raise ScheduleValidationError(f"Connection {config_in.source_connection_id} is not an API connection.")
    # PAY21 — same cross-cutting check for an ERP-kind schedule: must
    # actually be the one ERP connector type this build ships (same
    # connection_type check INV11's invoice_auto_post ERP connection
    # already applies below).
    if config_in.source_kind == "ERP" and conn.connection_type.value != "ORACLE_ERP_CLOUD":
        raise ScheduleValidationError(f"Connection {config_in.source_connection_id} is not an ERP connection.")
    if config_in.archive_after_success and not config_in.archive_path:
        raise ScheduleValidationError("Archive path is required when 'Archive after success' is enabled.")

    # INV11 — same cross-cutting DB-lookup validation pattern as
    # source_connection_id above: the pydantic-level check only proved an
    # id was present, not that it's real, owned by this org, and actually
    # an ERP connection (posting through a Postgres/whatever connection id
    # by mistake would otherwise fail loudly but late, deep inside a
    # background schedule run instead of at save time).
    if config_in.invoice_auto_post and config_in.invoice_erp_connection_id:
        erp_conn = await db.get(Connection, config_in.invoice_erp_connection_id)
        if erp_conn is None or (schedule.organization_id is not None and erp_conn.organization_id != schedule.organization_id):
            raise ScheduleValidationError(f"ERP connection {config_in.invoice_erp_connection_id} was not found.")
        if erp_conn.connection_type.value != "ORACLE_ERP_CLOUD":
            raise ScheduleValidationError(f"Connection {config_in.invoice_erp_connection_id} is not an ERP connection.")

    existing = await get_document_config(db, schedule.id)
    if existing is None:
        existing = DocumentScheduleConfig(schedule_id=schedule.id)
        db.add(existing)
    for field, value in config_in.model_dump().items():
        setattr(existing, field, value)


async def create_schedule(db: AsyncSession, *, organization_id: int | None, owner_id: int, body) -> Schedule:
    pipeline_id, pipeline_name = body.pipeline_id, body.pipeline_name
    anomaly_job_id = getattr(body, "anomaly_job_id", None)
    document_config = getattr(body, "document_config", None)
    if anomaly_job_id is not None and document_config is not None:
        raise ScheduleValidationError("A schedule can't be linked to both an Anomaly Job and a document source — pick one.")

    if anomaly_job_id is not None:
        from app.models.anomaly import AnomalyJob  # local import: avoid a module-level anomaly<->schedule import cycle
        job = await db.get(AnomalyJob, anomaly_job_id)
        if job is None or (organization_id is not None and job.organization_id != organization_id):
            raise ScheduleValidationError(f"Anomaly job {anomaly_job_id} was not found.")
        # Overridden, not merely defaulted — see NON_PIPELINE_SCHEDULE_ID's
        # docstring: every anomaly-linked schedule uses this sentinel regardless
        # of whatever pipeline_id/pipeline_name the caller happened to send.
        pipeline_id, pipeline_name = NON_PIPELINE_SCHEDULE_ID, job.name
    elif document_config is not None:
        pipeline_id, pipeline_name = NON_PIPELINE_SCHEDULE_ID, f"Document Source: {document_config.source_path}"
    elif not pipeline_id:
        raise ScheduleValidationError("pipeline_id is required for a Pipeline Builder schedule (or link an Anomaly Job / document source instead).")

    schedule = Schedule(
        organization_id=organization_id, owner_id=owner_id, name=body.name,
        pipeline_id=pipeline_id, pipeline_name=pipeline_name, anomaly_job_id=anomaly_job_id,
        environment=body.environment, status=ScheduleStatus.ACTIVE,
        retry_count=body.retry_count, retry_interval_seconds=body.retry_interval_seconds,
        timeout_seconds=body.timeout_seconds, concurrency=body.concurrency,
        notify_on_failure=body.notify_on_failure, notify_on_success=body.notify_on_success,
        notification_emails=body.notification_emails,
    )
    _apply_frequency(schedule, frequency=body.frequency, start_date=body.start_date, start_time=body.start_time,
                      timezone_name=body.timezone, cron_expression=body.cron_expression)
    schedule.next_run_at = compute_next_run(schedule)
    db.add(schedule)
    await db.flush()  # assigns schedule.id, needed by _validate_and_apply_document_config below, before the final commit

    if document_config is not None:
        await _validate_and_apply_document_config(db, schedule, document_config)

    await db.commit()
    await db.refresh(schedule)
    return schedule


async def update_schedule(db: AsyncSession, schedule: Schedule, body) -> Schedule:
    for field in ("name", "pipeline_id", "pipeline_name", "notification_emails"):
        value = getattr(body, field)
        if value is not None:
            setattr(schedule, field, value)
    if body.environment is not None:
        schedule.environment = body.environment
    for field in ("retry_count", "retry_interval_seconds", "timeout_seconds", "concurrency", "notify_on_failure", "notify_on_success"):
        value = getattr(body, field)
        if value is not None:
            setattr(schedule, field, value)

    document_config = getattr(body, "document_config", None)
    if document_config is not None:
        await _validate_and_apply_document_config(db, schedule, document_config)

    if body.frequency is not None or body.start_date is not None or body.start_time is not None or body.timezone is not None:
        _apply_frequency(
            schedule,
            frequency=body.frequency or schedule.frequency.value,
            start_date=body.start_date or _as_utc(schedule.start_at).astimezone(_tz(schedule.timezone)).strftime("%Y-%m-%d"),
            start_time=body.start_time or _as_utc(schedule.start_at).astimezone(_tz(schedule.timezone)).strftime("%H:%M"),
            timezone_name=body.timezone or schedule.timezone,
            cron_expression=body.cron_expression if body.cron_expression is not None else schedule.cron_expression,
        )
        if schedule.status not in (ScheduleStatus.PAUSED, ScheduleStatus.DISABLED):
            schedule.next_run_at = compute_next_run(schedule)

    await db.commit()
    await db.refresh(schedule)
    return schedule


async def delete_schedule(db: AsyncSession, schedule: Schedule) -> None:
    await db.execute(DocumentSourceRegistry.__table__.update().where(DocumentSourceRegistry.schedule_id == schedule.id).values(schedule_id=None))
    await db.execute(DocumentScheduleConfig.__table__.delete().where(DocumentScheduleConfig.schedule_id == schedule.id))
    await db.execute(ScheduleRun.__table__.delete().where(ScheduleRun.schedule_id == schedule.id))
    await db.delete(schedule)
    await db.commit()


async def clone_schedule(db: AsyncSession, schedule: Schedule, owner_id: int) -> Schedule:
    clone = Schedule(
        organization_id=schedule.organization_id, owner_id=owner_id, name=f"{schedule.name} (Copy)",
        pipeline_id=schedule.pipeline_id, pipeline_name=schedule.pipeline_name,
        environment=schedule.environment, timezone=schedule.timezone, frequency=schedule.frequency,
        start_at=schedule.start_at, cron_expression=schedule.cron_expression,
        status=ScheduleStatus.PAUSED,  # cloned schedules start paused so they don't silently double-run
        retry_count=schedule.retry_count, retry_interval_seconds=schedule.retry_interval_seconds,
        timeout_seconds=schedule.timeout_seconds, concurrency=schedule.concurrency,
        notify_on_failure=schedule.notify_on_failure, notify_on_success=schedule.notify_on_success,
        notification_emails=schedule.notification_emails,
    )
    db.add(clone)
    await db.commit()
    await db.refresh(clone)
    return clone


async def pause_schedule(db: AsyncSession, schedule: Schedule) -> Schedule:
    schedule.status = ScheduleStatus.PAUSED
    schedule.next_run_at = None
    await db.commit()
    await db.refresh(schedule)
    return schedule


async def resume_schedule(db: AsyncSession, schedule: Schedule) -> Schedule:
    schedule.status = ScheduleStatus.ACTIVE
    schedule.next_run_at = compute_next_run(schedule)
    await db.commit()
    await db.refresh(schedule)
    return schedule


# ── Query / list ─────────────────────────────────────────────────

async def get_schedule(db: AsyncSession, schedule_id: int, organization_id: int | None) -> Schedule | None:
    stmt = select(Schedule).where(Schedule.id == schedule_id)
    if organization_id is not None:
        stmt = stmt.where(Schedule.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def owner_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


async def list_schedules(
    db: AsyncSession, *, organization_id: int | None, search: str = "",
    status_filter: str | None = None, environment: str | None = None, frequency: str | None = None,
    anomaly_job_id: int | None = None,
    page: int = 1, page_size: int = 20, sort_by: str = "next_run_at", sort_dir: str = "asc",
) -> tuple[list[Schedule], int, dict[int, str]]:
    stmt = select(Schedule)
    if organization_id is not None:
        stmt = stmt.where(Schedule.organization_id == organization_id)
    if status_filter:
        stmt = stmt.where(Schedule.status == status_filter)
    if environment:
        stmt = stmt.where(Schedule.environment == environment)
    if frequency:
        stmt = stmt.where(Schedule.frequency == frequency)
    if anomaly_job_id is not None:
        stmt = stmt.where(Schedule.anomaly_job_id == anomaly_job_id)

    result = await db.execute(stmt)
    schedules = list(result.scalars().all())

    owner_ids = {s.owner_id for s in schedules if s.owner_id}
    name_map = await owner_name_map(db, owner_ids)

    if search:
        needle = search.lower()
        def matches(s: Schedule) -> bool:
            haystacks = [s.name, s.pipeline_name, s.environment.value, s.status.value, s.frequency.value, name_map.get(s.owner_id, "")]
            return any(needle in h.lower() for h in haystacks if h)
        schedules = [s for s in schedules if matches(s)]

    reverse = sort_dir == "desc"

    def sort_key(s: Schedule):
        val = getattr(s, sort_by, None)
        if val is None:
            return (1, datetime.min.replace(tzinfo=timezone.utc))  # push nulls last regardless of direction
        return (0, val)
    schedules.sort(key=sort_key, reverse=reverse)

    total = len(schedules)
    start = (page - 1) * page_size
    return schedules[start:start + page_size], total, name_map


async def get_stats(db: AsyncSession, organization_id: int | None) -> dict:
    stmt = select(Schedule)
    if organization_id is not None:
        stmt = stmt.where(Schedule.organization_id == organization_id)
    result = await db.execute(stmt)
    schedules = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    today = now.date()

    ids = [s.id for s in schedules]
    runs_today: list[ScheduleRun] = []
    if ids:
        r = await db.execute(select(ScheduleRun).where(ScheduleRun.schedule_id.in_(ids)))
        for run in r.scalars().all():
            started = _as_utc(run.started_at)
            if started.date() == today:
                runs_today.append(run)

    upcoming = sorted(
        (s for s in schedules if s.next_run_at and s.status == ScheduleStatus.ACTIVE),
        key=lambda s: s.next_run_at,
    )
    next_schedule = upcoming[0] if upcoming else None

    return {
        "active": sum(1 for s in schedules if s.status == ScheduleStatus.ACTIVE),
        "paused": sum(1 for s in schedules if s.status == ScheduleStatus.PAUSED),
        "running_today": sum(1 for r in runs_today if r.status == RunStatus.RUNNING),
        "failed_today": sum(1 for r in runs_today if r.status == RunStatus.FAILED),
        "next_run_at": _as_utc(next_schedule.next_run_at) if next_schedule else None,
        "next_run_schedule_name": next_schedule.name if next_schedule else None,
    }


def _as_utc_opt(dt: datetime | None) -> datetime | None:
    return _as_utc(dt) if dt is not None else None


def to_out(schedule: Schedule, owner_name: str | None, document_config: DocumentScheduleConfig | None = None) -> dict:
    return {
        "id": schedule.id, "name": schedule.name, "pipeline_id": schedule.pipeline_id, "pipeline_name": schedule.pipeline_name,
        "anomaly_job_id": schedule.anomaly_job_id,
        "document_config": _document_config_to_out(document_config) if document_config is not None else None,
        "environment": schedule.environment.value, "timezone": schedule.timezone, "frequency": schedule.frequency.value,
        "frequency_display": frequency_display(schedule), "start_at": _as_utc(schedule.start_at), "cron_expression": schedule.cron_expression,
        "status": schedule.status.value, "next_run_at": _as_utc_opt(schedule.next_run_at), "last_run_at": _as_utc_opt(schedule.last_run_at),
        "last_run_status": schedule.last_run_status,
        "retry_count": schedule.retry_count, "retry_interval_seconds": schedule.retry_interval_seconds,
        "timeout_seconds": schedule.timeout_seconds, "concurrency": schedule.concurrency,
        "notify_on_failure": schedule.notify_on_failure, "notify_on_success": schedule.notify_on_success,
        "notification_emails": schedule.notification_emails,
        "owner_id": schedule.owner_id, "owner_name": owner_name,
        "created_at": _as_utc(schedule.created_at), "updated_at": _as_utc(schedule.updated_at),
    }


def _document_config_to_out(config: DocumentScheduleConfig) -> dict:
    return {
        "source_connection_id": config.source_connection_id, "source_path": config.source_path,
        "file_pattern": config.file_pattern, "recursive_scan": config.recursive_scan,
        "batch_size": config.batch_size, "max_concurrency": config.max_concurrency,
        "confidence_threshold": config.confidence_threshold, "accuracy_threshold": config.accuracy_threshold,
        "archive_after_success": config.archive_after_success, "archive_path": config.archive_path,
        "move_failed_to_review_path": config.move_failed_to_review_path,
        "duplicate_content_behavior": config.duplicate_content_behavior, "max_file_size_mb": config.max_file_size_mb,
        "document_type": config.document_type,
        "invoice_auto_post": config.invoice_auto_post, "invoice_erp_connection_id": config.invoice_erp_connection_id,
    }


def run_to_out(run: ScheduleRun) -> dict:
    return {
        "id": run.id, "schedule_id": run.schedule_id, "status": run.status.value, "trigger": run.trigger,
        "started_at": _as_utc(run.started_at), "ended_at": _as_utc_opt(run.ended_at),
        "duration_seconds": run.duration_seconds, "records_processed": run.records_processed,
        "records_failed": run.records_failed, "error_message": run.error_message,
        "cancel_requested": run.cancel_requested, "document_stats": run.document_stats,
        "logs": run.logs or [],  # already ISO strings with UTC offset baked in at write time — see _log()
    }


# ── Simulated execution ────────────────────────────────────────────
# See module docstring: this stands in for calling a real scheduler/
# execution engine until one exists. Swap the body of the retry loop
# below for a real dispatch-to-pipeline call and nothing above/below it
# needs to change.

_STAGE_MESSAGES = [
    "Connecting to pipeline source...",
    "Processing via '{pipeline}'...",
    "Writing results to target...",
]
_FAILURE_MESSAGES = [
    "Pipeline timed out before completion.",
    "Source connection was unreachable.",
    "Validation step rejected the batch.",
    "Target write failed — check destination permissions.",
    "Upstream dependency did not produce data in time.",
]


def _log(ts: datetime, level: str, message: str) -> dict:
    return {"timestamp": ts.isoformat(), "level": level, "message": message}


async def _execute_anomaly_schedule(db: AsyncSession, schedule: Schedule, run: ScheduleRun, now: datetime) -> None:
    """AD12 — real dispatch branch: mutates `run` in place with the actual
    result of running schedule.anomaly_job_id via anomaly_service.run_job()
    (the same real engine Manual/Pipeline-triggered runs use — see that
    function's own docstring). Never raises: a bad/missing job or an
    engine-level failure is captured into run.status=FAILED +
    run.error_message instead, exactly like the simulated path's own
    failure branch, so a schedule keeps being pollable/retriable rather
    than the whole request 500ing."""
    from app.models.anomaly import AnomalyExecutionStatus, AnomalyJob, OverallStatus
    from app.services import anomaly_service

    logs: list[dict] = [_log(now, "INFO", f"Dispatching Anomaly Job #{schedule.anomaly_job_id}…")]
    try:
        job = await db.get(AnomalyJob, schedule.anomaly_job_id)
        if job is None or (schedule.organization_id is not None and job.organization_id != schedule.organization_id):
            raise ValueError(f"Anomaly job {schedule.anomaly_job_id} no longer exists or no longer belongs to this organization.")

        execution = await anomaly_service.run_job(
            db, organization_id=schedule.organization_id, job=job, created_by=schedule.owner_id, trigger="Schedule",
        )
        # execution.completed_at just round-tripped through a db.refresh()
        # inside run_job() — SQLite drops tzinfo on that round trip (see
        # this module's own _as_utc() docstring above), so it comes back
        # naive even though it was written as UTC-aware. `now` below is
        # always aware, so every datetime this branch computes from must
        # be normalized the same way or the subtraction raises.
        ended_at = _as_utc(execution.completed_at) if execution.completed_at else datetime.now(timezone.utc)

        if execution.status == AnomalyExecutionStatus.FAILED:
            run.status = RunStatus.FAILED
            run.error_message = execution.error_message or "Anomaly execution failed."
            logs.append(_log(ended_at, "ERROR", run.error_message))
        else:
            is_concerning = execution.overall_status in (OverallStatus.CRITICAL, OverallStatus.WARNING)
            run.status = RunStatus.WARNING if is_concerning else RunStatus.SUCCESS
            run.error_message = None
            overall = execution.overall_status.value if execution.overall_status else "UNKNOWN"
            logs.append(_log(
                ended_at, "INFO",
                f"Anomaly Job '{job.name}' — {execution.anomalies_found} anomalies across {execution.columns_scanned or 0} column(s), overall {overall}. Execution #{execution.id}.",
            ))

        run.ended_at = ended_at
        run.duration_seconds = round((ended_at - now).total_seconds(), 1)
        run.records_processed = execution.records_scanned or 0
        run.records_failed = execution.anomalies_found or 0  # repurposed for this schedule type: "problem records" found, not a load/transfer failure count
    except Exception as exc:
        ended_at = datetime.now(timezone.utc)
        run.status = RunStatus.FAILED
        run.ended_at = ended_at
        run.duration_seconds = round((ended_at - now).total_seconds(), 1)
        run.records_processed = 0
        run.records_failed = 0
        run.error_message = str(exc)
        logs.append(_log(ended_at, "ERROR", str(exc)))

    run.logs = logs


def _finalize_schedule(schedule: Schedule, run: ScheduleRun) -> None:
    """Shared by all three execute_schedule() dispatch branches (simulated
    pipeline / real anomaly / real document-source) — same
    ONCE-vs-recurring, FAILED-vs-not status/next_run_at logic every branch
    needs, now written once instead of copy-pasted three times."""
    schedule.last_run_at = run.ended_at
    schedule.last_run_status = run.status.value
    failed = run.status == RunStatus.FAILED
    if schedule.frequency == ScheduleFrequency.ONCE:
        schedule.status = ScheduleStatus.FAILED if failed else ScheduleStatus.COMPLETED
        schedule.next_run_at = None
    else:
        schedule.status = ScheduleStatus.FAILED if failed else ScheduleStatus.ACTIVE
        schedule.next_run_at = compute_next_run(schedule, after=run.ended_at)


async def execute_schedule(db: AsyncSession, schedule: Schedule, *, trigger: str) -> ScheduleRun:
    if schedule.status == ScheduleStatus.RUNNING:
        raise ScheduleAlreadyRunningError(f"Schedule '{schedule.name}' is already running — wait for it to finish or check its current run's progress.")

    now = datetime.now(timezone.utc)
    run = ScheduleRun(schedule_id=schedule.id, status=RunStatus.RUNNING, trigger=trigger, started_at=now, logs=[])
    db.add(run)
    schedule.status = ScheduleStatus.RUNNING
    await db.commit()
    await db.refresh(run)

    if schedule.anomaly_job_id is not None:
        await _execute_anomaly_schedule(db, schedule, run, now)
        _finalize_schedule(schedule, run)
        await db.commit()
        await db.refresh(run)
        await db.refresh(schedule)
        return run

    doc_config = await get_document_config(db, schedule.id)
    if doc_config is not None:
        # Fire-and-forget (spec section 10/14/91) — real OCR + AI
        # extraction per file can take well over a minute (see
        # document_service.py's own note on this exact cost), so unlike
        # every branch above/below, this one is NEVER awaited inline in
        # the HTTP request that triggered it. The caller gets this
        # RUNNING run back immediately; the frontend polls
        # GET /schedules/{id}/runs/{run_id} for live progress — same
        # fire-and-forget shape document_service.start_processing() uses.
        #
        # db.refresh(schedule) here matters even though nothing above
        # looks like it needs it: this session (get_tenant_db) uses
        # SQLAlchemy's default expire_on_commit=True, so the `commit()`
        # a few lines up (setting status=RUNNING) expired every attribute
        # on `schedule` — the caller (api/schedules.py's run_now) reads
        # schedule.updated_at etc. right after this returns, and a bare
        # attribute access on an expired ORM object outside an awaited
        # context raises a sync-in-async MissingGreenlet error instead of
        # transparently reloading. The other two dispatch branches above
        # never hit this because they already end in their own
        # `await db.refresh(schedule)` as part of finalizing a completed
        # run — found live running a real document schedule.
        await db.refresh(schedule)
        asyncio.create_task(_run_document_schedule_background(schedule.id, run.id, schedule.organization_id))
        return run

    rnd = random.Random()
    logs: list[dict] = []
    cursor = now
    records = rnd.randint(500, 50_000)
    max_attempts = 1 + max(0, schedule.retry_count)
    success = False
    error_message: str | None = None
    failed_records = 0

    for attempt in range(1, max_attempts + 1):
        logs.append(_log(cursor, "INFO", f"Attempt {attempt}/{max_attempts} started."))
        for template in _STAGE_MESSAGES:
            cursor += timedelta(seconds=rnd.uniform(2, 25))
            logs.append(_log(cursor, "INFO", template.format(pipeline=schedule.pipeline_name)))

        success = rnd.random() < settings.schedule_run_success_rate
        cursor += timedelta(seconds=rnd.uniform(1, 10))
        if success:
            logs.append(_log(cursor, "INFO", f"Attempt {attempt} succeeded."))
            break
        error_message = rnd.choice(_FAILURE_MESSAGES)
        failed_records = rnd.randint(1, max(1, records // 8))
        logs.append(_log(cursor, "ERROR", f"Attempt {attempt} failed: {error_message}"))
        if attempt < max_attempts:
            cursor += timedelta(seconds=schedule.retry_interval_seconds)
            logs.append(_log(cursor, "INFO", f"Retrying in {schedule.retry_interval_seconds}s…"))

    if success:
        logs.append(_log(cursor, "INFO", "Job completed successfully."))
        error_message = None
        failed_records = 0
    else:
        logs.append(_log(cursor, "ERROR", "Job failed after exhausting retries." if max_attempts > 1 else "Job failed."))

    ended_at = cursor
    run.status = RunStatus.SUCCESS if success else RunStatus.FAILED
    run.ended_at = ended_at
    run.duration_seconds = round((ended_at - now).total_seconds(), 1)
    run.records_processed = records
    run.records_failed = failed_records
    run.error_message = error_message
    run.logs = logs

    _finalize_schedule(schedule, run)

    await db.commit()
    await db.refresh(run)
    await db.refresh(schedule)
    return run


async def get_runs(db: AsyncSession, schedule_id: int, *, page: int = 1, page_size: int = 20) -> tuple[list[ScheduleRun], int]:
    result = await db.execute(select(ScheduleRun).where(ScheduleRun.schedule_id == schedule_id))
    runs = sorted(result.scalars().all(), key=lambda r: r.started_at, reverse=True)
    total = len(runs)
    start = (page - 1) * page_size
    return runs[start:start + page_size], total


async def request_cancel(db: AsyncSession, run: ScheduleRun) -> ScheduleRun:
    """Spec section 81 — cooperative cancellation. Only meaningful for a
    RUNNING document-schedule run (the only kind that runs long enough to
    cancel); harmless no-op flag-set on any other run type/status."""
    run.cancel_requested = True
    await db.commit()
    await db.refresh(run)
    return run


# ── Real document-source dispatch (Scheduled Document Intelligence
# Workflow) ──────────────────────────────────────────────────────────
# Orchestration only — the actual reusable work lives in
# document_schedule_service.py (discovery/idempotency) and
# document_service.py (the real OCR/AI/Review-Queue pipeline, the exact
# same one manual scan and bulk upload call). Nothing below reimplements
# either.

_STATS_DEFAULTS = {
    "discovered": 0, "queued": 0, "processed": 0, "success": 0, "review": 0,
    "failed": 0, "skipped": 0, "unsupported": 0,
    "current_file": None, "current_stage": None,
    # PAY17 — approval-outcome counters for document_type="INVOICE"
    # schedules (INV11/PAY9's own scheduler integration). Zero for every
    # non-invoice schedule (the classifier below is only ever called
    # when config.document_type == "INVOICE", and these keys just stay
    # at their default otherwise) — never fabricated for a run that
    # never touched the approval engine.
    "auto_approved": 0, "workflows_created": 0, "already_in_workflow": 0,
    "rule_conflicts": 0, "approver_not_found": 0,
}


def _stats_defaults() -> dict:
    return dict(_STATS_DEFAULTS)


async def _classify_invoice_approval_outcome(db: AsyncSession, document_id: int) -> dict:
    """PAY17 — after process_document() has already run one INVOICE-typed
    document all the way through materialize/validate/decide (and, via
    decide()'s own PAY9 wiring, possibly create_workflow()), classifies
    what happened to it by reading its real, already-persisted state —
    never a special-cased return value threaded up through
    process_document(), which stays a generic "process this document"
    call with no approval-specific knowledge of its own (the spec's own
    "the scheduler is a trigger, not the owner of approval business
    logic" principle — this classifier reads outcomes, it doesn't decide
    them). Returns a dict of stat-key -> increment (usually one key), or
    {} when this document never became an invoice at all (extraction/
    materialization failed before that point).

    A document schedule run materializes a brand-new ApInvoice per file
    (materialize_invoice() is idempotent by document_id — a fresh
    document_id here always means a fresh invoice row), so every
    ApprovalWorkflowAuditLog row tied to that invoice's id was
    necessarily written during THIS file's own processing — no
    run-window timestamp filtering needed to avoid attributing an
    unrelated invoice's history here, even though multiple files process
    concurrently within one run."""
    from app.models.approval.workflow import ApprovalWorkflowAuditEventType, ApprovalWorkflowAuditLog
    from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceDecision, ApInvoiceStatus

    invoice = (await db.execute(select(ApInvoice).where(ApInvoice.document_id == document_id))).scalar_one_or_none()
    if invoice is None:
        return {}

    if any((r or {}).get("category") == "APPROVAL_RULE_CONFLICT" for r in (invoice.decision_reasons or [])):
        return {"rule_conflicts": 1}

    events = (await db.execute(
        select(ApprovalWorkflowAuditLog.event_type).where(ApprovalWorkflowAuditLog.invoice_id == invoice.id)
    )).scalars().all()

    increments: dict = {}
    if ApprovalWorkflowAuditEventType.CREATED in events:
        increments["workflows_created"] = 1
    elif ApprovalWorkflowAuditEventType.DUPLICATE_SUPPRESSED in events:
        increments["already_in_workflow"] = 1
    elif invoice.final_decision == ApInvoiceDecision.STRAIGHT_THROUGH and invoice.status == ApInvoiceStatus.READY_TO_POST:
        increments["auto_approved"] = 1

    approver_not_found = sum(1 for e in events if e == ApprovalWorkflowAuditEventType.RESOLUTION_FAILED)
    if approver_not_found:
        increments["approver_not_found"] = approver_not_found
    return increments


async def _download_with_retry(connection: Connection, file_path: str, *, retry_count: int, retry_interval: int) -> bytes:
    """Spec section 27 — exponential backoff (5s/15s/45s-style: each
    attempt's wait is retry_interval * 3^(attempt-1), capped at 5 minutes)
    around exactly the transient failure classes the spec names (SFTP/
    cloud timeout, network failure) — anything the connector itself raises
    as ConnectorError. A file that's simply not there / permission denied
    surfaces the same way today (connectors don't yet distinguish "retry
    this" from "don't bother"), so max_attempts=1 (retry_count=0, this
    schedule's default) keeps that case failing fast exactly like before;
    a schedule that explicitly configures retries accepts the tradeoff of
    a few wasted attempts against a permanently-broken source in exchange
    for genuinely recovering from a blip."""
    connector = build_connector(connection)
    max_attempts = 1 + max(0, retry_count)
    attempt = 0
    while True:
        attempt += 1
        try:
            def _read() -> bytes:
                stream = connector.open_read_stream(file_path)
                try:
                    return stream.read()
                finally:
                    stream.close()
            return await asyncio.to_thread(_read)
        except ConnectorError:
            if attempt >= max_attempts:
                raise
            delay = min(retry_interval * (3 ** (attempt - 1)), 300)
            await asyncio.sleep(delay)


async def _process_one_file(
    *, organization_id: int | None, schedule_id: int, connection: Connection, config: DocumentScheduleConfig,
    file_info, registry_id: int, run_id: int, stats: dict, stats_lock: asyncio.Lock, sessionmaker,
    schedule_retry_count: int, schedule_retry_interval: int,
) -> str:
    """One file, start to finish, in its own DB session (AsyncSession isn't
    safe to share across concurrently-running coroutines — every
    concurrent file gets its own session, the same principle
    document_service._process_in_background() already follows for a single
    background document). Returns the outcome bucket name ("success" /
    "review" / "failed") for the caller's own bookkeeping if it wants it;
    stats is already updated by the time this returns."""
    outcome = "failed"
    approval_increments: dict = {}
    async with sessionmaker() as db:
        registry = await db.get(DocumentSourceRegistry, registry_id)
        try:
            await doc_sched_svc.transition(db, registry, DocumentSourceStatus.PROCESSING)
            async with stats_lock:
                stats["current_file"] = registry.file_name
                stats["current_stage"] = "DOWNLOADING"
                await _persist_stats(db, run_id, stats)

            content = await _download_with_retry(
                connection, file_info.path, retry_count=schedule_retry_count, retry_interval=schedule_retry_interval,
            )

            doc = await document_service.save_from_source(
                db, content=content, filename=file_info.name, organization_id=organization_id,
                source_connection_id=connection.id, source_path=config.source_path, source_schedule_id=schedule_id,
            )
            registry.document_id = doc.id
            await db.commit()

            async with stats_lock:
                stats["current_stage"] = "OCR / AI EXTRACTION"
                await _persist_stats(db, run_id, stats)

            doc = await document_service.process_document(
                db, doc, confidence_threshold=config.confidence_threshold, accuracy_threshold=config.accuracy_threshold,
                document_type=config.document_type,
                invoice_auto_post=config.invoice_auto_post, invoice_erp_connection_id=config.invoice_erp_connection_id,
            )

            # PAY17 — read the real approval outcome for THIS document's
            # invoice (if any) off already-persisted state, independent
            # of the doc.status/outcome bucket below (an invoice can end
            # up needing approval, or hit a rule conflict, regardless of
            # whether the extraction itself needed Review Queue).
            if config.document_type == "INVOICE":
                approval_increments = await _classify_invoice_approval_outcome(db, doc.id)

            if doc.status == DocumentStatus.NEEDS_REVIEW:
                await review_svc.create_from_document(db, doc)
                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.REVIEW_REQUIRED)
                outcome = "review"
            elif doc.status == DocumentStatus.FAILED:
                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.FAILED, error=doc.error_message)
                outcome = "failed"
            else:
                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.PROCESSED)
                outcome = "success"
                # File archiving (spec section 32/33) — only AFTER a real
                # commit of the PROCESSED status above, and a failure here
                # never re-flags the document for reprocessing: it stays
                # PROCESSED, only the archive note says the move didn't
                # happen, exactly as the spec requires.
                if config.archive_after_success and config.archive_path:
                    try:
                        await asyncio.to_thread(build_connector(connection).move_file, file_info.path, config.archive_path)
                    except ConnectorError as exc:
                        registry.last_error = f"Processed successfully, but archive move failed: {exc}"
                        await db.commit()
        except Exception as exc:  # noqa: BLE001 - a single file's failure must never sink the whole batch (spec section 28)
            # Written directly (not via transition()) — a truly
            # unanticipated exception can happen from ANY status
            # (download, save, or process_document() itself all throw
            # from a different current status), and every one of them
            # legitimately ends at FAILED; transition()'s explicit state
            # machine is for the MODELED paths above, not this catch-all.
            registry.processing_attempts += 1
            registry.status = DocumentSourceStatus.FAILED
            registry.last_error = str(exc)[:2000]
            await db.commit()
            outcome = "failed"

        async with stats_lock:
            stats["processed"] += 1
            stats[outcome] = stats.get(outcome, 0) + 1
            for key, amount in approval_increments.items():
                stats[key] = stats.get(key, 0) + amount
            stats["current_file"] = None
            stats["current_stage"] = None
            await _persist_stats(db, run_id, stats)

    return outcome


async def _process_one_record(
    *, organization_id: int | None, schedule_id: int, connection: Connection, config: DocumentScheduleConfig,
    row: dict, record_key: str, registry_id: int, run_id: int, stats: dict, stats_lock: asyncio.Lock, sessionmaker,
) -> str:
    """PAY18/PAY20 — DATABASE- and API-kind document schedules' shared
    counterpart to _process_one_file() above: one structured record
    (a query result row, or a decoded API response record), start to
    finish, in its own DB session. Nothing in this function is aware of
    which of the two it's handling — that distinction lives entirely
    upstream in discovery (discover_database_records() vs
    discover_api_records()) and in record_identity(); by the time a row
    reaches here it's just a dict. There is no download/archive step (no
    file exists to download or move) and no OCR/AI Extraction stage (see
    document_service.build_fields_from_record()/process_document()'s
    preextracted_fields branch) — everything else (registry transitions,
    Review Queue routing, PAY17 approval-outcome classification, stats
    bookkeeping) is identical to the file path, on purpose: a
    non-file-sourced invoice should behave exactly like a file-sourced
    one from the point its Document row exists onward."""
    outcome = "failed"
    approval_increments: dict = {}
    async with sessionmaker() as db:
        registry = await db.get(DocumentSourceRegistry, registry_id)
        try:
            await doc_sched_svc.transition(db, registry, DocumentSourceStatus.PROCESSING)
            async with stats_lock:
                stats["current_file"] = registry.file_name
                stats["current_stage"] = "MAPPING FIELDS"
                await _persist_stats(db, run_id, stats)

            doc = await document_service.save_database_record(
                db, row=row, organization_id=organization_id,
                source_connection_id=connection.id, source_path=config.source_path,
                file_name=f"row_{record_key}", source_schedule_id=schedule_id,
            )
            registry.document_id = doc.id
            await db.commit()

            async with stats_lock:
                stats["current_stage"] = "VALIDATING"
                await _persist_stats(db, run_id, stats)

            fields = document_service.build_fields_from_record(row, config.document_type)
            doc = await document_service.process_document(
                db, doc, confidence_threshold=config.confidence_threshold, accuracy_threshold=config.accuracy_threshold,
                document_type=config.document_type,
                invoice_auto_post=config.invoice_auto_post, invoice_erp_connection_id=config.invoice_erp_connection_id,
                preextracted_fields=fields,
            )

            if config.document_type == "INVOICE":
                approval_increments = await _classify_invoice_approval_outcome(db, doc.id)

            if doc.status == DocumentStatus.NEEDS_REVIEW:
                await review_svc.create_from_document(db, doc)
                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.REVIEW_REQUIRED)
                outcome = "review"
            elif doc.status == DocumentStatus.FAILED:
                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.FAILED, error=doc.error_message)
                outcome = "failed"
            else:
                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.PROCESSED)
                outcome = "success"
        except Exception as exc:  # noqa: BLE001 - a single record's failure must never sink the whole batch (spec section 28)
            registry.processing_attempts += 1
            registry.status = DocumentSourceStatus.FAILED
            registry.last_error = str(exc)[:2000]
            await db.commit()
            outcome = "failed"

        async with stats_lock:
            stats["processed"] += 1
            stats[outcome] = stats.get(outcome, 0) + 1
            for key, amount in approval_increments.items():
                stats[key] = stats.get(key, 0) + amount
            stats["current_file"] = None
            stats["current_stage"] = None
            await _persist_stats(db, run_id, stats)

    return outcome


async def _persist_stats(db: AsyncSession, run_id: int, stats: dict) -> None:
    run = await db.get(ScheduleRun, run_id)
    if run is not None:
        run.document_stats = dict(stats)
        await db.commit()


async def _run_document_schedule_background(schedule_id: int, run_id: int, organization_id: int | None) -> None:
    """The fire-and-forget body execute_schedule() kicks off via
    asyncio.create_task() for a schedule with a DocumentScheduleConfig.
    Runs completely detached from the HTTP request that triggered it — own
    DB session(s) throughout, same tenant_engine_registry pattern
    document_service._process_in_background() already uses."""
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    stats = _stats_defaults()
    stats_lock = asyncio.Lock()
    started_at = datetime.now(timezone.utc)

    async def _fail_run(message: str) -> None:
        async with sessionmaker() as db:
            schedule = await db.get(Schedule, schedule_id)
            run = await db.get(ScheduleRun, run_id)
            if schedule is None or run is None:
                return
            run.status = RunStatus.FAILED
            run.error_message = message
            run.ended_at = datetime.now(timezone.utc)
            run.duration_seconds = round((run.ended_at - started_at).total_seconds(), 1)
            _finalize_schedule(schedule, run)
            await db.commit()

    async with sessionmaker() as db:
        schedule = await db.get(Schedule, schedule_id)
        config = await get_document_config(db, schedule_id)
        if schedule is None or config is None:
            return
        connection = await db.get(Connection, config.source_connection_id)
        if connection is None:
            await _fail_run(f"SOURCE_CONNECTION_FAILED: connection #{config.source_connection_id} no longer exists.")
            return

        is_record_source = config.source_kind in ("DATABASE", "API", "ERP")

        if is_record_source:
            try:
                if config.source_kind == "DATABASE":
                    records = await doc_sched_svc.discover_database_records(connection, config)
                elif config.source_kind == "API":  # PAY20
                    records = await doc_sched_svc.discover_api_records(connection, config)
                else:  # ERP (PAY21)
                    records = await doc_sched_svc.discover_erp_records(connection, config)
            except ConnectorError as exc:
                await _fail_run(f"SOURCE_CONNECTION_FAILED: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - never leave a run stuck RUNNING over an unanticipated query/fetch error
                await _fail_run(f"SOURCE_QUERY_FAILED: {exc}")
                return
            # batch_size caps how many rows ONE run takes on, same
            # section 5/31 principle a FILE schedule's discovery already
            # follows — discover_database_records()/discover_api_records()
            # themselves already bound their result to this same limit, so
            # this slice is a no-op today; kept for symmetry and as a
            # genuine backstop if that changes.
            records = records[: max(1, config.batch_size)]
            stats["discovered"] = len(records)
            await _persist_stats(db, run_id, stats)

            to_process: list[tuple[object, ...]] = []
            for row in records:
                content_hash = doc_sched_svc.record_content_hash(row)
                source_path, file_name, record_key = doc_sched_svc.record_identity(config, row, content_hash)
                file_info = FileInfo(name=file_name, path=source_path, size=len(json.dumps(row, default=str)))

                registry = await doc_sched_svc.upsert_registry_entry(
                    db, organization_id=organization_id, schedule_id=schedule_id, connection=connection,
                    source_path=source_path, file_info=file_info, content_hash=content_hash, run_id=run_id,
                )
                if not doc_sched_svc.needs_processing(registry):
                    continue  # already PROCESSED with this exact content — spec section 7, same rule as a file

                registry.processing_run_id = run_id

                if config.duplicate_content_behavior == "SKIP_DUPLICATE":
                    dup = await doc_sched_svc.find_duplicate_content(db, organization_id=organization_id, content_hash=content_hash, exclude_id=registry.id)
                    if dup is not None:
                        await doc_sched_svc.transition(db, registry, DocumentSourceStatus.SKIPPED, error=f"Duplicate content of '{dup.file_name}' (already processed as registry #{dup.id}).")
                        stats["skipped"] += 1
                        continue

                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.QUEUED)
                to_process.append((row, record_key, registry.id))

            stats["queued"] = len(to_process)
            await _persist_stats(db, run_id, stats)
        else:
            try:
                files = await doc_sched_svc.discover_files(connection, config)
            except ConnectorError as exc:
                await _fail_run(f"SOURCE_CONNECTION_FAILED: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - never leave a run stuck RUNNING over an unanticipated discovery error
                await _fail_run(f"SOURCE_PATH_NOT_FOUND: {exc}")
                return

            # batch_size caps how many files ONE run takes on (spec section
            # 5/31) — anything beyond it is simply picked up by the next run,
            # not lost (still DISCOVERED, still in the source).
            files = files[: max(1, config.batch_size)]
            stats["discovered"] = len(files)
            await _persist_stats(db, run_id, stats)

            to_process: list[tuple[object, ...]] = []
            for file_info in files:
                try:
                    content_hash = await doc_sched_svc.compute_file_hash(connection, file_info.path)
                except ConnectorError:
                    continue  # couldn't even hash it (blip while scanning) — one bad file, not a run failure (section 28)

                registry = await doc_sched_svc.upsert_registry_entry(
                    db, organization_id=organization_id, schedule_id=schedule_id, connection=connection,
                    source_path=config.source_path, file_info=file_info, content_hash=content_hash, run_id=run_id,
                )
                if not doc_sched_svc.needs_processing(registry):
                    continue  # already PROCESSED with this exact content — spec section 7

                # This run is about to act on the file (queue/skip/fail/mark
                # unsupported) — that's what processing_run_id records ("the
                # run that acted on this file"), so stamp it here rather than
                # unconditionally in upsert_registry_entry(). See that
                # function's docstring for the bug this fixes.
                registry.processing_run_id = run_id

                if not doc_sched_svc.is_supported_file(file_info.name):
                    await doc_sched_svc.transition(db, registry, DocumentSourceStatus.UNSUPPORTED)
                    stats["unsupported"] += 1
                    continue
                if file_info.size > config.max_file_size_mb * 1024 * 1024:
                    await doc_sched_svc.transition(db, registry, DocumentSourceStatus.FAILED, error="FILE_SIZE_EXCEEDED")
                    stats["failed"] += 1
                    continue
                if config.duplicate_content_behavior == "SKIP_DUPLICATE":
                    dup = await doc_sched_svc.find_duplicate_content(db, organization_id=organization_id, content_hash=content_hash, exclude_id=registry.id)
                    if dup is not None:
                        await doc_sched_svc.transition(db, registry, DocumentSourceStatus.SKIPPED, error=f"Duplicate content of '{dup.file_name}' (already processed as registry #{dup.id}).")
                        stats["skipped"] += 1
                        continue

                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.QUEUED)
                to_process.append((file_info, registry.id))

            stats["queued"] = len(to_process)
            await _persist_stats(db, run_id, stats)

        if not to_process:
            run = await db.get(ScheduleRun, run_id)
            run.status = RunStatus.SUCCESS
            run.ended_at = datetime.now(timezone.utc)
            run.duration_seconds = round((run.ended_at - started_at).total_seconds(), 1)
            run.records_processed = 0
            run.error_message = None
            run.logs = [{"timestamp": run.ended_at.isoformat(), "level": "INFO", "message": "No new unprocessed documents found."}]
            _finalize_schedule(schedule, run)
            await db.commit()
            return

    # Concurrent per-item processing (spec section 10/30) — bounded by
    # this schedule's own max_concurrency, each file/record in its own
    # session.
    semaphore = asyncio.Semaphore(max(1, config.max_concurrency))

    async def _bounded(item: tuple, registry_id: int) -> None:
        async with semaphore:
            async with sessionmaker() as check_db:
                fresh_run = await check_db.get(ScheduleRun, run_id)
                if fresh_run is not None and fresh_run.cancel_requested:
                    fresh_registry = await check_db.get(DocumentSourceRegistry, registry_id)
                    if fresh_registry is not None and fresh_registry.status == DocumentSourceStatus.QUEUED:
                        await doc_sched_svc.transition(check_db, fresh_registry, DocumentSourceStatus.SKIPPED, error="Run cancelled before this file started.")
                    return  # cooperative cancellation checkpoint (section 81) — between files only, never mid-file
            if is_record_source:
                row, record_key = item
                await _process_one_record(
                    organization_id=organization_id, schedule_id=schedule_id, connection=connection, config=config,
                    row=row, record_key=record_key, registry_id=registry_id, run_id=run_id, stats=stats, stats_lock=stats_lock,
                    sessionmaker=sessionmaker,
                )
            else:
                (file_info,) = item
                await _process_one_file(
                    organization_id=organization_id, schedule_id=schedule_id, connection=connection, config=config,
                    file_info=file_info, registry_id=registry_id, run_id=run_id, stats=stats, stats_lock=stats_lock,
                    sessionmaker=sessionmaker, schedule_retry_count=schedule.retry_count, schedule_retry_interval=schedule.retry_interval_seconds,
                )

    if is_record_source:
        await asyncio.gather(*((_bounded((row, record_key), rid)) for row, record_key, rid in to_process))
    else:
        await asyncio.gather(*((_bounded((file_info,), rid)) for file_info, rid in to_process))

    async with sessionmaker() as db:
        run = await db.get(ScheduleRun, run_id)
        schedule = await db.get(Schedule, schedule_id)
        run.ended_at = datetime.now(timezone.utc)
        run.duration_seconds = round((run.ended_at - started_at).total_seconds(), 1)
        run.records_processed = stats["success"]
        run.records_failed = stats["failed"]
        unit = "record(s)" if is_record_source else "file(s)"
        if run.cancel_requested:
            run.status = RunStatus.CANCELLED
            run.error_message = f"Cancelled after processing {stats['processed']} of {stats['queued']} queued {unit}."
        elif stats["failed"] == 0:
            run.status = RunStatus.SUCCESS
            run.error_message = None
        elif stats["success"] or stats["review"]:
            run.status = RunStatus.COMPLETED_WITH_ERRORS
            run.error_message = f"{stats['failed']} of {stats['queued']} {unit} failed — see Document Processing History for details."
        else:
            run.status = RunStatus.FAILED
            run.error_message = f"All {stats['failed']} queued {unit} failed."
        _finalize_schedule(schedule, run)
        await db.commit()


async def recover_stuck_document_schedules() -> dict:
    """Startup recovery (spec section 56/58) — called once from main.py's
    lifespan, same place _seed_*() already runs. Two things can be
    orphaned by a backend restart mid-run:
      1. A ScheduleRun still RUNNING — genuinely impossible if this
         process just started, so any such row is from a process that
         died mid-execution. Marked FAILED with a clear reason; its
         Schedule is un-stuck from RUNNING back to ACTIVE/FAILED via the
         same _finalize_schedule() every normal completion uses.
      2. A DocumentSourceRegistry row stuck in PROCESSING/QUEUED with no
         live run behind it — requeued (back to QUEUED, so the next run
         picks it up) if under its schedule's retry budget, else FAILED.
    Runs against the platform/default tenant DB only (AsyncSessionLocal)
    — the same scope every other startup seed function in main.py already
    has; an org on its own separate physical tenant database (opt-in, see
    Bring-Your-Own-Database) would need its own recovery pass, a known
    limitation shared with those existing seed functions."""
    recovered_runs = 0
    requeued_documents = 0
    failed_documents = 0

    async with AsyncSessionLocal() as db:
        stuck_runs = (await db.execute(
            select(ScheduleRun).where(ScheduleRun.status == RunStatus.RUNNING)
        )).scalars().all()
        for run in stuck_runs:
            schedule = await db.get(Schedule, run.schedule_id)
            run.status = RunStatus.FAILED
            run.error_message = "Interrupted by a backend restart — any files still queued will be picked up by the next run."
            run.ended_at = datetime.now(timezone.utc)
            started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
            run.duration_seconds = round((run.ended_at - started).total_seconds(), 1)
            if schedule is not None:
                _finalize_schedule(schedule, run)
            recovered_runs += 1

        stuck_docs = (await db.execute(
            select(DocumentSourceRegistry).where(
                DocumentSourceRegistry.status.in_([DocumentSourceStatus.PROCESSING, DocumentSourceStatus.QUEUED])
            )
        )).scalars().all()
        for entry in stuck_docs:
            schedule = await db.get(Schedule, entry.schedule_id) if entry.schedule_id else None
            max_attempts = 1 + max(0, schedule.retry_count if schedule else 0)
            if entry.processing_attempts < max_attempts:
                entry.status = DocumentSourceStatus.QUEUED
                entry.last_error = "Requeued after a backend restart interrupted processing."
                requeued_documents += 1
            else:
                entry.status = DocumentSourceStatus.FAILED
                entry.last_error = "Exceeded retry budget after being interrupted by repeated backend restarts."
                failed_documents += 1

        await db.commit()

    return {"recovered_runs": recovered_runs, "requeued_documents": requeued_documents, "failed_documents": failed_documents}


async def get_run(db: AsyncSession, schedule_id: int, run_id: int) -> ScheduleRun | None:
    result = await db.execute(select(ScheduleRun).where(ScheduleRun.id == run_id, ScheduleRun.schedule_id == schedule_id))
    return result.scalar_one_or_none()
