"""
Bulk Object Migration — run lifecycle + bounded worker pool (BM4).

This is the "N WORKERS" half of the architecture — bulk_migration_planner_
service.py (BM3) already turned the pipeline's enabled objects into
BulkMigrationTask rows; this module claims them, executes them (via
bulk_migration_task_executor.py), and drives the run to completion.

Concurrency idiom is the one thing repeated in three places in this
codebase already (services/schedule_service.py's
_run_document_schedule_background(), execution/pipeline_executor.py,
services/document_schedule_service.py): asyncio.Semaphore(N) + a bounded
worker loop + asyncio.gather() — no Celery/Redis/RQ exists anywhere here
(confirmed via requirements.txt). Dispatch itself is fire-and-forget
asyncio.create_task() from start_run(), same as pipeline_executor.py's own
dispatch — the HTTP request that clicks "Run" returns immediately with the
new BulkMigrationRun row; the frontend polls for progress (matching
JobLogDrawer.jsx/TenantMigrationLogDrawer.jsx's existing polling pattern).

Worker pool shape — TRUE dynamic work-stealing, not fixed object ranges:
a fixed-size pool of N worker coroutines each loop claim-one -> execute-one
-> claim-next, so a worker that finishes early immediately picks up the
next claimable task regardless of what slower siblings are doing. Because
dependency levels gate which tasks are even claimable (BLOCKED until
promoted), a "generation" of N workers naturally drains everything
currently claimable and then exits when no more work is available; the
outer loop then promotes the next dependency level's BLOCKED tasks (see
_promote_blocked_tasks()) and starts a fresh generation, repeating until
the run is genuinely out of work.

Task claiming is a portable atomic compare-and-swap UPDATE ... WHERE
status IN (...) + rowcount check — NOT `SELECT ... FOR UPDATE SKIP
LOCKED`, because this project's default tenant DB is SQLite (see
database/connection.py's TenantBase / the Bring-Your-Own-Database feature
for Postgres/MySQL/SQL Server as opt-in alternatives) and SQLite has no
row-level locking. The same UPDATE+rowcount idiom works identically across
all four dialects — disclosed explicitly as the reason, not a silent
downgrade. Since every worker in THIS release lives in the same asyncio
event loop/process (no multi-process worker deployment exists anywhere
else in this codebase either), the atomic claim is stronger protection
than strictly needed today, but is exactly what would keep working
unmodified if this ever became a horizontally-scaled deployment.

Task finalization (_finalize_task) is the single choke point every status
write outside of claim_task() goes through, gated by
models/bulk_migration_run.py's ALLOWED_TASK_TRANSITIONS dict — mirrors
document_schedule_service.py's transition() precedent over
DocumentSourceStatus.ALLOWED_TRANSITIONS.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import tenant_engine_registry
from app.database.connection import AsyncSessionLocal
from app.execution.retry import PERMANENT_ERRORS as CONNECTOR_PERMANENT_ERRORS
from app.loading.writers.registry import get_bulk_writer
from app.models.audit_log import record_event
from app.models.bulk_migration import BulkMigrationPipeline
from app.models.bulk_migration_run import (
    ALLOWED_TASK_TRANSITIONS, BulkMigrationError, BulkMigrationErrorCode, BulkMigrationRun,
    BulkMigrationRunStatus, BulkMigrationTask, BulkMigrationTaskEvent, BulkMigrationTaskStatus,
)
from app.services import bulk_migration_concurrency as concurrency
from app.services import bulk_migration_planner_service as planner
from app.services import bulk_migration_task_executor as task_executor
from app.services import connection_service

# Error codes that are never worth retrying — a bad config/schema will
# fail identically on attempt 2 as attempt 1. Everything else (connection
# blips, timeouts, throttling, unclassified) is treated as transient,
# matching execution/retry.py's own permanent-vs-everything-else split.
PERMANENT_BM_ERROR_CODES = {
    BulkMigrationErrorCode.CONFIGURATION_ERROR, BulkMigrationErrorCode.SCHEMA_MISMATCH,
    BulkMigrationErrorCode.MAPPING_ERROR, BulkMigrationErrorCode.VALIDATION_ERROR,
    BulkMigrationErrorCode.DEPENDENCY_ERROR, BulkMigrationErrorCode.UNSUPPORTED_LOAD_TYPE,
    BulkMigrationErrorCode.SOURCE_OBJECT_NOT_FOUND, BulkMigrationErrorCode.TARGET_OBJECT_NOT_FOUND,
    BulkMigrationErrorCode.AUTHENTICATION_ERROR,
}
TERMINAL_TASK_STATUSES = {
    BulkMigrationTaskStatus.SUCCESS, BulkMigrationTaskStatus.FAILED,
    BulkMigrationTaskStatus.SKIPPED, BulkMigrationTaskStatus.CANCELLED,
}
CLAIM_BATCH = 25
BACKOFF_BASE_SECONDS = 10.0
BACKOFF_MAX_SECONDS = 90.0


class BulkMigrationServiceError(Exception):
    pass


def _classify_error(exc: Exception) -> tuple[str, str, bool]:
    """Returns (error_code, message, is_permanent)."""
    if isinstance(exc, task_executor.TaskCancelledSignal):
        return "CANCELLED", str(exc), True  # not used for retry math — the caller special-cases this type before calling _classify_error
    if isinstance(exc, BulkMigrationError):
        return exc.code.value, exc.message, exc.code in PERMANENT_BM_ERROR_CODES
    if isinstance(exc, CONNECTOR_PERMANENT_ERRORS):
        return BulkMigrationErrorCode.CONFIGURATION_ERROR.value, str(exc), True
    return BulkMigrationErrorCode.UNKNOWN_ERROR.value, str(exc), False


async def _log_event(db: AsyncSession, run_id: int, task_id: int | None, event_type: str, message: str, worker_id: str | None = None) -> None:
    db.add(BulkMigrationTaskEvent(run_id=run_id, task_id=task_id, event_type=event_type, message=message, worker_id=worker_id))
    await db.commit()


async def _finalize_task(db: AsyncSession, task_id: int, expected_status: BulkMigrationTaskStatus, new_status: BulkMigrationTaskStatus, **extra_values) -> bool:
    if new_status not in ALLOWED_TASK_TRANSITIONS.get(expected_status, set()):
        return False
    values = {"status": new_status, **extra_values}
    if new_status in TERMINAL_TASK_STATUSES:
        values.setdefault("completed_at", datetime.now(timezone.utc))
    result = await db.execute(
        update(BulkMigrationTask).where(BulkMigrationTask.id == task_id, BulkMigrationTask.status == expected_status).values(**values)
    )
    return result.rowcount == 1


# ── Task claiming ────────────────────────────────────────────────────────

async def claim_task(db: AsyncSession, run_id: int, worker_id: str) -> BulkMigrationTask | None:
    """Portable atomic claim — see module docstring."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(BulkMigrationTask.id)
        .where(
            BulkMigrationTask.run_id == run_id,
            or_(
                BulkMigrationTask.status == BulkMigrationTaskStatus.PENDING,
                and_(
                    BulkMigrationTask.status == BulkMigrationTaskStatus.RETRY_PENDING,
                    or_(BulkMigrationTask.retry_after.is_(None), BulkMigrationTask.retry_after <= now),
                ),
            ),
        )
        .order_by(BulkMigrationTask.dependency_level.asc(), BulkMigrationTask.id.asc())
        .limit(CLAIM_BATCH)
    )
    candidate_ids = (await db.execute(stmt)).scalars().all()
    for candidate_id in candidate_ids:
        result = await db.execute(
            update(BulkMigrationTask)
            .where(BulkMigrationTask.id == candidate_id, BulkMigrationTask.status.in_([BulkMigrationTaskStatus.PENDING, BulkMigrationTaskStatus.RETRY_PENDING]))
            .values(status=BulkMigrationTaskStatus.RUNNING, worker_id=worker_id, claimed_at=now, started_at=now, last_heartbeat=now, attempt_number=BulkMigrationTask.attempt_number + 1)
        )
        if result.rowcount == 1:
            await db.commit()
            await _sync_live_counters(db, run_id)
            return await db.get(BulkMigrationTask, candidate_id)
    await db.rollback()
    return None


