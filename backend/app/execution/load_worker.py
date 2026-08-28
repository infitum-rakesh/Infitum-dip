"""
load_worker — runs a single TargetLoadRun to completion (Phase 6). Same
"fetch a bounded chunk, act on it, THEN advance the checkpoint counters"
discipline as execution/worker.py: records_written only advances after a
batch's write_batch() call actually returns, never before, so a crash
mid-batch under-reports rather than over-reports what really landed.

Two real read strategies (JJ3 — see models/target_load_run.py's docstring
for the full rationale):

run_load() — SQL_STREAM mode (original, unchanged). The source side is a
single open SQL cursor (open_sql_stream, exactly like execution/worker.py's
run_query_partition), so a retried/failed run always restarts the whole
stream from the top, not from wherever it left off — no per-batch resume
point here, same disclosed limitation QUERY-mode extraction already has.

run_key_range_partition() — KEY_RANGE mode (new). One partition = one
contiguous key range, checkpointed and safe to run concurrently with
sibling partitions — mirrors execution/worker.py's run_partition() for
extraction, except each chunk is written straight to the TARGET here
(writer.write_batch()) instead of staged to a local file, and the
checkpoint (last_key_read) only advances after that write actually lands.
This is what makes a 100M+ row load survive a restart instead of
starting over from row 0 — see execution/load_executor.py for the
partition-planning orchestration that drives this per-partition worker.

Both paths write via loading/writers/base.py's write_batch_with_fallback()
(KK2/KK3), not the writer's raw write_batch() directly: a bulk-insert
failure on a chunk falls back to inserting that chunk row-by-row so good
rows still land, and each row that still fails comes back with its own
real error — persisted as a RejectedRecord (app/services/reject_service.py)
and reflected in records_rejected here and on the run, instead of either
failing the whole chunk on one bad row or silently dropping it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import update

from app.connectors.base import scrub_secrets
from app.database import tenant_engine_registry
from app.execution.checkpoint import decode_key, encode_key
from app.execution.retry import with_retry
from app.loading.writers.base import write_batch_with_fallback
from app.models.pipeline_run import PartitionStatus
from app.models.rejected_record import RejectSourceType
from app.models.target_load_run import LoadStatus, TargetLoadRun, TargetLoadRunPartition, WriteMode
from app.services import reject_service


async def run_load(
    source_connector, writer, run: TargetLoadRun, *,
    sql: str, target_schema: str | None, target_object: str, columns: list[str],
    cancel_check,
) -> None:
    await _mark_started(run.id, run.organization_id)

    # A failure anywhere here could come from either side (source read or
    # target write) — scrub against both connectors' secrets, not just one.
    def _scrub(exc) -> str:
        return scrub_secrets(str(exc), source_connector.secrets, writer.connector.secrets)

    if run.write_mode == WriteMode.TRUNCATE_INSERT:
        try:
            await asyncio.to_thread(writer.truncate, target_schema, target_object)
        except Exception as exc:
            await _mark_failed(run.id, f"Could not truncate target before load: {_scrub(exc)}", run.organization_id)
            return

    key_columns = run.key_columns if run.write_mode == WriteMode.UPSERT else None

    stream = None
    try:
        async def _open():
            return await asyncio.to_thread(source_connector.open_sql_stream, sql, {})
        stream = await with_retry(_open, on_retry=_log_retry(run.id, "open"))

        while True:
            if cancel_check(run.id):
                await _mark_status(run.id, LoadStatus.CANCELLED, run.organization_id)
                return

            async def _fetch():
                return await asyncio.to_thread(stream.fetch_chunk, run.chunk_size)
            rows = await with_retry(_fetch, on_retry=_log_retry(run.id, "fetch"))

            if not rows:
                break
            await _advance_read(run.id, len(rows), run.organization_id)

            async def _write():
                return await asyncio.to_thread(write_batch_with_fallback, writer, target_schema, target_object, columns, rows, key_columns)
            written, rejects = await with_retry(_write, on_retry=_log_retry(run.id, "write"))
            await _advance_written(run.id, written, len(rejects), run.organization_id)
            if rejects:
                await reject_service.record_rejects(
                    organization_id=run.organization_id, source_type=RejectSourceType.TARGET_LOAD, source_id=run.id,
                    node_id=None, target_object=target_object, rejects=rejects,
                )

            if len(rows) < run.chunk_size:
                break

        await _mark_status(run.id, LoadStatus.SUCCEEDED, run.organization_id)
    except Exception as exc:
        await _mark_failed(run.id, _scrub(exc), run.organization_id)
    finally:
        if stream is not None:
            await asyncio.to_thread(stream.close)


async def run_key_range_partition(
    source_connector, writer, run: TargetLoadRun, partition: TargetLoadRunPartition, *,
    source_schema: str | None, source_object: str, target_schema: str | None, target_object: str,
    columns: list[str], key_columns: list[str] | None, row_transform,
    cancel_check,
) -> None:
    """KEY_RANGE mode — see module docstring. `row_transform`, when given,
    is applied to each fetched source-keyed row dict before it's written
    (renames source field names to mapped target column names, applies
    constant_value/default_value — see mapping/mapper.py's build_soql());
    None means the fetched rows already match `columns` as-is."""
    await _mark_started_partition(partition.id, run.organization_id)

    after_value = (
        decode_key(partition.last_key_read, run.key_type) if partition.last_key_read
        else decode_key(partition.range_start, run.key_type)
    )
    upper_bound = decode_key(partition.range_end, run.key_type)

    try:
        while True:
            if cancel_check(run.id):
                await _mark_partition_status(partition.id, PartitionStatus.CANCELLED, run.organization_id)
                return

            async def _read():
                return await asyncio.to_thread(
                    source_connector.read_key_range_chunk,
                    source_object, source_schema, run.key_column,
                    after_value, upper_bound, run.chunk_size,
                )
            chunk = await with_retry(_read, on_retry=_log_retry(run.id, f"partition-{partition.partition_index}-read"))

            if chunk.count == 0:
                break

            rows = [row_transform(r) for r in chunk.rows] if row_transform else chunk.rows

            async def _write():
                return await asyncio.to_thread(write_batch_with_fallback, writer, target_schema, target_object, columns, rows, key_columns)
            written, rejects = await with_retry(_write, on_retry=_log_retry(run.id, f"partition-{partition.partition_index}-write"))

            after_value = chunk.last_key
            # Same ordering rule as everywhere else in this codebase: the
            # checkpoint only advances after the write it's vouching for
            # has actually landed, never before.
            await _advance_partition(partition.id, run.id, encode_key(after_value), chunk.count, written, run.organization_id, len(rejects))
            if rejects:
                await reject_service.record_rejects(
                    organization_id=run.organization_id, source_type=RejectSourceType.TARGET_LOAD, source_id=run.id,
                    node_id=None, target_object=target_object, rejects=rejects,
                )

            if chunk.count < run.chunk_size:
                break  # short chunk — this was the last one in range

        await _mark_partition_status(partition.id, PartitionStatus.COMPLETE, run.organization_id)
    except Exception as exc:
        message = scrub_secrets(str(exc), source_connector.secrets, writer.connector.secrets)
        await _mark_partition_failed(partition.id, message, run.organization_id)


def _log_retry(run_id: int, stage: str):
    def _cb(attempt: int, delay: float, exc: Exception):
        print(f"[load] run={run_id} {stage} retry {attempt} in {delay:.1f}s after: {exc}")
    return _cb


async def _mark_started_partition(partition_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(TargetLoadRunPartition).where(TargetLoadRunPartition.id == partition_id)
            .values(status=PartitionStatus.RUNNING, started_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _advance_partition(
    partition_id: int, run_id: int, last_key: str | None, read_count: int, written_count: int,
    organization_id: int | None, rejected_count: int = 0,
) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(TargetLoadRunPartition).where(TargetLoadRunPartition.id == partition_id)
            .values(
                last_key_read=last_key,
                records_read=TargetLoadRunPartition.records_read + read_count,
                records_written=TargetLoadRunPartition.records_written + written_count,
                records_rejected=TargetLoadRunPartition.records_rejected + rejected_count,
            )
        )
        # Atomic increments on the parent run's own counters too — same
        # rule as execution/worker.py: multiple sibling partitions update
        # this row concurrently, so it has to be a SQL-side increment, not
        # a Python read-modify-write.
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id).values(
                records_read=TargetLoadRun.records_read + read_count,
                records_written=TargetLoadRun.records_written + written_count,
                records_rejected=TargetLoadRun.records_rejected + rejected_count,
            )
        )
        await db.commit()


async def _mark_partition_status(partition_id: int, status: PartitionStatus, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(TargetLoadRunPartition).where(TargetLoadRunPartition.id == partition_id)
            .values(status=status, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _mark_partition_failed(partition_id: int, message: str, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(TargetLoadRunPartition).where(TargetLoadRunPartition.id == partition_id)
            .values(status=PartitionStatus.FAILED, error_message=message, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _mark_started(run_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(status=LoadStatus.RUNNING, started_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _advance_read(run_id: int, count: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(records_read=TargetLoadRun.records_read + count)
        )
        await db.commit()


async def _advance_written(run_id: int, count: int, rejected_count: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(
                records_written=TargetLoadRun.records_written + count,
                records_rejected=TargetLoadRun.records_rejected + rejected_count,
            )
        )
        await db.commit()


async def _mark_status(run_id: int, status: LoadStatus, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(status=status, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _mark_failed(run_id: int, message: str, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(status=LoadStatus.FAILED, error_message=message, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()
