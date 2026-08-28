"""
worker — runs a single partition to completion: read one chunk, write it
to staging, THEN advance the checkpoint — never reversed. If the process
dies between the write and the checkpoint update, the worst case on
resume is re-reading and re-appending one chunk's worth of rows (a small,
bounded duplication window at the tail of a partition's staging file),
which is far safer than advancing the checkpoint before the write lands
and silently losing that chunk forever. This mirrors the platform-wide
rule from Settings/OrganizationSettings and the watermark spec: never
mark something done before the thing that makes it true has landed.

Checkpoint/metric updates use atomic SQL increments (UPDATE ... SET
records_read = records_read + :n), not read-modify-write in Python —
multiple partitions of the same run update the same PipelineRun row
concurrently, and a read-modify-write there would lose updates under
concurrent commits. See database/connection.py for the WAL + busy_timeout
pragmas this concurrency also depends on.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import update

from app.database import tenant_engine_registry
from app.execution.checkpoint import decode_key, encode_key
from app.execution.retry import with_retry
from app.loading import staging
from app.models.pipeline_run import PartitionStatus, PipelineRun, PipelineRunPartition


async def run_partition(connector, run: PipelineRun, partition: PipelineRunPartition, *, cancel_check, staging_base: str) -> None:
    """TABLE/VIEW key-range path — checkpointed, resumable, safe to run
    concurrently with sibling partitions (each owns a disjoint key range
    and its own staging file, so there's no shared mutable state besides
    the atomic DB counters above)."""
    await _mark_started(partition.id, run.organization_id)

    after_value = (
        decode_key(partition.last_key_read, run.key_type) if partition.last_key_read
        else decode_key(partition.range_start, run.key_type)
    )
    upper_bound = decode_key(partition.range_end, run.key_type)

    try:
        while True:
            if cancel_check(run.id):
                await _mark_status(partition.id, PartitionStatus.CANCELLED, run.organization_id)
                return

            async def _read():
                return await asyncio.to_thread(
                    connector.read_key_range_chunk,
                    run.object_name, run.schema_name, run.key_column,
                    after_value, upper_bound, run.chunk_size,
                )
            chunk = await with_retry(_read, on_retry=_log_retry(run.id, partition.partition_index))

            if chunk.count == 0:
                break

            await asyncio.to_thread(staging.write_chunk, staging_base, run.id, partition.partition_index, chunk.rows)
            after_value = chunk.last_key
            await _advance(partition.id, run.id, encode_key(after_value), chunk.count, run.organization_id)

            if chunk.count < run.chunk_size:
                break  # short chunk — this was the last one in range

        await _mark_status(partition.id, PartitionStatus.COMPLETE, run.organization_id)
    except Exception as exc:
        await _mark_failed(partition.id, connector._scrub(str(exc)), run.organization_id)


async def run_query_partition(connector, run: PipelineRun, partition: PipelineRunPartition, *, cancel_check, staging_base: str) -> None:
    """QUERY (custom SQL) path — a single open cursor for the whole run,
    fetched in bounded chunks. There's no key column to checkpoint against
    arbitrary SQL, so Resume on a QUERY-mode run restarts this partition
    from the beginning (see staging.reset_partition) rather than faking a
    resume point that doesn't exist — a disclosed limitation, not a bug."""
    await _mark_started(partition.id, run.organization_id)
    await asyncio.to_thread(staging.reset_partition, staging_base, run.id, partition.partition_index)

    stream = None
    try:
        async def _open():
            return await asyncio.to_thread(connector.open_sql_stream, run.custom_sql, run.sql_parameters or {})
        stream = await with_retry(_open, on_retry=_log_retry(run.id, partition.partition_index))

        while True:
            if cancel_check(run.id):
                await _mark_status(partition.id, PartitionStatus.CANCELLED, run.organization_id)
                return

            async def _fetch():
                return await asyncio.to_thread(stream.fetch_chunk, run.chunk_size)
            rows = await with_retry(_fetch, on_retry=_log_retry(run.id, partition.partition_index))

            if not rows:
                break
            await asyncio.to_thread(staging.write_chunk, staging_base, run.id, partition.partition_index, rows)
            await _advance(partition.id, run.id, None, len(rows), run.organization_id)
            if len(rows) < run.chunk_size:
                break

        await _mark_status(partition.id, PartitionStatus.COMPLETE, run.organization_id)
    except Exception as exc:
        await _mark_failed(partition.id, connector._scrub(str(exc)), run.organization_id)
    finally:
        if stream is not None:
            await asyncio.to_thread(stream.close)


async def run_file_partition(connector, run: PipelineRun, partition: PipelineRunPartition, *, cancel_check, staging_base: str) -> None:
    """FILE source path (Phase 5) — one partition streams one whole file,
    read in bounded-memory chunks via FileConnectorBase.open_row_stream(),
    the file-mode analogue of open_sql_stream(). Like QUERY mode there is
    no natural mid-file checkpoint, so a retried partition restarts that
    file from row 0 (staging is reset first) — disclosed, not faked as
    resumable. On success, if run.archive_dir is configured, the source
    file is genuinely moved there via connector.move_file() and
    partition.archived is set true only if that move actually succeeds;
    on failure, if run.error_dir is configured, the file is moved there
    instead so a bad file doesn't get silently re-picked-up by the next
    run's file-matching pattern."""
    await _mark_started(partition.id, run.organization_id)
    await asyncio.to_thread(staging.reset_partition, staging_base, run.id, partition.partition_index)

    stream = None
    try:
        async def _open():
            return await asyncio.to_thread(connector.open_row_stream, partition.file_name)
        stream = await with_retry(_open, on_retry=_log_retry(run.id, partition.partition_index))

        while True:
            if cancel_check(run.id):
                await _mark_status(partition.id, PartitionStatus.CANCELLED, run.organization_id)
                return

            async def _fetch():
                return await asyncio.to_thread(stream.fetch_chunk, run.chunk_size)
            rows = await with_retry(_fetch, on_retry=_log_retry(run.id, partition.partition_index))

            if not rows:
                break
            await asyncio.to_thread(staging.write_chunk, staging_base, run.id, partition.partition_index, rows)
            await _advance(partition.id, run.id, None, len(rows), run.organization_id)
            if len(rows) < run.chunk_size:
                break

        await _mark_status(partition.id, PartitionStatus.COMPLETE, run.organization_id)
    except Exception as exc:
        await _mark_failed(partition.id, connector._scrub(str(exc)), run.organization_id)
        await _move_after_run(connector, run.error_dir, partition, run.id, run.organization_id, mark_archived=False)
        return
    finally:
        if stream is not None:
            await asyncio.to_thread(stream.close)

    await _move_after_run(connector, run.archive_dir, partition, run.id, run.organization_id, mark_archived=True)