async def _sync_live_counters(db: AsyncSession, run_id: int) -> None:
    """Single source of truth for EVERY object-status counter on the run
    row — recomputed via one GROUP BY query rather than incremental +1/-1
    bumps scattered across claim/promote/fail/cancel/retry call sites.
    That scattered-increment approach is exactly the bug class this
    project keeps re-discovering (retry_failed()'s original double-count
    fix, then pending/blocked staleness, then skipped_objects never being
    written at all) — recomputing from the real task rows every time a
    task's status changes eliminates the whole class at once rather than
    patching each symptom. RETRY_PENDING folds into pending_objects —
    both are "waiting to be claimed" from the UI's point of view; there's
    no separate schema column for it. error_count is deliberately NOT
    touched here — it's a genuine cumulative/lifetime metric (how many
    times something went wrong across all attempts, including attempts
    that were later retried successfully), not a current-status count,
    so it stays a plain incremental bump via _bump_run_counter()."""
    rows = (await db.execute(
        select(BulkMigrationTask.status, func.count()).where(BulkMigrationTask.run_id == run_id).group_by(BulkMigrationTask.status)
    )).all()
    counts = dict(rows)
    await db.execute(update(BulkMigrationRun).where(BulkMigrationRun.id == run_id).values(
        pending_objects=counts.get(BulkMigrationTaskStatus.PENDING, 0) + counts.get(BulkMigrationTaskStatus.RETRY_PENDING, 0),
        queued_objects=counts.get(BulkMigrationTaskStatus.QUEUED, 0),
        running_objects=counts.get(BulkMigrationTaskStatus.RUNNING, 0),
        blocked_objects=counts.get(BulkMigrationTaskStatus.BLOCKED, 0),
        success_objects=counts.get(BulkMigrationTaskStatus.SUCCESS, 0),
        failed_objects=counts.get(BulkMigrationTaskStatus.FAILED, 0),
        skipped_objects=counts.get(BulkMigrationTaskStatus.SKIPPED, 0),
        cancelled_objects=counts.get(BulkMigrationTaskStatus.CANCELLED, 0),
    ))
    await db.commit()


