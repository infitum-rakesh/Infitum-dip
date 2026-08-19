"""
PipelineExecutor — real backend execution engine for Source Dataset
extraction (Phase 3 of the data-integration build).

Runs as an asyncio background task per run (start_run fires
asyncio.create_task and returns immediately) — never inside the request/
response cycle, never in the browser. See models/pipeline_run.py for the
current scope boundary: this executes ONE Source Dataset node's
extraction to a local staging file, not a full source-to-target pipeline
(transform/validate/target-load execution isn't real yet).

Known limitation, disclosed rather than hidden: this is a single-process,
in-memory task runner, not a distributed worker queue. If the API process
restarts mid-run, the in-flight asyncio task is lost — but every
partition's checkpoint (last_key_read) already committed to the DB
survives, so calling Resume after a restart picks up correctly from
exactly where each partition left off; it just doesn't happen
automatically without that call. A production deployment would swap this
module's start_run() for enqueueing onto a real worker queue (Celery/RQ/
arq) without changing worker.py or anything upstream of it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.database.connection import AsyncSessionLocal
from app.execution.checkpoint import encode_key, infer_key_type
from app.execution.worker import run_file_partition, run_netsuite_search_partition, run_partition, run_query_partition
from app.ingestion.partition_reader import plan_partitions
from app.loading import staging
from app.models.pipeline_run import PartitionStatus, PipelineRun, PipelineRunPartition, RunStatus
from app.services.connection_service import build_connector, get_connection

STAGING_BASE_PATH = "staging_data"  # relative to the backend process cwd

# In-process run bookkeeping — see module docstring for why this doesn't
# survive a restart, and why that's an acceptable, disclosed limitation.
_running_tasks: dict[int, asyncio.Task] = {}
_cancelled: set[int] = set()


def is_running(run_id: int) -> bool:
    return run_id in _running_tasks


def is_cancelled(run_id: int) -> bool:
    return run_id in _cancelled


def request_cancel(run_id: int) -> bool:
    if run_id not in _running_tasks:
        return False
    _cancelled.add(run_id)
    return True


async def start_run(run_id: int) -> None:
    """Fire-and-forget from the caller's perspective — the API route that
    calls this does not (and must not) await it; it returns the run's
    PENDING status immediately and the frontend polls GET /runs/{id}."""
    task = asyncio.create_task(_execute(run_id))
    _running_tasks[run_id] = task
    task.add_done_callback(lambda _t: _running_tasks.pop(run_id, None))


async def _execute(run_id: int) -> None:
    async with AsyncSessionLocal() as db:
        run = await db.get(PipelineRun, run_id)
        # Defense in depth: re-verify the connection genuinely belongs to
        # this run's OWN organization_id at execute time too, not just at
        # creation (execution_service.create_run) — protects any row that
        # predates that check, or reached the DB by another path.
        conn = await get_connection(db, run.connection_id, run.organization_id)
        run.status = RunStatus.RUNNING
        run.started_at = run.started_at or datetime.now(timezone.utc)
        await db.commit()

    if conn is None:
        async with AsyncSessionLocal() as db:
            r = await db.get(PipelineRun, run_id)
            r.status = RunStatus.FAILED
            r.error_message = f"Connection {run.connection_id} was not found or no longer belongs to this organization."
            r.completed_at = datetime.now(timezone.utc)
            await db.commit()
        return

    connector = None
    try:
        connector = build_connector(conn)
        kind = run.source_kind or ("QUERY" if run.custom_sql else "TABLE")  # older rows predate source_kind — infer the same way create_run used to imply it
        if kind == "FILE":
            await _run_files(run_id, run, connector)
        elif kind == "QUERY":
            await _run_query(run_id, run, connector)
        elif kind == "NETSUITE_SEARCH":
            await _run_netsuite_search(run_id, run, connector)
        else:
            await _run_partitioned(run_id, run, connector)
        await _finalize(run_id)
    except Exception as exc:
        # connector may be None if build_connector() itself threw (before
        # decrypted secrets were ever attached) — nothing to scrub against
        # in that narrow case, but every other failure path has a real
        # connector to redact secrets through.
        message = connector._scrub(str(exc)) if connector is not None else str(exc)[:300]
        async with AsyncSessionLocal() as db:
            r = await db.get(PipelineRun, run_id)
            r.status = RunStatus.FAILED
            r.error_message = message
            r.completed_at = datetime.now(timezone.utc)
            await db.commit()
    finally:
        _cancelled.discard(run_id)


async def _run_partitioned(run_id: int, run: PipelineRun, connector) -> None:
    partitions = await _load_or_plan_partitions(run_id, run, connector)
    todo = [p for p in partitions if p.status != PartitionStatus.COMPLETE]
    sem = asyncio.Semaphore(max(1, run.worker_count))

    async def _bounded(p):
        async with sem:
            await run_partition(connector, run, p, cancel_check=is_cancelled, staging_base=STAGING_BASE_PATH)

    await asyncio.gather(*[_bounded(p) for p in todo])


async def _run_query(run_id: int, run: PipelineRun, connector) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(PipelineRunPartition).where(PipelineRunPartition.run_id == run_id))
        partitions = list(result.scalars().all())
        if not partitions:
            p = PipelineRunPartition(run_id=run_id, partition_index=0)
            db.add(p)
            await db.commit()
            await db.refresh(p)
            partitions = [p]

    partition = partitions[0]
    if partition.status == PartitionStatus.COMPLETE:
        return  # already done — Resume on a finished QUERY run is a no-op
    await run_query_partition(connector, run, partition, cancel_check=is_cancelled, staging_base=STAGING_BASE_PATH)


async def _run_files(run_id: int, run: PipelineRun, connector) -> None:
    """FILE source path — one partition per matched file, run
    concurrently up to worker_count at a time, same bounded-concurrency
    shape as the TABLE/VIEW key-range path."""
    partitions = await _load_or_plan_file_partitions(run_id, run, connector)
    todo = [p for p in partitions if p.status != PartitionStatus.COMPLETE]
    sem = asyncio.Semaphore(max(1, run.worker_count))

    async def _bounded(p):
        async with sem:
            await run_file_partition(connector, run, p, cancel_check=is_cancelled, staging_base=STAGING_BASE_PATH)

    await asyncio.gather(*[_bounded(p) for p in todo])


async def _run_netsuite_search(run_id: int, run: PipelineRun, connector) -> None:
    """NetSuite SOAP saved-search path — one partition per real result
    page, same bounded-concurrency shape as TABLE/FILE mode. See
    execution/worker.py's run_netsuite_search_partition()."""
    partitions = await _load_or_plan_netsuite_partitions(run_id, run, connector)
    todo = [p for p in partitions if p.status != PartitionStatus.COMPLETE]
    sem = asyncio.Semaphore(max(1, run.worker_count))

    async def _bounded(p):
        async with sem:
            await run_netsuite_search_partition(connector, run, p, cancel_check=is_cancelled, staging_base=STAGING_BASE_PATH)

    await asyncio.gather(*[_bounded(p) for p in todo])


