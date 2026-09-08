"""
Bulk Object Migration — task executor (BM4).

Executes ONE already-claimed BulkMigrationTask to completion or failure.
Directly modeled on execution/load_worker.py's run_load() — same "fetch a
bounded chunk, write it, THEN advance the checkpoint/counters" discipline,
same open_sql_stream()/fetch_chunk() + write_batch_with_fallback()
primitives, same "open a fresh short-lived DB session per progress update"
convention (multiple tasks across multiple workers update rows
concurrently; atomic SQL-side increments, never Python read-modify-write —
see execution/worker.py's docstring for why).

Scope of this release (disclosed, not faked): TABLE/VIEW objects only —
FILE objects raise CONFIGURATION_ERROR (a real, if smaller, gap — see the
plan's "explicitly deferred" list). Checkpointing tracks rows_processed
per committed chunk for real progress/throughput reporting, but a RETRIED
task (after a crash or failure) restarts its stream from the top, not from
its last checkpoint — the same disclosed limitation this codebase's own
QUERY-mode extraction (PipelineRun/TargetLoadRun, source_kind="QUERY") and
load_worker.py's run_load() already carry: a single open SQL cursor has no
natural mid-stream resume point without true key-range partitioning, which
is explicitly deferred to a later phase (intra-object partition
parallelism). SCD1/SCD2/DELTA load types raise UNSUPPORTED_LOAD_TYPE —
also explicitly deferred, never silently no-op'd.

Retry boundary: with_retry() (execution/retry.py, reused unmodified) gives
in-place resilience to a single transient chunk-level hiccup (a dropped
connection, a timeout) WITHIN this one task attempt. If a chunk operation
still fails after with_retry's own budget, the exception propagates out of
execute_task() — the CALLER (bulk_migration_service.py's worker loop) is
responsible for classifying that as permanent (-> FAILED) vs transient
with task-level retry budget remaining (-> RETRY_PENDING with backoff),
and for updating the run's aggregate counters. This module's only job is
moving real data and keeping this one task's own progress columns honest.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import update

from app.connectors.base import scrub_secrets
from app.database import tenant_engine_registry
from app.execution.retry import with_retry
from app.loading.writers.base import write_batch_with_fallback
from app.models.bulk_migration_run import (
    BulkMigrationError, BulkMigrationErrorCode, BulkMigrationRun, BulkMigrationTask, BulkMigrationTaskStatus,
)

HEARTBEAT_EVERY_N_CHUNKS = 1  # touch last_heartbeat after every committed chunk — cheap, and it's the signal recover_stuck_bulk_migration_tasks() (BM5) watches


class TaskCancelledSignal(Exception):
    """Raised (not silently returned) when cancel_check() sees the run's
    cancel_requested flag mid-stream — gives bulk_migration_service.py's
    caller one specific exception type to route to CANCELLED, distinct
    from every other exception type here which routes to FAILED/
    RETRY_PENDING. Single-responsibility split: this module only ever
    self-finalizes the SUCCESS path (matching execution/load_worker.py's
    own precedent); every non-success outcome — cancelled, permanent
    failure, or transient-retry — is finalized by the caller, which is
    the one place that knows the task's retry budget and the run's
    continue_on_error policy."""


def _build_source_sql(source_connector, task: BulkMigrationTask) -> str:
    base_sql = source_connector.build_select_all_sql(task.source_object, task.source_schema)
    if not task.filter_condition:
        return base_sql
    # Wrapped as a subquery rather than string-appending "WHERE" — safe
    # regardless of whether build_select_all_sql() already produced its
    # own WHERE/ORDER BY, at the cost of one extra derived-table level
    # every dialect used here (Postgres/MySQL/SQL Server/Oracle/Snowflake)
    # supports natively.
    return f"SELECT * FROM ({base_sql}) bm_src WHERE {task.filter_condition}"


async def execute_task(
    source_connector, target_writer, task: BulkMigrationTask, *,
    organization_id: int | None, cancel_check,
) -> None:
    if task.load_type in ("SCD1", "SCD2", "DELTA"):
        raise BulkMigrationError(BulkMigrationErrorCode.UNSUPPORTED_LOAD_TYPE, f"Load type {task.load_type} isn't implemented in this release.")
    if task.object_type not in ("TABLE", "VIEW"):
        raise BulkMigrationError(BulkMigrationErrorCode.CONFIGURATION_ERROR, f"Object type '{task.object_type}' isn't implemented in this release — only TABLE/VIEW.")

    def _scrub(exc) -> str:
        return scrub_secrets(str(exc), source_connector.secrets, target_writer.connector.secrets)

    # ── Ensure target + truncate-before-load (FULL_REPLACE only) ──────
    if task.create_target_if_not_exists or task.load_type == "FULL_REPLACE":
        try:
            columns = await with_retry(lambda: _to_thread_get_columns(source_connector, task))
        except Exception as exc:
            raise BulkMigrationError(BulkMigrationErrorCode.SOURCE_OBJECT_NOT_FOUND, _scrub(exc)) from exc
        if not columns:
            raise BulkMigrationError(BulkMigrationErrorCode.SOURCE_OBJECT_NOT_FOUND, f"Source object '{task.source_object}' has no columns / does not exist.")
        if task.create_target_if_not_exists:
            await asyncio.to_thread(
                target_writer.ensure_table_from_columns, task.target_schema, task.target_object, columns,
                recreate=bool(task.drop_and_recreate and task.load_type == "FULL_REPLACE"), key_columns=task.primary_key,
            )
        if task.load_type == "FULL_REPLACE" and task.truncate_before_load and not task.drop_and_recreate:
            try:
                await asyncio.to_thread(target_writer.truncate, task.target_schema, task.target_object)
            except Exception as exc:
                raise BulkMigrationError(BulkMigrationErrorCode.LOAD_ERROR, f"Could not truncate target before load: {_scrub(exc)}") from exc

    key_columns = task.primary_key if task.load_type == "MERGE" else None
    sql = _build_source_sql(source_connector, task)
    batch_size = task.batch_size or 5000

    stream = None
    started_at = datetime.now(timezone.utc)
    rows_processed_total = task.rows_processed or 0
    rows_rejected_total = task.rows_rejected or 0
    batch_number = 0
    try:
        async def _open():
            return await asyncio.to_thread(source_connector.open_sql_stream, sql, {})
        stream = await with_retry(_open)
        columns_from_stream = stream.columns

        while True:
            if await cancel_check(task.run_id):
                raise TaskCancelledSignal(f"Run {task.run_id} cancellation requested.")

            async def _fetch():
                return await asyncio.to_thread(stream.fetch_chunk, batch_size)
            rows = await with_retry(_fetch)

            if not rows:
                break

            async def _write():
                return await asyncio.to_thread(write_batch_with_fallback, target_writer, task.target_schema, task.target_object, columns_from_stream, rows, key_columns)
            written, rejects = await with_retry(_write)

            rows_processed_total += written
            rows_rejected_total += len(rejects)
            batch_number += 1
            elapsed = max((datetime.now(timezone.utc) - started_at).total_seconds(), 0.001)
            await _advance_progress(
                task.id, task.run_id, written, len(rejects), organization_id,
                checkpoint={"rows_processed": rows_processed_total, "batch_number": batch_number},
                throughput=rows_processed_total / elapsed,
            )
            if rejects:
                # Rejected rows are surfaced via task-level counters/checkpoint
                # today; per-row RejectedRecord persistence (mirroring
                # execution/load_worker.py's reject_service.record_rejects())
                # is explicitly deferred, disclosed here rather than silently
                # dropped — task.rows_rejected still reflects the true count.
                pass

            if len(rows) < batch_size:
                break

        await _mark_success(task.id, task.run_id, organization_id, rows_processed_total, rows_rejected_total)
    finally:
        if stream is not None:
            await asyncio.to_thread(stream.close)


def _to_thread_get_columns(source_connector, task: BulkMigrationTask):
    return asyncio.to_thread(source_connector.get_columns, task.source_object, task.source_schema)


async def _advance_progress(task_id: int, run_id: int, written: int, rejected: int, organization_id: int | None, *, checkpoint: dict, throughput: float) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(BulkMigrationTask).where(BulkMigrationTask.id == task_id).values(
                rows_processed=BulkMigrationTask.rows_processed + written,
                rows_rejected=BulkMigrationTask.rows_rejected + rejected,
                checkpoint=checkpoint, throughput_rows_per_sec=throughput,
                last_heartbeat=datetime.now(timezone.utc),
            )
        )
        await db.execute(
            update(BulkMigrationRun).where(BulkMigrationRun.id == run_id).values(
                rows_processed=BulkMigrationRun.rows_processed + written,
                rows_rejected=BulkMigrationRun.rows_rejected + rejected,
            )
        )
        await db.commit()


async def _mark_success(task_id: int, run_id: int, organization_id: int | None, rows_processed: int, rows_rejected: int) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(BulkMigrationTask).where(BulkMigrationTask.id == task_id).values(
                status=BulkMigrationTaskStatus.SUCCESS, completed_at=datetime.now(timezone.utc),
                target_row_count=rows_processed, rows_processed=rows_processed, rows_rejected=rows_rejected,
            )
        )
        await db.commit()