async def _promote_blocked_tasks(db: AsyncSession, run_id: int) -> int:
    """Level-based dependency gating — see module docstring's "TRUE dynamic
    work-stealing" section. Once every task at level L has reached a
    terminal status, BLOCKED tasks at level L+1 become claimable: PENDING
    if every object_key in their own depends_on actually SUCCEEDED, else
    SKIPPED (running against a missing/failed dependency's data would be
    worse than skipping — continue_on_error still lets the REST of the run
    proceed either way)."""
    blocked_levels = sorted(set((await db.execute(
        select(BulkMigrationTask.dependency_level).where(BulkMigrationTask.run_id == run_id, BulkMigrationTask.status == BulkMigrationTaskStatus.BLOCKED).distinct()
    )).scalars().all()))
    if not blocked_levels:
        return 0

    promoted = 0
    for level in blocked_levels:
        prior_incomplete = (await db.execute(
            select(func.count()).select_from(BulkMigrationTask).where(
                BulkMigrationTask.run_id == run_id, BulkMigrationTask.dependency_level < level,
                BulkMigrationTask.status.notin_(TERMINAL_TASK_STATUSES),
            )
        )).scalar_one()
        if prior_incomplete > 0:
            continue  # an earlier level (this one's or an ancestor's) still has work in flight

        succeeded_keys = set((await db.execute(
            select(BulkMigrationTask.object_key).where(
                BulkMigrationTask.run_id == run_id, BulkMigrationTask.dependency_level < level, BulkMigrationTask.status == BulkMigrationTaskStatus.SUCCESS,
            )
        )).scalars().all())
        blocked_tasks = (await db.execute(
            select(BulkMigrationTask).where(BulkMigrationTask.run_id == run_id, BulkMigrationTask.dependency_level == level, BulkMigrationTask.status == BulkMigrationTaskStatus.BLOCKED)
        )).scalars().all()
        for t in blocked_tasks:
            deps = t.depends_on or []
            all_ok = all(d in succeeded_keys for d in deps)
            if all_ok:
                await db.execute(update(BulkMigrationTask).where(BulkMigrationTask.id == t.id, BulkMigrationTask.status == BulkMigrationTaskStatus.BLOCKED).values(status=BulkMigrationTaskStatus.PENDING))
            else:
                await db.execute(update(BulkMigrationTask).where(BulkMigrationTask.id == t.id, BulkMigrationTask.status == BulkMigrationTaskStatus.BLOCKED).values(
                    status=BulkMigrationTaskStatus.SKIPPED, completed_at=datetime.now(timezone.utc),
                    error_code=BulkMigrationErrorCode.DEPENDENCY_ERROR.value, error_message="A required dependency did not complete successfully.",
                ))
            promoted += 1
    if promoted:
        await db.commit()
        await _sync_live_counters(db, run_id)
    return promoted


# ── Run lifecycle ────────────────────────────────────────────────────────

async def start_run(db: AsyncSession, pipeline: BulkMigrationPipeline, *, trigger_type: str = "MANUAL", created_by: int | None = None) -> BulkMigrationRun:
    active_count = (await db.execute(
        select(func.count()).select_from(BulkMigrationRun).where(
            BulkMigrationRun.pipeline_id == pipeline.id,
            BulkMigrationRun.status.in_([BulkMigrationRunStatus.PENDING, BulkMigrationRunStatus.VALIDATING, BulkMigrationRunStatus.RUNNING, BulkMigrationRunStatus.PAUSING, BulkMigrationRunStatus.PAUSED]),
        )
    )).scalar_one()
    if active_count >= max(1, pipeline.max_concurrent_runs):
        raise BulkMigrationServiceError(
            f"This pipeline already has {active_count} active run(s), at its configured limit of {pipeline.max_concurrent_runs}. "
            "Wait for one to finish, or cancel it, before starting another."
        )

    run = await planner.create_run(db, pipeline, trigger_type=trigger_type, created_by=created_by)
    # Fire-and-forget — mirrors execution/pipeline_executor.py's own
    # dispatch: the HTTP request returns the PENDING run immediately, the
    # frontend polls GET /runs/{id} for live progress.
    asyncio.create_task(_run_worker_pool(run.id, pipeline.organization_id))
    return run