async def _load_or_plan_netsuite_partitions(run_id: int, run: PipelineRun, connector) -> list[PipelineRunPartition]:
    """Plans one partition per real NetSuite result page. Page 1 is
    fetched HERE (not deferred to the worker) purely to learn total_pages
    from NetSuite's own real response — since that page's rows are
    already in hand, they're written straight to partition 0's staging
    file and that partition is marked COMPLETE immediately, so the run
    doesn't pay for page 1 twice (see run_netsuite_search_partition's
    docstring on why each OTHER page's fetch is independently
    self-contained instead of sharing a searchId)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PipelineRunPartition).where(PipelineRunPartition.run_id == run_id).order_by(PipelineRunPartition.partition_index)
        )
        partitions = list(result.scalars().all())
        if partitions:
            return partitions  # already planned (e.g. this is a Resume)

        rows, _total_records, total_pages = await asyncio.to_thread(
            connector.run_saved_search_page, run.schema_name, run.object_name, 1, run.chunk_size,
        )
        total_pages = max(1, total_pages)

        new_partitions = [PipelineRunPartition(run_id=run_id, partition_index=i, range_start=str(i + 1)) for i in range(total_pages)]
        db.add_all(new_partitions)
        r = await db.get(PipelineRun, run_id)
        r.partition_count = len(new_partitions)
        await db.commit()
        for p in new_partitions:
            await db.refresh(p)

    # Page 1's rows are already fetched — land them directly instead of
    # making run_netsuite_search_partition() re-fetch page 1 a second time.
    first_partition_index = new_partitions[0].partition_index
    if rows:
        await asyncio.to_thread(staging.write_chunk, STAGING_BASE_PATH, run_id, first_partition_index, rows)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(PipelineRunPartition).where(PipelineRunPartition.id == new_partitions[0].id)
            .values(status=PartitionStatus.COMPLETE, records_read=len(rows), started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc))
        )
        await db.execute(
            update(PipelineRun).where(PipelineRun.id == run_id).values(
                records_read=PipelineRun.records_read + len(rows), records_written=PipelineRun.records_written + len(rows),
            )
        )
        await db.commit()
    return new_partitions


async def _load_or_plan_file_partitions(run_id: int, run: PipelineRun, connector) -> list[PipelineRunPartition]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PipelineRunPartition).where(PipelineRunPartition.run_id == run_id).order_by(PipelineRunPartition.partition_index)
        )
        partitions = list(result.scalars().all())
        if partitions:
            return partitions  # already planned (e.g. this is a Resume) — never re-plan and duplicate partitions

        pattern = run.object_name or None
        files = await asyncio.to_thread(connector.list_files, pattern)

        # Real, if partial, duplicate-file detection: hash every matched
        # file up front, then skip any whose checksum already appears on a
        # COMPLETE partition from an earlier run of this same pipeline
        # node — a file re-delivered under a new name (or re-picked-up
        # because an archive move failed) doesn't get re-ingested.
        seen_checksums = await _completed_checksums_for_node(run.pipeline_id, run.node_id, exclude_run_id=run_id)

        new_partitions: list[PipelineRunPartition] = []
        skipped: list[str] = []
        index = 0
        for f in files:
            try:
                checksum = await asyncio.to_thread(connector.file_checksum, await asyncio.to_thread(connector.resolve_path, f.name))
            except Exception as exc:
                # Checksumming failed (permissions, transient I/O) — don't
                # silently drop the file from the run; plan it anyway with
                # no checksum on record, same as a checksum genuinely
                # never having been computed for it.
                print(f"[execution] run={run_id} file '{f.name}' checksum failed, planning without dedup check: {exc}")
                checksum = None
            if checksum and checksum in seen_checksums:
                skipped.append(f.name)
                continue
            new_partitions.append(PipelineRunPartition(run_id=run_id, partition_index=index, file_name=f.name, file_checksum=checksum))
            index += 1

        if skipped:
            print(f"[execution] run={run_id} skipped {len(skipped)} duplicate file(s) by checksum: {skipped}")

        db.add_all(new_partitions)
        r = await db.get(PipelineRun, run_id)
        r.partition_count = len(new_partitions)
        await db.commit()
        for p in new_partitions:
            await db.refresh(p)
        return new_partitions


async def _completed_checksums_for_node(pipeline_id: int, node_id: str, *, exclude_run_id: int) -> set[str]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PipelineRunPartition.file_checksum)
            .join(PipelineRun, PipelineRun.id == PipelineRunPartition.run_id)
            .where(
                PipelineRun.pipeline_id == pipeline_id, PipelineRun.node_id == node_id,
                PipelineRun.id != exclude_run_id,
                PipelineRunPartition.status == PartitionStatus.COMPLETE,
                PipelineRunPartition.file_checksum.is_not(None),
            )
        )
        return {row[0] for row in result.all()}


async def _load_or_plan_partitions(run_id: int, run: PipelineRun, connector) -> list[PipelineRunPartition]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PipelineRunPartition).where(PipelineRunPartition.run_id == run_id).order_by(PipelineRunPartition.partition_index)
        )
        partitions = list(result.scalars().all())
        if partitions:
            return partitions

        min_v, max_v = await asyncio.to_thread(connector.key_range, run.object_name, run.schema_name, run.key_column)
        key_type = infer_key_type(min_v) if min_v is not None else "str"
        ranges = plan_partitions(min_v, max_v, key_type, run.partition_count)

        new_partitions = [
            PipelineRunPartition(run_id=run_id, partition_index=i, range_start=encode_key(s), range_end=encode_key(e))
            for i, (s, e) in enumerate(ranges)
        ]
        db.add_all(new_partitions)

        r = await db.get(PipelineRun, run_id)
        r.key_type = key_type
        r.partition_count = len(new_partitions)
        await db.commit()
        for p in new_partitions:
            await db.refresh(p)
        return new_partitions


async def _finalize(run_id: int) -> None:
    async with AsyncSessionLocal() as db:
        run = await db.get(PipelineRun, run_id)
        result = await db.execute(select(PipelineRunPartition).where(PipelineRunPartition.run_id == run_id))
        partitions = list(result.scalars().all())

        if is_cancelled(run_id) or any(p.status == PartitionStatus.CANCELLED for p in partitions):
            run.status = RunStatus.CANCELLED
        elif any(p.status == PartitionStatus.FAILED for p in partitions):
            run.status = RunStatus.FAILED
            failed = next(p for p in partitions if p.status == PartitionStatus.FAILED)
            run.error_message = f"Partition {failed.partition_index}: {failed.error_message}"
        else:
            run.status = RunStatus.SUCCEEDED
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
