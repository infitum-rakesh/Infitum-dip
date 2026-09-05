"""
scheduler_poller.py — the real "Scheduler starts automatically" mechanism
(spec section 10/56) that was missing before: schedule_service.py's own
prior docstring said so explicitly ("nothing here fires automatically on a
wall-clock timer... actual execution only happens when something calls
POST /schedules/{id}/run"). This module is that missing piece — started
from main.py's lifespan alongside the existing billing scheduler
(APScheduler is already a dependency of this codebase; see
services/billing/billing_scheduler.py for the precedent this follows).

Every schedule type benefits uniformly (document-source, real AnomalyJob,
and the still-simulated generic pipeline path) — this module only decides
WHEN to call schedule_service.execute_schedule(), never what a schedule
actually does once fired.

Runs against the platform/default tenant DB only (AsyncSessionLocal) —
same single-DB-instance scope every existing startup task in main.py
already has (see schedule_service.recover_stuck_document_schedules()'s
docstring for the same disclosed limitation).
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.models.schedule import Schedule, ScheduleStatus
from app.services import schedule_service

logger = logging.getLogger(__name__)


async def _due_schedules(db: AsyncSession) -> list[Schedule]:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Schedule).where(Schedule.status == ScheduleStatus.ACTIVE, Schedule.next_run_at.is_not(None), Schedule.next_run_at <= now)
    )
    return list(result.scalars().all())


async def run_poll() -> dict:
    """One poll tick — called every 60s by the APScheduler job main.py
    registers. Each due schedule is fired independently and defensively
    (one schedule's failure to start must never stop the others — same
    per-job isolation principle billing_scheduler.py's two sweeps already
    follow); execute_schedule() itself is the run-lock (schedule.status
    compare-and-swap — see ScheduleAlreadyRunningError), so a schedule
    whose previous run is still going is silently skipped this tick and
    picked up on a later one, not double-fired."""
    fired = 0
    skipped_already_running = 0
    errors = 0
    due_count = 0

    async with AsyncSessionLocal() as db:
        due = await _due_schedules(db)
        due_count = len(due)
        for schedule in due:
            try:
                await schedule_service.execute_schedule(db, schedule, trigger="Schedule")
                fired += 1
            except schedule_service.ScheduleAlreadyRunningError:
                skipped_already_running += 1
            except Exception:  # noqa: BLE001 - one schedule's failure must never stop the poller from firing the rest
                logger.exception("Scheduler poll: schedule #%s failed to start", schedule.id)
                errors += 1

    return {"due": due_count, "fired": fired, "skipped_already_running": skipped_already_running, "errors": errors}