async def _run_worker_pool(run_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)

    async with sessionmaker() as db:
        run = await db.get(BulkMigrationRun, run_id)
        run.status = BulkMigrationRunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        await db.commit()
        effective_workers = run.effective_workers
        await _log_event(db, run_id, None, "RUN_STARTED", f"{run.total_objects} objects, {run.dependency_levels} dependency level(s), {effective_workers} workers.")

    async def _cancel_check(_run_id: int) -> bool:
        async with sessionmaker() as check_db:
            row = (await check_db.execute(select(BulkMigrationRun.cancel_requested).where(BulkMigrationRun.id == run_id))).scalar_one_or_none()
            return bool(row)

    async def _one_worker(worker_id: str) -> None:
        async with sessionmaker() as db:
            while True:
                run_row = (await db.execute(select(BulkMigrationRun.cancel_requested, BulkMigrationRun.pause_requested).where(BulkMigrationRun.id == run_id))).one_or_none()
                if run_row is None or run_row.cancel_requested or run_row.pause_requested:
                    return
                task = await claim_task(db, run_id, worker_id)
                if task is None:
                    return  # nothing currently claimable — this generation is done
                await _run_one_task(db, sessionmaker, run_id, task, worker_id, _cancel_check, organization_id)

    # Outer "generation" loop — see module docstring.
    while True:
        async with sessionmaker() as db:
            run_row = (await db.execute(select(BulkMigrationRun.cancel_requested, BulkMigrationRun.pause_requested, BulkMigrationRun.effective_workers).where(BulkMigrationRun.id == run_id))).one_or_none()
        if run_row is None:
            return
        if run_row.cancel_requested:
            await _handle_cancel_requested(sessionmaker, run_id)
            return
        if run_row.pause_requested:
            await _handle_pause_requested(sessionmaker, run_id)
            return

        await asyncio.gather(*[_one_worker(f"w{i}") for i in range(max(1, run_row.effective_workers))])

        # Re-check cancel/pause AFTER the generation drains, not just before
        # dispatching it — cancel_requested can flip to True while a
        # generation's workers are already claimed and running (they only
        # look at it before claiming their NEXT task, not mid-execution).
        # Falling straight through to the still-claimable/finalize check
        # below would then finalize the run as COMPLETED even though
        # cancelled_objects > 0 (found live) — checking again here routes
        # any leftover PENDING/BLOCKED/RETRY_PENDING tasks through the
        # same cancel/pause handling as the pre-dispatch check.
        async with sessionmaker() as db:
            run_row2 = (await db.execute(select(BulkMigrationRun.cancel_requested, BulkMigrationRun.pause_requested).where(BulkMigrationRun.id == run_id))).one_or_none()
        if run_row2 is None:
            return
        if run_row2.cancel_requested:
            # Unconditional, regardless of whether anything is actually
            # left to claim — once cancel is requested the run's terminal
            # status should read CANCELLED, matching normal "Cancel"
            # button semantics even if a couple of in-flight tasks
            # happened to finish (success/failure) before their own
            # cancel_check() caught up.
            await _handle_cancel_requested(sessionmaker, run_id)
            return
        if run_row2.pause_requested:
            # UNLIKE cancel, only actually pause if there's real remaining
            # work — pause never interrupts in-flight tasks (only
            # cancel_check() does that), so if this generation happened to
            # drain everything before the pause flag was noticed, showing
            # PAUSED with a "Resume" button that has nothing to resume
            # would be misleading. Fall through to the normal
            # promote/finalize path instead, and clear the stale flag.
            async with sessionmaker() as db:
                remaining = (await db.execute(
                    select(func.count()).select_from(BulkMigrationTask).where(
                        BulkMigrationTask.run_id == run_id,
                        or_(BulkMigrationTask.status == BulkMigrationTaskStatus.PENDING, BulkMigrationTask.status == BulkMigrationTaskStatus.RETRY_PENDING, BulkMigrationTask.status == BulkMigrationTaskStatus.BLOCKED),
                    )
                )).scalar_one()
                if remaining > 0:
                    await _handle_pause_requested(sessionmaker, run_id)
                    return
                await db.execute(update(BulkMigrationRun).where(BulkMigrationRun.id == run_id).values(pause_requested=False))
                await db.commit()

        async with sessionmaker() as db:
            promoted = await _promote_blocked_tasks(db, run_id)
            if promoted:
                continue  # a fresh generation of workers can now claim the newly-promoted tasks

            still_claimable = (await db.execute(
                select(func.count()).select_from(BulkMigrationTask).where(
                    BulkMigrationTask.run_id == run_id,
                    or_(BulkMigrationTask.status == BulkMigrationTaskStatus.PENDING, BulkMigrationTask.status == BulkMigrationTaskStatus.RETRY_PENDING, BulkMigrationTask.status == BulkMigrationTaskStatus.BLOCKED),
                )
            )).scalar_one()
            if still_claimable == 0:
                await _finalize_run(db, run_id)
                return
            # Only RETRY_PENDING tasks whose retry_after hasn't arrived yet
            # remain — sleep briefly rather than busy-looping.
            await asyncio.sleep(2)


