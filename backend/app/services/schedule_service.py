"""
ScheduleService — automates execution of existing pipelines.

Integration, not duplication: Schedules never re-implements what a pipeline
does (OCR / AI Extraction / Transformation / Validation / Reconciliation
etc. all already exist as their own modules — Document Intelligence's are
real, the rest are still frontend-only per Pipeline Builder). A Schedule
just references a pipeline by id/name (the same catalogue Pipeline Builder
already lists via pipelineApi.js) and records WHEN it should run.

No background worker/cron daemon exists in this codebase, so nothing here
fires automatically on a wall-clock timer. `next_run_at` is computed and
displayed for information (and is what a real scheduler daemon would poll
on), but actual execution only happens when something calls
POST /schedules/{id}/run — either a human clicking "Run Now" or, later, a
real scheduler process hitting the same endpoint. That's the seam: swap in
a cron-like poller later and this module doesn't change.

Because there's no second execution engine to call into yet either,
execute_schedule() simulates a plausible run (status/duration/records/logs),
the same pattern used by document_service.process_document() and
connection_service.test_connection().
"""
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal

from app.core.config import get_settings
from app.models.schedule import RunStatus, Schedule, ScheduleFrequency, ScheduleRun, ScheduleStatus
from app.models.user import User

settings = get_settings()

OPEN_RUN_STATUSES = {RunStatus.RUNNING}


class ScheduleValidationError(Exception):
    """Raised for bad frequency/cron/date-time input."""


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


async def create_schedule(db: AsyncSession, *, organization_id: int | None, owner_id: int, body) -> Schedule:
    schedule = Schedule(
        organization_id=organization_id, owner_id=owner_id, name=body.name,
        pipeline_id=body.pipeline_id, pipeline_name=body.pipeline_name,
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


def to_out(schedule: Schedule, owner_name: str | None) -> dict:
    return {
        "id": schedule.id, "name": schedule.name, "pipeline_id": schedule.pipeline_id, "pipeline_name": schedule.pipeline_name,
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


def run_to_out(run: ScheduleRun) -> dict:
    return {
        "id": run.id, "schedule_id": run.schedule_id, "status": run.status.value, "trigger": run.trigger,
        "started_at": _as_utc(run.started_at), "ended_at": _as_utc_opt(run.ended_at),
        "duration_seconds": run.duration_seconds, "records_processed": run.records_processed,
        "records_failed": run.records_failed, "error_message": run.error_message,
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


async def execute_schedule(db: AsyncSession, schedule: Schedule, *, trigger: str) -> ScheduleRun:
    now = datetime.now(timezone.utc)
    run = ScheduleRun(schedule_id=schedule.id, status=RunStatus.RUNNING, trigger=trigger, started_at=now, logs=[])
    db.add(run)
    schedule.status = ScheduleStatus.RUNNING
    await db.commit()
    await db.refresh(run)

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

    schedule.last_run_at = ended_at
    schedule.last_run_status = run.status.value
    if schedule.frequency == ScheduleFrequency.ONCE:
        schedule.status = ScheduleStatus.COMPLETED if success else ScheduleStatus.FAILED
        schedule.next_run_at = None
    else:
        schedule.status = ScheduleStatus.ACTIVE if success else ScheduleStatus.FAILED
        schedule.next_run_at = compute_next_run(schedule, after=ended_at)

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


async def get_run(db: AsyncSession, schedule_id: int, run_id: int) -> ScheduleRun | None:
    result = await db.execute(select(ScheduleRun).where(ScheduleRun.id == run_id, ScheduleRun.schedule_id == schedule_id))
    return result.scalar_one_or_none()
