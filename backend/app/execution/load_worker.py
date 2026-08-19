"""
load_worker — runs a single TargetLoadRun to completion (Phase 6). Same
"fetch a bounded chunk, act on it, THEN advance the checkpoint counters"
discipline as execution/worker.py: records_written only advances after a
batch's write_batch() call actually returns, never before, so a crash
mid-batch under-reports rather than over-reports what really landed.

There is no per-batch resume point here (see models/target_load_run.py
docstring) — the source side is a single open SQL cursor
(open_sql_stream, exactly like execution/worker.py's run_query_partition),
so a retried/failed run always restarts the whole stream from the top,
not from wherever it left off.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import update

from app.connectors.base import scrub_secrets
from app.database.connection import AsyncSessionLocal
from app.execution.retry import with_retry
from app.models.target_load_run import LoadStatus, TargetLoadRun, WriteMode


async def run_load(
    source_connector, writer, run: TargetLoadRun, *,
    sql: str, target_schema: str | None, target_object: str, columns: list[str],
    cancel_check,
) -> None:
    await _mark_started(run.id)

    # A failure anywhere here could come from either side (source read or
    # target write) — scrub against both connectors' secrets, not just one.
    def _scrub(exc) -> str:
        return scrub_secrets(str(exc), source_connector.secrets, writer.connector.secrets)

    if run.write_mode == WriteMode.TRUNCATE_INSERT:
        try:
            await asyncio.to_thread(writer.truncate, target_schema, target_object)
        except Exception as exc:
            await _mark_failed(run.id, f"Could not truncate target before load: {_scrub(exc)}")
            return

    key_columns = run.key_columns if run.write_mode == WriteMode.UPSERT else None

    stream = None
    try:
        async def _open():
            return await asyncio.to_thread(source_connector.open_sql_stream, sql, {})
        stream = await with_retry(_open, on_retry=_log_retry(run.id, "open"))

        while True:
            if cancel_check(run.id):
                await _mark_status(run.id, LoadStatus.CANCELLED)
                return

            async def _fetch():
                return await asyncio.to_thread(stream.fetch_chunk, run.chunk_size)
            rows = await with_retry(_fetch, on_retry=_log_retry(run.id, "fetch"))

            if not rows:
                break
            await _advance_read(run.id, len(rows))

            async def _write():
                return await asyncio.to_thread(writer.write_batch, target_schema, target_object, columns, rows, key_columns)
            written = await with_retry(_write, on_retry=_log_retry(run.id, "write"))
            await _advance_written(run.id, written)

            if len(rows) < run.chunk_size:
                break

        await _mark_status(run.id, LoadStatus.SUCCEEDED)
    except Exception as exc:
        await _mark_failed(run.id, _scrub(exc))
    finally:
        if stream is not None:
            await asyncio.to_thread(stream.close)


def _log_retry(run_id: int, stage: str):
    def _cb(attempt: int, delay: float, exc: Exception):
        print(f"[load] run={run_id} {stage} retry {attempt} in {delay:.1f}s after: {exc}")
    return _cb


async def _mark_started(run_id: int) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(status=LoadStatus.RUNNING, started_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _advance_read(run_id: int, count: int) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(records_read=TargetLoadRun.records_read + count)
        )
        await db.commit()


async def _advance_written(run_id: int, count: int) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(records_written=TargetLoadRun.records_written + count)
        )
        await db.commit()


async def _mark_status(run_id: int, status: LoadStatus) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(status=status, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _mark_failed(run_id: int, message: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(TargetLoadRun).where(TargetLoadRun.id == run_id)
            .values(status=LoadStatus.FAILED, error_message=message, completed_at=datetime.now(timezone.utc))
        )
        await db.commit()