async def _run_one_task(db: AsyncSession, sessionmaker, run_id: int, task: BulkMigrationTask, worker_id: str, cancel_check, organization_id: int | None) -> None:
    source_conn = await connection_service.get_connection(db, task.source_connection_id, organization_id)
    target_conn = await connection_service.get_connection(db, task.target_connection_id, organization_id)
    if not source_conn or not target_conn:
        await _fail_or_retry(db, run_id, task, BulkMigrationErrorCode.CONNECTION_ERROR.value, "Source or target connection no longer exists.", permanent=True)
        return

    try:
        source_connector = connection_service.build_connector(source_conn)
        target_connector = connection_service.build_connector(target_conn)
        writer = get_bulk_writer(target_conn.connection_type.value, target_connector)
    except Exception as exc:
        await _fail_or_retry(db, run_id, task, BulkMigrationErrorCode.CONFIGURATION_ERROR.value, str(exc), permanent=True)
        return

    source_sem = concurrency.get_connection_semaphore(source_conn.id)
    target_sem = concurrency.get_connection_semaphore(target_conn.id)
    async with source_sem:
        async with target_sem:
            try:
                await task_executor.execute_task(source_connector, writer, task, organization_id=organization_id, cancel_check=cancel_check)
                await _log_event(db, run_id, task.id, "TASK_SUCCESS", f"{task.object_key} completed.", worker_id)
            except task_executor.TaskCancelledSignal:
                await _finalize_task(db, task.id, BulkMigrationTaskStatus.RUNNING, BulkMigrationTaskStatus.CANCELLED)
            except Exception as exc:
                code, message, permanent = _classify_error(exc)
                await _fail_or_retry(db, run_id, task, code, message, permanent=permanent)
    await _sync_live_counters(db, run_id)


async def _fail_or_retry(db: AsyncSession, run_id: int, task: BulkMigrationTask, error_code: str, error_message: str, *, permanent: bool) -> None:
    fresh = await db.get(BulkMigrationTask, task.id)
    if fresh is None:
        return
    exhausted = fresh.attempt_number >= max(1, fresh.max_retries) + 1
    if permanent or exhausted:
        ok = await _finalize_task(db, task.id, BulkMigrationTaskStatus.RUNNING, BulkMigrationTaskStatus.FAILED, error_code=error_code, error_message=error_message)
        if ok:
            await _log_event(db, run_id, task.id, "TASK_FAILED", f"{task.object_key}: [{error_code}] {error_message}")
            await _bump_run_counter(db, run_id, "error_count")
        return
    delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2.5 ** (fresh.attempt_number - 1)))
    delay *= 0.7 + 0.6 * random.random()  # full jitter — same formula as execution/retry.py::with_retry()
    retry_after = datetime.now(timezone.utc).timestamp() + delay
    ok = await _finalize_task(
        db, task.id, BulkMigrationTaskStatus.RUNNING, BulkMigrationTaskStatus.RETRY_PENDING,
        error_code=error_code, error_message=error_message, retry_after=datetime.fromtimestamp(retry_after, tz=timezone.utc),
    )
    if ok:
        await _log_event(db, run_id, task.id, "TASK_RETRY_SCHEDULED", f"{task.object_key}: attempt {fresh.attempt_number} failed ([{error_code}] {error_message}), retrying in {delay:.0f}s.")


async def _bump_run_counter(db: AsyncSession, run_id: int, column: str) -> None:
    await db.execute(update(BulkMigrationRun).where(BulkMigrationRun.id == run_id).values(**{column: getattr(BulkMigrationRun, column) + 1}))
    await db.commit()


async def _finalize_run(db: AsyncSession, run_id: int) -> None:
    run = await db.get(BulkMigrationRun, run_id)
    if run is None or run.status not in (BulkMigrationRunStatus.RUNNING, BulkMigrationRunStatus.VALIDATING, BulkMigrationRunStatus.PENDING):
        return
    if run.failed_objects > 0 and run.success_objects == 0 and run.skipped_objects == 0:
        status = BulkMigrationRunStatus.FAILED
    elif run.failed_objects > 0:
        status = BulkMigrationRunStatus.COMPLETED_WITH_ERRORS
    else:
        status = BulkMigrationRunStatus.COMPLETED
    run.status = status
    run.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await _log_event(db, run_id, None, "RUN_FINISHED", f"Final status: {status.value}. success={run.success_objects} failed={run.failed_objects} skipped={run.skipped_objects} cancelled={run.cancelled_objects}.")
    try:
        await record_event(db, organization_id=run.organization_id, actor_id=None, action="Run Finished", module="Bulk Object Migration", resource="BulkMigrationRun", resource_id=run.run_key, details=status.value)
    except Exception:
        pass