async def _move_after_run(
    connector, dest_dir: str | None, partition: PipelineRunPartition, run_id: int, organization_id: int | None, *, mark_archived: bool,
) -> None:
    if not dest_dir or not partition.file_name:
        return
    try:
        resolved = await asyncio.to_thread(connector.resolve_path, partition.file_name)
        await asyncio.to_thread(connector.move_file, resolved, dest_dir)
        if mark_archived:
            await _mark_archived(partition.id, organization_id)
    except Exception as exc:
        # The extraction itself already succeeded/failed and is recorded —
        # a failed post-hoc archive/error move is logged, not allowed to
        # overwrite that real outcome or crash the run.
        print(f"[execution] run={run_id} partition={partition.partition_index} move to '{dest_dir}' failed: {exc}")


async def run_netsuite_search_partition(connector, run: PipelineRun, partition: PipelineRunPartition, *, cancel_check, staging_base: str) -> None:
    """NetSuite SOAP saved-search path — one partition per real NetSuite
    result PAGE (its own native pagination), the same "one partition per
    natural, atomic unit of work" shape run_file_partition() above uses
    for one-partition-per-file. run.schema_name holds the NetSuite record
    type, run.object_name the saved search's internal ID (or scriptId) —
    reusing the existing generic columns rather than adding NetSuite-
    specific ones, same convention FILE mode's object_name-as-pattern
    already established. partition.range_start holds this partition's
    1-based page index.

    A page is fetched atomically — NetSuite's SOAP API has no sub-page
    checkpoint to resume from — so a retried partition re-fetches that
    whole page from scratch, same disclosed (not faked) resume limitation
    QUERY/FILE mode already have. Each page fetch is also independently
    self-contained (see NetSuiteSoapConnector.run_saved_search_page: it
    issues its own fresh search() + searchMoreWithId() rather than
    reusing a searchId across partitions) — genuinely correct without a
    shared-state bug, at the cost of re-running the saved search's own
    server-side query once per page rather than once per run; acceptable
    for the bounded result sizes a saved search realistically returns,
    disclosed here rather than silently assumed away."""
    await _mark_started(partition.id, run.organization_id)
    if cancel_check(run.id):
        await _mark_status(partition.id, PartitionStatus.CANCELLED, run.organization_id)
        return

    page_index = int(partition.range_start or (partition.partition_index + 1))
    try:
        async def _fetch():
            return await asyncio.to_thread(
                connector.run_saved_search_page, run.schema_name, run.object_name, page_index, run.chunk_size,
            )
        rows, _total_records, _total_pages = await with_retry(_fetch, on_retry=_log_retry(run.id, partition.partition_index))

        if rows:
            await asyncio.to_thread(staging.write_chunk, staging_base, run.id, partition.partition_index, rows)
            await _advance(partition.id, run.id, None, len(rows), run.organization_id)
        await _mark_status(partition.id, PartitionStatus.COMPLETE, run.organization_id)
    except Exception as exc:
        await _mark_failed(partition.id, connector._scrub(str(exc)), run.organization_id)


def _log_retry(run_id: int, partition_index: int):
    def _cb(attempt: int, delay: float, exc: Exception):
        print(f"[execution] run={run_id} partition={partition_index} retry {attempt} in {delay:.1f}s after: {exc}")
    return _cb


async def _mark_started(partition_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(PipelineRunPartition).where(PipelineRunPartition.id == partition_id)
            .values(status=PartitionStatus.RUNNING, started_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _advance(partition_id: int, run_id: int, last_key_encoded: str | None, chunk_count: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        values = {"records_read": PipelineRunPartition.records_read + chunk_count}
        if last_key_encoded is not None:
            values["last_key_read"] = last_key_encoded
        await db.execute(update(PipelineRunPartition).where(PipelineRunPartition.id == partition_id).values(**values))
        await db.execute(
            update(PipelineRun).where(PipelineRun.id == run_id).values(
                records_read=PipelineRun.records_read + chunk_count,
                records_written=PipelineRun.records_written + chunk_count,  # staging write is 1:1 with read — no transform/filter yet
            )
        )
        await db.commit()


async def _mark_status(partition_id: int, status: PartitionStatus, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(PipelineRunPartition).where(PipelineRunPartition.id == partition_id)
            .values(status=status, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _mark_archived(partition_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(update(PipelineRunPartition).where(PipelineRunPartition.id == partition_id).values(archived=True))
        await db.commit()


async def _mark_failed(partition_id: int, message: str, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        await db.execute(
            update(PipelineRunPartition).where(PipelineRunPartition.id == partition_id)
            .values(status=PartitionStatus.FAILED, error_message=message,
                    retry_count=PipelineRunPartition.retry_count + 1, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()
