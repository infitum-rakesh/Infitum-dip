"""
LoadExecutor — real backend execution engine for a target load (Phase 6),
the write-side counterpart to execution/pipeline_executor.py.

Same single-process, in-memory asyncio.create_task runner, same disclosed
limitation (a process restart loses the in-flight task; the run row on
disk reflects however far it actually got, never faked as further along).

Two real read strategies per run (JJ3/JJ4 — see models/target_load_run.py's
docstring for the full rationale). Chosen HERE, once, from the mapping's
own real shape and a real capability probe against the live source
connector — never from a request flag the caller has to get right:

  KEY_RANGE — preferred whenever it's actually viable: the mapping is a
  plain column copy (no lookup/transformation — those need a real SQL
  engine to evaluate), AND the source connector genuinely supports
  key-range reads (every SQL database connector, AND Salesforce via its
  Id-keyed SOQL — connectors/application/salesforce.py). Partitioned,
  parallel, and checkpointed — see execution/load_worker.py's
  run_key_range_partition() and this module's own partition-planning
  helpers below (mirroring execution/pipeline_executor.py's TABLE path).
  This is what makes a 100M+ row load survive a restart instead of
  starting over from row 0, and what lets it use more than one worker.

  SQL_STREAM — the original path (execution/load_worker.py's run_load()),
  kept byte-for-byte for its own callers. Used when the mapping needs a
  lookup/transformation (only a real SQL engine can evaluate those) and
  the source is a real SQL dialect connector; a non-SQL source (e.g.
  Salesforce) with a lookup/transformation mapping is refused up front
  with a clear, disclosed error (mapper.validate_key_range_mapping())
  rather than silently dropping the lookup or mis-evaluating it.

create_table: real CREATE TABLE from the source's own column metadata
(loading/writers/base.py's ensure_table_from_columns()) before the first
batch, when the target object doesn't already exist — see JJ1 for why
this matters (a properly sized VARCHAR2/VARCHAR, not blanket CLOB/TEXT,
at real bulk-load scale).

Source/target column metadata and the mapping's SQL/row-transform are
built fresh here, every run, never cached from when the ColumnMapping was
last saved — same "never stale" rule mapping_service.py already follows
for Preview/Validate.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.connectors.base import ConnectorUnsupportedError, ConnectorValidationError, scrub_secrets
from app.database import tenant_engine_registry
from app.execution.checkpoint import encode_key, infer_key_type
from app.execution.load_worker import run_key_range_partition, run_load
from app.ingestion.partition_reader import plan_partitions
from app.loading.writers.registry import get_bulk_writer
from app.mapping import mapper
from app.models.column_mapping import ColumnMapping
from app.models.pipeline_run import PartitionStatus
from app.models.target_load_run import LoadStatus, TargetLoadRun, TargetLoadRunPartition, WriteMode
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


async def start_load(run_id: int, organization_id: int | None) -> None:
    """Fire-and-forget — the API route that calls this does not await it;
    it returns the run's PENDING status immediately and the frontend polls
    GET /loads/{id}/status. organization_id comes from the run row the
    caller already has in hand — passed through explicitly since this
    task outlives the request and its session."""
    task = asyncio.create_task(_execute(run_id, organization_id))
    _running_tasks[run_id] = task
    task.add_done_callback(lambda _t: _running_tasks.pop(run_id, None))


async def _pick_key_column(source_connector, source_object: str, source_schema: str | None) -> str | None:
    """Real primary-key detection via the connector's own get_columns()
    (reflected SQL metadata, or Salesforce's own describe() — which
    always marks "Id" primary_key=True) — never guessed. None means this
    source object has no real primary key we can partition on."""
    try:
        cols = await asyncio.to_thread(source_connector.get_columns, source_object, source_schema)
    except Exception:
        return None
    return next((c.name for c in cols if c.primary_key), None)


async def _build_create_table_columns(source_connector, mapping: ColumnMapping):
    """Real per-target-column metadata for ensure_table_from_columns()
    (JJ1) — pulled from the source's own get_columns() wherever a mapping
    row is a plain source-column copy; a synthesized bare string column
    (same safe fallback as before) for constant_value/lookup/
    transformation rows, which have no real source column backing them."""
    from app.connectors.base import ColumnInfo

    try:
        source_cols_by_name = {
            c.name: c for c in await asyncio.to_thread(source_connector.get_columns, mapping.source_object, mapping.source_schema)
        }
    except Exception:
        source_cols_by_name = {}

    columns = []
    for m in mapping.mappings:
        target = m["target_column"]
        src_name = m["source_columns"][0] if m.get("source_columns") else None
        source_col = source_cols_by_name.get(src_name) if src_name else None
        if source_col is not None:
            columns.append(ColumnInfo(name=target, data_type=source_col.data_type, nullable=True, max_length=source_col.max_length))
        else:
            columns.append(ColumnInfo(name=target, data_type="string", nullable=True))
    return columns


async def _execute(run_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        run = await db.get(TargetLoadRun, run_id)
        mapping = await db.get(ColumnMapping, run.mapping_id)
        # Defense in depth: re-verify both connections genuinely belong to
        # this run's OWN organization_id at execute time too, not just at
        # mapping-creation time (mapping_service.create_mapping) —
        # protects any row that predates that check, or reached the DB by
        # another path.
        source_conn = await get_connection(db, mapping.source_connection_id, run.organization_id)
        target_conn = await get_connection(db, mapping.target_connection_id, run.organization_id)
        # A non-None started_at means this run has genuinely been dispatched
        # before (a Resume, or a re-invocation on an already-finished run) —
        # the ONE signal the create_table/recreate_table step below uses to
        # avoid re-running DDL (DROP+CREATE, wiping already-loaded rows) on
        # anything but a true first attempt. Captured before the line below
        # overwrites it.
        is_first_attempt = run.started_at is None
        run.status = LoadStatus.RUNNING
        run.started_at = run.started_at or datetime.now(timezone.utc)
        await db.commit()

    if source_conn is None or target_conn is None:
        async with sessionmaker() as db:
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

        columns = [m["target_column"] for m in mapping.mappings]
        needs_sql_engine = any(m.get("lookup") or m.get("transformation") for m in mapping.mappings)
        is_sql_dialect = hasattr(source_connector, "quote_object")

        if needs_sql_engine and not is_sql_dialect:
            # Raises MapperValidationError naming the offending column(s) —
            # a lookup/transformation needs a real SQL engine to evaluate,
            # and this source (e.g. Salesforce) doesn't have one.
            mapper.validate_key_range_mapping(mapping.mappings)

        # Target column metadata — best-effort: the target object may not
        # exist yet if create_table is set, in which case this is (and
        # should be) empty rather than an error.
        try:
            target_columns = await asyncio.to_thread(target_connector.get_columns, mapping.target_object, mapping.target_schema)
        except Exception:
            target_columns = []

        source_key_column = None
        if not needs_sql_engine:
            source_key_column = await _pick_key_column(source_connector, mapping.source_object, mapping.source_schema)

        key_columns = None
        if run.write_mode == WriteMode.UPSERT:
            target_pk = {c.name for c in target_columns if c.primary_key}
            key_columns = [c for c in columns if c in target_pk]
            if not key_columns and run.create_table and source_key_column:
                # Target doesn't exist yet (create_table) — no live PK to
                # detect. Fall back to the mapped target column for the
                # source's own real key column, which becomes both the new
                # table's PRIMARY KEY and the UPSERT match key.
                key_columns = [
                    m["target_column"] for m in mapping.mappings
                    if m.get("source_columns") == [source_key_column]
                ]
            if not key_columns:
                raise ConnectorValidationError(
                    "UPSERT needs at least one primary-key column on the target that is also part of the "
                    "mapped columns — this target has none mapped" +
                    (" and create_table can't infer one either (map the source's own key column, e.g. Id, "
                     "to a target column)." if run.create_table else ".")
                )
            async with sessionmaker() as db:
                await db.execute(update(TargetLoadRun).where(TargetLoadRun.id == run_id).values(key_columns=key_columns))
                await db.commit()
            run.key_columns = key_columns

        if run.create_table and is_first_attempt:
            # Only on the true first attempt — recreate_table means DROP+
            # CREATE, and re-running that on a Resume (or a re-invocation on
            # an already-finished run) would silently wipe every row this
            # run already landed, while its own records_written counter
            # keeps claiming they're still there. plain create-if-missing
            # (recreate_table=False) is technically idempotent, but skipping
            # it here too avoids a redundant reflection call on every resume.
            create_columns = await _build_create_table_columns(source_connector, mapping)
            try:
                table_status = await asyncio.to_thread(
                    writer.ensure_table_from_columns, mapping.target_schema, mapping.target_object, create_columns,
                    recreate=run.recreate_table, key_columns=key_columns,
                )
                print(f"[load] run={run_id} target table '{mapping.target_object}': {table_status}")
            except Exception as exc:
                raise ConnectorValidationError(f"Could not create target table: {exc}") from exc

        use_key_range = False
        if source_key_column:
            try:
                min_v, max_v = await asyncio.to_thread(
                    source_connector.key_range, mapping.source_object, mapping.source_schema, source_key_column
                )
                use_key_range = True
            except ConnectorUnsupportedError:
                use_key_range = False

        if not use_key_range and not is_sql_dialect:
            raise ConnectorValidationError(
                f"{source_conn.connection_type.value} doesn't support a real SQL read (needed for this "
                "mapping's lookup/transformation) or a key-range read (needed for a plain column copy — "
                "this source object may have no detectable primary key) — this combination can't be loaded yet."
            )

        if use_key_range:
            async with sessionmaker() as db:
                await db.execute(
                    update(TargetLoadRun).where(TargetLoadRun.id == run_id)
                    .values(source_kind="KEY_RANGE", key_column=source_key_column)
                )
                await db.commit()
            run.source_kind, run.key_column = "KEY_RANGE", source_key_column
            row_transform = mapper.build_row_transform(mapping.mappings)
            await _run_key_range_load(
                run_id, run, source_connector, writer,
                source_schema=mapping.source_schema, source_object=mapping.source_object,
                target_schema=mapping.target_schema, target_object=mapping.target_object,
                columns=columns, key_columns=key_columns, row_transform=row_transform,
                min_key=min_v, max_key=max_v, is_first_attempt=is_first_attempt,
            )
        else:
            async with sessionmaker() as db:
                await db.execute(update(TargetLoadRun).where(TargetLoadRun.id == run_id).values(source_kind="SQL_STREAM"))
                await db.commit()
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
        # Anything that fails BEFORE the dispatched path's own _mark_started
        # (bad mapping SQL, no usable primary key for UPSERT, no writer
        # registered for this target type, create_table failure) — the
        # dispatched path handles everything after that point itself (and
        # its own scrubbing) and never re-raises. Scrub against whichever
        # connector(s) actually got built before the failure.
        secret_dicts = [c.secrets for c in (source_connector, target_connector) if c is not None]
        message = scrub_secrets(str(exc), *secret_dicts) if secret_dicts else str(exc)[:300]
        async with sessionmaker() as db:
            r = await db.get(TargetLoadRun, run_id)
            r.status = LoadStatus.FAILED
            r.error_message = message
            r.completed_at = datetime.now(timezone.utc)
            await db.commit()
    finally:
        _cancelled.discard(run_id)


async def _run_key_range_load(
    run_id: int, run: TargetLoadRun, source_connector, writer, *,
    source_schema: str | None, source_object: str, target_schema: str | None, target_object: str,
    columns: list[str], key_columns: list[str] | None, row_transform, min_key, max_key, is_first_attempt: bool,
) -> None:
    # TRUNCATE_INSERT only truncates on the true FIRST attempt — never on a
    # Resume (or a re-invocation on an already-finished run), which would
    # wipe out every already-COMPLETE partition's real data while its
    # checkpoint metadata still claims it's done, silently losing rows
    # rather than completing the run.
    if run.write_mode == WriteMode.TRUNCATE_INSERT and is_first_attempt:
        try:
            await asyncio.to_thread(writer.truncate, target_schema, target_object)
        except Exception as exc:
            message = scrub_secrets(str(exc), source_connector.secrets, writer.connector.secrets)
            sessionmaker = await tenant_engine_registry.get_background_sessionmaker(run.organization_id)
            async with sessionmaker() as db:
                r = await db.get(TargetLoadRun, run_id)
                r.status = LoadStatus.FAILED
                r.error_message = f"Could not truncate target before load: {message}"
                r.completed_at = datetime.now(timezone.utc)
                await db.commit()
            return

    partitions, key_type = await _load_or_plan_key_range_partitions(run_id, run, min_key, max_key)
    # The DB row's key_type was just set (or already existed, on Resume) —
    # mirror it onto THIS in-memory `run` object too, since that's what
    # run_key_range_partition below actually reads via decode_key(). A
    # write-only-to-DB update here would leave every partition decoding
    # against a stale/None key_type and silently mis-typing every
    # checkpoint value (wrong for int/datetime keys; harmless only by
    # coincidence for Salesforce's string Id).
    run.key_type = key_type
    todo = [p for p in partitions if p.status != PartitionStatus.COMPLETE]
    sem = asyncio.Semaphore(max(1, run.worker_count))

    async def _bounded(p):
        async with sem:
            await run_key_range_partition(
                source_connector, writer, run, p,
                source_schema=source_schema, source_object=source_object,
                target_schema=target_schema, target_object=target_object,
                columns=columns, key_columns=key_columns, row_transform=row_transform,
                cancel_check=is_cancelled,
            )

    await asyncio.gather(*[_bounded(p) for p in todo])
    await _finalize_key_range(run_id, run.organization_id)


async def _load_or_plan_key_range_partitions(run_id: int, run: TargetLoadRun, min_key, max_key) -> tuple[list[TargetLoadRunPartition], str]:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(run.organization_id)
    async with sessionmaker() as db:
        result = await db.execute(
            select(TargetLoadRunPartition).where(TargetLoadRunPartition.run_id == run_id).order_by(TargetLoadRunPartition.partition_index)
        )
        partitions = list(result.scalars().all())
        if partitions:
            # Already planned — this is a Resume. key_type was persisted on
            # the first pass; read it back rather than re-inferring (min_key
            # here is a FRESH key_range() call's result, which is fine for
            # re-deriving ranges but shouldn't silently override a key_type
            # that already has real partitions/checkpoints keyed against it).
            r = await db.get(TargetLoadRun, run_id)
            return partitions, r.key_type or "str"

        key_type = infer_key_type(min_key) if min_key is not None else "str"
        ranges = plan_partitions(min_key, max_key, key_type, run.partition_count)

        new_partitions = [
            TargetLoadRunPartition(run_id=run_id, partition_index=i, range_start=encode_key(s), range_end=encode_key(e))
            for i, (s, e) in enumerate(ranges)
        ]
        db.add_all(new_partitions)
        r = await db.get(TargetLoadRun, run_id)
        r.key_type = key_type
        await db.commit()
        for p in new_partitions:
            await db.refresh(p)
        return new_partitions, key_type


async def _finalize_key_range(run_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        run = await db.get(TargetLoadRun, run_id)
        result = await db.execute(select(TargetLoadRunPartition).where(TargetLoadRunPartition.run_id == run_id))
        partitions = list(result.scalars().all())

        if is_cancelled(run_id) or any(p.status == PartitionStatus.CANCELLED for p in partitions):
            run.status = LoadStatus.CANCELLED
        elif any(p.status == PartitionStatus.FAILED for p in partitions):
            run.status = LoadStatus.FAILED
            failed = next(p for p in partitions if p.status == PartitionStatus.FAILED)
            run.error_message = f"Partition {failed.partition_index}: {failed.error_message}"
        else:
            run.status = LoadStatus.SUCCEEDED
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