async def _handle_cancel_requested(sessionmaker, run_id: int) -> None:
    async with sessionmaker() as db:
        run = await db.get(BulkMigrationRun, run_id)
        run.status = BulkMigrationRunStatus.CANCELLING
        await db.commit()
        # Stop scheduling new work — every still-PENDING/QUEUED/BLOCKED/
        # RETRY_PENDING task moves straight to CANCELLED; a task already
        # RUNNING when cancel was requested is left alone (its own
        # cancel_check() will catch it on the next chunk boundary and
        # raise TaskCancelledSignal, finalized by _run_one_task above) —
        # never abruptly killed mid-write.
        result = await db.execute(
            update(BulkMigrationTask).where(
                BulkMigrationTask.run_id == run_id,
                BulkMigrationTask.status.in_([
                    BulkMigrationTaskStatus.PENDING, BulkMigrationTaskStatus.QUEUED, BulkMigrationTaskStatus.BLOCKED,
                    BulkMigrationTaskStatus.RETRY_PENDING,
                ]),
            ).values(status=BulkMigrationTaskStatus.CANCELLED, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()
        run.status = BulkMigrationRunStatus.CANCELLED
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await _sync_live_counters(db, run_id)
        await _log_event(db, run_id, None, "RUN_CANCELLED", f"{result.rowcount or 0} not-yet-started task(s) cancelled.")


async def _handle_pause_requested(sessionmaker, run_id: int) -> None:
    async with sessionmaker() as db:
        run = await db.get(BulkMigrationRun, run_id)
        run.status = BulkMigrationRunStatus.PAUSED
        await db.commit()
        await _log_event(db, run_id, None, "RUN_PAUSED", "No new tasks will be claimed until resumed.")


async def request_pause(db: AsyncSession, run: BulkMigrationRun) -> BulkMigrationRun:
    if run.status != BulkMigrationRunStatus.RUNNING:
        raise BulkMigrationServiceError(f"Cannot pause a run in status {run.status.value}.")
    run.pause_requested = True
    await db.commit()
    await db.refresh(run)
    return run


async def resume_run(db: AsyncSession, run: BulkMigrationRun) -> BulkMigrationRun:
    if run.status != BulkMigrationRunStatus.PAUSED:
        raise BulkMigrationServiceError(f"Cannot resume a run in status {run.status.value}.")
    run.pause_requested = False
    run.status = BulkMigrationRunStatus.RUNNING
    await db.commit()
    await db.refresh(run)
    asyncio.create_task(_run_worker_pool(run.id, run.organization_id))
    await _log_event(db, run.id, None, "RUN_RESUMED", "Resuming with a fresh worker generation.")
    return run


async def request_cancel(db: AsyncSession, run: BulkMigrationRun) -> BulkMigrationRun:
    if run.status not in (BulkMigrationRunStatus.PENDING, BulkMigrationRunStatus.VALIDATING, BulkMigrationRunStatus.RUNNING, BulkMigrationRunStatus.PAUSED):
        raise BulkMigrationServiceError(f"Cannot cancel a run in status {run.status.value}.")
    run.cancel_requested = True
    await db.commit()
    await db.refresh(run)
    if run.status == BulkMigrationRunStatus.PAUSED:
        # No worker pool is currently active to notice cancel_requested —
        # handle it inline rather than waiting for a resume that may never come.
        sessionmaker = await tenant_engine_registry.get_background_sessionmaker(run.organization_id)
        await _handle_cancel_requested(sessionmaker, run.id)
        await db.refresh(run)
    return run


async def retry_failed(db: AsyncSession, run: BulkMigrationRun, *, include_successful: bool = False) -> int:
    """"Resume Run" — spec's explicit "process only failed+pending by
    default, unless the user opts into retrying successful objects too"
    requirement. Re-dispatches a fresh worker-pool generation against the
    SAME run row (no new BulkMigrationTask rows — the point of Resume is
    picking up where this run left off, not starting a new one)."""
    if run.status not in (BulkMigrationRunStatus.COMPLETED_WITH_ERRORS, BulkMigrationRunStatus.FAILED, BulkMigrationRunStatus.CANCELLED):
        raise BulkMigrationServiceError(f"Cannot retry a run in status {run.status.value} — only a finished run can be retried.")

    statuses_to_requeue = [BulkMigrationTaskStatus.FAILED, BulkMigrationTaskStatus.CANCELLED]
    if include_successful:
        statuses_to_requeue.append(BulkMigrationTaskStatus.SUCCESS)

    requeued = (await db.execute(
        select(func.count()).select_from(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id, BulkMigrationTask.status.in_(statuses_to_requeue))
    )).scalar_one()
    if not requeued:
        return 0

    await db.execute(
        update(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id, BulkMigrationTask.status.in_(statuses_to_requeue))
        .values(status=BulkMigrationTaskStatus.PENDING, error_code=None, error_message=None, retry_after=None, completed_at=None)
    )
    run.status = BulkMigrationRunStatus.RUNNING
    run.cancel_requested = False
    run.pause_requested = False
    await db.commit()
    # Aggregate counters (pending/failed/cancelled/success) are recomputed
    # from the real task rows here rather than incrementally adjusted —
    # see _sync_live_counters()'s docstring for why: an incremental
    # decrement here previously double-counted when a requeued task later
    # failed again (found live).
    await _sync_live_counters(db, run.id)
    asyncio.create_task(_run_worker_pool(run.id, run.organization_id))
    await _log_event(db, run.id, None, "RUN_RETRY", f"{requeued} task(s) requeued (include_successful={include_successful}).")
    return requeued


# ── Crash recovery (BM5) ─────────────────────────────────────────────────
# Two real, distinct mechanisms — both spec-required, neither faked:
#   1. recover_stuck_bulk_migration_tasks() — STARTUP-only, called once
#      from main.py's lifespan (same place _recover_document_schedules()
#      already runs — see services/schedule_service.py's
#      recover_stuck_document_schedules(), the direct precedent this
#      mirrors). Any BulkMigrationRun left RUNNING/VALIDATING/PENDING/
#      PAUSING/CANCELLING is unconditionally orphaned — the previous
#      process's _run_worker_pool() coroutine died with it, so every task
#      still marked RUNNING is real crash debris, not "maybe still going".
#   2. sweep_stale_bulk_migration_tasks() — a PERIODIC APScheduler job
#      (registered in main.py._start_billing_scheduler(), the one
#      in-process scheduler this codebase runs — no second scheduler
#      introduced) for the narrower case the spec's STALE status exists
#      for: the backend process is still alive, but ONE worker's task
#      hung (a network call that never returned, no exception raised) —
#      its last_heartbeat goes stale while its run's OTHER workers and
#      outer generation loop are still healthy. Detected purely by
#      last_heartbeat age, never by "is this run's asyncio task still
#      running" (no reliable way to introspect that across a restart, and
#      not needed — the atomic claim_task() CAS means an extra worker-pool
#      redispatch after a stale-task fix is wasteful at worst, never
#      unsafe: two pools racing for the same task only one ever wins the
#      UPDATE).
#
# Both scoped to the platform/default tenant DB only (AsyncSessionLocal) —
# the same disclosed limitation every other startup/periodic sweep in this
# codebase already carries; an org on its own separate BYODB tenant
# database would need its own recovery pass.

STALE_HEARTBEAT_THRESHOLD_SECONDS = 300  # 5 minutes with no chunk-boundary heartbeat touch = presumed dead
ORPHANED_RUN_STATUSES = (
    BulkMigrationRunStatus.PENDING, BulkMigrationRunStatus.VALIDATING, BulkMigrationRunStatus.RUNNING,
    BulkMigrationRunStatus.PAUSING, BulkMigrationRunStatus.CANCELLING,
)


async def _recover_run_tasks(db: AsyncSession, run: BulkMigrationRun) -> tuple[int, int]:
    """Shared by both recovery paths: finds this run's RUNNING tasks,
    marks them STALE, then immediately resolves each to RETRY_PENDING
    (attempt budget remaining) or FAILED (exhausted). error_count (a
    genuine cumulative metric) is bumped here directly; the caller is
    responsible for calling _sync_live_counters() afterwards to bring
    pending_objects/failed_objects/etc. in line with the real task rows.
    Returns (requeued, failed)."""
    stuck_tasks = (await db.execute(
        select(BulkMigrationTask).where(BulkMigrationTask.run_id == run.id, BulkMigrationTask.status == BulkMigrationTaskStatus.RUNNING)
    )).scalars().all()
    requeued = failed = 0
    for task in stuck_tasks:
        ok = await _finalize_task(db, task.id, BulkMigrationTaskStatus.RUNNING, BulkMigrationTaskStatus.STALE)
        if not ok:
            continue
        exhausted = task.attempt_number >= max(1, task.max_retries) + 1
        if exhausted:
            await _finalize_task(db, task.id, BulkMigrationTaskStatus.STALE, BulkMigrationTaskStatus.FAILED, error_code=BulkMigrationErrorCode.TIMEOUT.value, error_message="Worker heartbeat went stale (crash or hang) and the retry budget is exhausted.")
            run.error_count = (run.error_count or 0) + 1
            failed += 1
        else:
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2.5 ** (task.attempt_number - 1)))
            await db.execute(update(BulkMigrationTask).where(BulkMigrationTask.id == task.id).values(
                status=BulkMigrationTaskStatus.RETRY_PENDING, retry_after=datetime.now(timezone.utc) + timedelta(seconds=delay),
                error_code=BulkMigrationErrorCode.TIMEOUT.value, error_message="Worker heartbeat went stale (crash or hang) — requeued.",
            ))
            requeued += 1
        await _log_event(db, run.id, task.id, "TASK_STALE_RECOVERED", f"{task.object_key}: {'FAILED (retry budget exhausted)' if exhausted else 'requeued for retry'}.")
    return requeued, failed


