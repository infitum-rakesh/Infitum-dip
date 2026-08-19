"""
LoadExecutor — real backend execution engine for a target load (Phase 6),
the write-side counterpart to execution/pipeline_executor.py.

Same single-process, in-memory asyncio.create_task runner, same disclosed
limitation (a process restart loses the in-flight task; the run row on
disk reflects however far it actually got, never faked as further along).
No partition/checkpoint machinery here — see models/target_load_run.py
for why a load run is a single stream, not partitioned.

Source/target column metadata and the mapping's SQL are built fresh here,
every run, never cached from when the ColumnMapping was last saved —
same "never stale" rule mapping_service.py already follows for
Preview/Validate.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import update

from app.connectors.base import ConnectorValidationError, scrub_secrets
from app.database.connection import AsyncSessionLocal
from app.execution.load_worker import run_load
from app.loading.writers.registry import get_bulk_writer
from app.mapping import mapper
from app.models.column_mapping import ColumnMapping
from app.models.target_load_run import LoadStatus, TargetLoadRun, WriteMode
from app.services.connection_service import build_connector, get_connection

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


async def start_load(run_id: int) -> None:
    """Fire-and-forget — the API route that calls this does not await it;
    it returns the run's PENDING status immediately and the frontend polls
    GET /loads/{id}/status."""
    task = asyncio.create_task(_execute(run_id))
    _running_tasks[run_id] = task
    task.add_done_callback(lambda _t: _running_tasks.pop(run_id, None))


async def _execute(run_id: int) -> None:
    async with AsyncSessionLocal() as db:
        run = await db.get(TargetLoadRun, run_id)
        mapping = await db.get(ColumnMapping, run.mapping_id)
        # Defense in depth: re-verify both connections genuinely belong to
        # this run's OWN organization_id at execute time too, not just at
        # mapping-creation time (mapping_service.create_mapping) —
        # protects any row that predates that check, or reached the DB by
        # another path.
        source_conn = await get_connection(db, mapping.source_connection_id, run.organization_id)
        target_conn = await get_connection(db, mapping.target_connection_id, run.organization_id)
        run.status = LoadStatus.RUNNING
        run.started_at = run.started_at or datetime.now(timezone.utc)
        await db.commit()

    if source_conn is None or target_conn is None:
        async with AsyncSessionLocal() as db:
            r = await db.get(TargetLoadRun, run_id)
            r.status = LoadStatus.FAILED
            r.error_message = "The source or target connection for this mapping was not found or no longer belongs to this organization."
            r.completed_at = datetime.now(timezone.utc)
            await db.commit()
        return

    source_connector = target_connector = None
    try:
        source_connector = build_connector(source_conn)
        target_connector = build_connector(target_conn)
        writer = get_bulk_writer(target_conn.connection_type.value, target_connector)

        target_columns = await asyncio.to_thread(
            target_connector.get_columns, mapping.target_object, mapping.target_schema
        )
        columns = [m["target_column"] for m in mapping.mappings]

        key_columns = None
        if run.write_mode == WriteMode.UPSERT:
            target_pk = {c.name for c in target_columns if c.primary_key}
            key_columns = [c for c in columns if c in target_pk]
            if not key_columns:
                raise ConnectorValidationError(
                    "UPSERT needs at least one primary-key column on the target that is also part of "
                    "the mapped columns — this target has none (or its primary key isn't mapped)."
                )
            async with AsyncSessionLocal() as db:
                await db.execute(update(TargetLoadRun).where(TargetLoadRun.id == run_id).values(key_columns=key_columns))
                await db.commit()
            run.key_columns = key_columns

        sql = mapper.build_preview_sql(
            source_connector.quote_object, source_connector.quote_identifier,
            mapping.source_schema, mapping.source_object, mapping.mappings,
        )

        await run_load(
            source_connector, writer, run,
            sql=sql, target_schema=mapping.target_schema, target_object=mapping.target_object,
            columns=columns, cancel_check=is_cancelled,
        )
    except Exception as exc:
        # Anything that fails BEFORE run_load's own _mark_started (bad
        # mapping SQL, no usable primary key for UPSERT, no writer
        # registered for this target type) — run_load handles everything
        # after that point itself (and its own scrubbing) and never
        # re-raises. Scrub against whichever connector(s) actually got
        # built before the failure.
        secret_dicts = [c.secrets for c in (source_connector, target_connector) if c is not None]
        message = scrub_secrets(str(exc), *secret_dicts) if secret_dicts else str(exc)[:300]
        async with AsyncSessionLocal() as db:
            r = await db.get(TargetLoadRun, run_id)
            r.status = LoadStatus.FAILED
            r.error_message = message
            r.completed_at = datetime.now(timezone.utc)
            await db.commit()
    finally:
        _cancelled.discard(run_id)
