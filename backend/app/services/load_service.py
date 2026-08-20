"""
load_service — the API-facing layer for Phase 6 target loads: resolves a
saved ColumnMapping into a TargetLoadRun row and answers status/metrics
queries. The actual load work happens in execution/load_executor.py,
kicked off as a background asyncio task from the API route — this module
never blocks on it, same split as execution_service.py/Phase 3.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution import load_executor
from app.models.column_mapping import ColumnMapping
from app.models.target_load_run import LoadStatus, TargetLoadRun, WriteMode


class LoadValidationError(Exception):
    """Bad request — the mapping isn't runnable as-is."""


DEFAULT_CHUNK_SIZE = 5000


async def create_load(
    db: AsyncSession, *, organization_id: int | None, mapping: ColumnMapping,
    write_mode: str | None, chunk_size: int | None, created_by: int | None,
    create_table: bool = False, recreate_table: bool = False,
    worker_count: int | None = None, partition_count: int | None = None,
) -> TargetLoadRun:
    if not mapping.mappings:
        raise LoadValidationError("This mapping has no column mapping rows to load — add at least one in the Mapping page first.")
    if mapping.status == "CONFLICT":
        raise LoadValidationError("This mapping has unresolved conflicts (see Validate) — fix them before running a load.")

    try:
        mode = WriteMode(write_mode) if write_mode else WriteMode.APPEND
    except ValueError:
        raise LoadValidationError(f"Unknown write mode '{write_mode}' — use APPEND, TRUNCATE_INSERT, or UPSERT.")

    run = TargetLoadRun(
        organization_id=organization_id,
        mapping_id=mapping.id,
        write_mode=mode,
        chunk_size=chunk_size or DEFAULT_CHUNK_SIZE,
        create_table=create_table,
        recreate_table=recreate_table,
        worker_count=worker_count or 4,
        partition_count=partition_count or 4,
        status=LoadStatus.PENDING,
        created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def get_load(db: AsyncSession, run_id: int, organization_id: int | None) -> TargetLoadRun | None:
    stmt = select(TargetLoadRun).where(TargetLoadRun.id == run_id)
    if organization_id is not None:
        stmt = stmt.where(TargetLoadRun.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_loads(db: AsyncSession, mapping_id: int, organization_id: int | None) -> tuple[list[TargetLoadRun], int]:
    stmt = select(TargetLoadRun).where(TargetLoadRun.mapping_id == mapping_id)
    if organization_id is not None:
        stmt = stmt.where(TargetLoadRun.organization_id == organization_id)
    result = await db.execute(stmt)
    runs = list(result.scalars().all())
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return runs, len(runs)


async def cancel_load(db: AsyncSession, run: TargetLoadRun) -> TargetLoadRun:
    requested = load_executor.request_cancel(run.id)
    if not requested:
        # Nothing actually running in this process (already finished, or
        # the process restarted) — reflect that directly rather than
        # pretending a cancel request landed somewhere.
        if run.status == LoadStatus.RUNNING:
            run.status = LoadStatus.CANCELLED
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(run)
    return run


def compute_metrics(run: TargetLoadRun) -> dict:
    elapsed = None
    throughput = None
    if run.started_at:
        # SQLite round-trips datetimes naive even though stored as aware —
        # normalize both sides the same way (see execution_service.py's
        # compute_metrics for the identical fix and why).
        end_raw = run.completed_at or datetime.now(timezone.utc)
        end = end_raw if end_raw.tzinfo else end_raw.replace(tzinfo=timezone.utc)
        started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
        elapsed = max(0.0, (end - started).total_seconds())
        if elapsed > 0 and run.records_written > 0:
            throughput = round(run.records_written / elapsed, 1)
    return {
        "status": run.status.value,
        "records_read": run.records_read,
        "records_written": run.records_written,
        "records_rejected": run.records_rejected,
        "throughput_rps": throughput,
        "elapsed_seconds": elapsed,
    }