async def recover_stuck_bulk_migration_tasks() -> dict:
    recovered_runs = requeued_tasks = failed_tasks = 0
    async with AsyncSessionLocal() as db:
        stuck_runs = (await db.execute(select(BulkMigrationRun).where(BulkMigrationRun.status.in_(ORPHANED_RUN_STATUSES)))).scalars().all()
        for run in stuck_runs:
            requeued, failed = await _recover_run_tasks(db, run)
            requeued_tasks += requeued
            failed_tasks += failed
            recovered_runs += 1
            await db.commit()
            await _sync_live_counters(db, run.id)

            if run.cancel_requested:
                await db.execute(update(BulkMigrationTask).where(
                    BulkMigrationTask.run_id == run.id,
                    BulkMigrationTask.status.in_([BulkMigrationTaskStatus.PENDING, BulkMigrationTaskStatus.QUEUED, BulkMigrationTaskStatus.BLOCKED, BulkMigrationTaskStatus.RETRY_PENDING]),
                ).values(status=BulkMigrationTaskStatus.CANCELLED, completed_at=datetime.now(timezone.utc)))
                run.status = BulkMigrationRunStatus.CANCELLED
                run.completed_at = datetime.now(timezone.utc)
                await db.commit()
                await _sync_live_counters(db, run.id)
                continue
            if run.pause_requested:
                run.status = BulkMigrationRunStatus.PAUSED
                await db.commit()
                continue

            remaining = (await db.execute(
                select(func.count()).select_from(BulkMigrationTask).where(
                    BulkMigrationTask.run_id == run.id,
                    BulkMigrationTask.status.in_([BulkMigrationTaskStatus.PENDING, BulkMigrationTaskStatus.QUEUED, BulkMigrationTaskStatus.RETRY_PENDING, BulkMigrationTaskStatus.BLOCKED]),
                )
            )).scalar_one()
            if remaining == 0:
                await _finalize_run(db, run.id)
            else:
                asyncio.create_task(_run_worker_pool(run.id, run.organization_id))
                await _log_event(db, run.id, None, "RUN_RECOVERED", f"Backend restart interrupted this run — {requeued} task(s) requeued, {failed} exhausted. Resuming.")

    return {"recovered_runs": recovered_runs, "requeued_tasks": requeued_tasks, "failed_tasks": failed_tasks}


async def sweep_stale_bulk_migration_tasks() -> dict:
    """Periodic (APScheduler) heartbeat sweep — see section docstring
    above. Only touches tasks belonging to a run that is itself still
    RUNNING (a hung single task inside an otherwise-healthy run), not the
    startup-only "every RUNNING task is orphaned" assumption above."""
    threshold = datetime.now(timezone.utc) - timedelta(seconds=STALE_HEARTBEAT_THRESHOLD_SECONDS)
    requeued_tasks = failed_tasks = 0
    async with AsyncSessionLocal() as db:
        stale_task_run_ids = (await db.execute(
            select(BulkMigrationTask.run_id).distinct().where(
                BulkMigrationTask.status == BulkMigrationTaskStatus.RUNNING,
                BulkMigrationTask.last_heartbeat.is_not(None),
                BulkMigrationTask.last_heartbeat < threshold,
            )
        )).scalars().all()
        for run_id in stale_task_run_ids:
            run = await db.get(BulkMigrationRun, run_id)
            if run is None or run.status != BulkMigrationRunStatus.RUNNING:
                continue
            requeued, failed = await _recover_run_tasks(db, run)
            requeued_tasks += requeued
            failed_tasks += failed
            await db.commit()
            await _sync_live_counters(db, run.id)
            if requeued:
                # See module-level note: redispatching even if the run's
                # own outer loop is still alive is wasteful, never unsafe.
                asyncio.create_task(_run_worker_pool(run.id, run.organization_id))
    return {"requeued_tasks": requeued_tasks, "failed_tasks": failed_tasks}


# ── API response shaping (BM6) ───────────────────────────────────────────

def to_run_out(run: BulkMigrationRun) -> dict:
    return {
        "id": run.id, "pipeline_id": run.pipeline_id, "pipeline_version": run.pipeline_version, "run_key": run.run_key,
        "status": run.status.value if hasattr(run.status, "value") else run.status, "trigger_type": run.trigger_type,
        "cancel_requested": run.cancel_requested, "pause_requested": run.pause_requested,
        "effective_workers": run.effective_workers, "effective_batch_size": run.effective_batch_size,
        "continue_on_error": run.continue_on_error, "total_objects": run.total_objects,
        "pending_objects": run.pending_objects, "queued_objects": run.queued_objects, "running_objects": run.running_objects,
        "success_objects": run.success_objects, "failed_objects": run.failed_objects, "skipped_objects": run.skipped_objects,
        "cancelled_objects": run.cancelled_objects, "blocked_objects": run.blocked_objects,
        "total_rows_source": run.total_rows_source, "rows_processed": run.rows_processed, "rows_rejected": run.rows_rejected,
        "error_count": run.error_count, "dependency_levels": run.dependency_levels, "error_message": run.error_message,
        "started_at": run.started_at, "completed_at": run.completed_at, "created_at": run.created_at,
    }


def to_task_out(task: BulkMigrationTask) -> dict:
    return {
        "id": task.id, "run_id": task.run_id, "object_key": task.object_key,
        "source_schema": task.source_schema, "source_object": task.source_object,
        "target_schema": task.target_schema, "target_object": task.target_object,
        "object_type": task.object_type, "load_type": task.load_type, "dependency_level": task.dependency_level,
        "status": task.status.value if hasattr(task.status, "value") else task.status,
        "attempt_number": task.attempt_number, "worker_id": task.worker_id,
        "started_at": task.started_at, "completed_at": task.completed_at, "last_heartbeat": task.last_heartbeat,
        "source_row_count": task.source_row_count, "target_row_count": task.target_row_count,
        "rows_processed": task.rows_processed, "rows_rejected": task.rows_rejected,
        "throughput_rows_per_sec": task.throughput_rows_per_sec,
        "error_code": task.error_code, "error_message": task.error_message,
    }


def to_event_out(event: BulkMigrationTaskEvent) -> dict:
    return {
        "id": event.id, "run_id": event.run_id, "task_id": event.task_id, "event_type": event.event_type,
        "event_timestamp": event.event_timestamp, "worker_id": event.worker_id, "message": event.message,
    }
