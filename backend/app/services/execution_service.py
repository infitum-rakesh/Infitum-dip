"""
execution_service — the API-facing layer for Phase 3 pipeline runs:
resolves a saved pipeline node into a PipelineRun row, and answers
status/metrics queries. The actual extraction work happens in
execution/pipeline_executor.py, kicked off as a background asyncio task
from the API route — this module never blocks on it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution import pipeline_executor
from app.models.pipeline import Pipeline
from app.models.pipeline_run import PartitionStatus, PipelineRun, PipelineRunPartition, RunStatus
from app.services import connection_service, pipeline_service


class RunValidationError(Exception):
    """Bad request — the node isn't a runnable Source Dataset config."""


DEFAULT_CHUNK_SIZE = 20000
DEFAULT_WORKER_COUNT = 4
DEFAULT_PARTITION_COUNT = 4


async def create_run(
    db: AsyncSession, *, organization_id: int | None, pipeline: Pipeline, node_id: str,
    key_column: str | None, chunk_size: int | None, worker_count: int | None, partition_count: int | None,
    created_by: int | None, archive_dir: str | None = None, error_dir: str | None = None,
) -> PipelineRun:
    version = await pipeline_service.get_current_version(db, pipeline)
    if version is None:
        raise RunValidationError("This pipeline has no saved version to run.")

    definition = version.pipeline_definition or {}
    node = next((n for n in definition.get("nodes", []) if n.get("id") == node_id), None)
    if node is None:
        raise RunValidationError(f"Node '{node_id}' was not found in the current saved pipeline version.")
    if node.get("type") != "source":
        raise RunValidationError(f"Node '{node_id}' is a '{node.get('type')}' node, not a Source Dataset.")

    cfg = node.get("config") or {}
    connection_id = cfg.get("connectionId")
    if not connection_id:
        raise RunValidationError("This Source Dataset node has no connection selected.")
    # A saved pipeline definition is just JSON — connectionId inside a
    # node's config can't be trusted as-is. Verify it genuinely belongs
    # to this org before ever extracting through it; without this, a
    # crafted pipeline definition could reach another tenant's connection.
    if organization_id is not None:
        owned = await connection_service.get_connection(db, connection_id, organization_id)
        if owned is None:
            raise RunValidationError(f"Connection {connection_id} was not found.")
    object_type = cfg.get("objectType")

    schema_name = object_name = custom_sql = None
    sql_parameters: dict = {}
    source_kind = None
    run_archive_dir = run_error_dir = None
    if object_type in ("TABLE", "VIEW"):
        object_name = cfg.get("object")
        schema_name = cfg.get("schema") or None
        if not object_name:
            raise RunValidationError("This Source Dataset node has no table/view selected.")
        if not key_column:
            raise RunValidationError("A key column is required to extract a Table/View (drives partitioning and checkpointing).")
        source_kind = "TABLE"
    elif object_type == "QUERY":
        custom_sql = (cfg.get("customSql") or "").strip()
        if not custom_sql:
            raise RunValidationError("This Source Dataset node has no SQL entered.")
        sql_parameters = {p["name"]: p["value"] for p in (cfg.get("parameters") or []) if p.get("name")}
        source_kind = "QUERY"
    elif object_type == "NETSUITE_SEARCH":
        # Reuses the same "schema"/"object" node-config keys the TABLE/VIEW
        # path does — schema_name holds the NetSuite record type (e.g.
        # "customer"), object_name the saved search's internal ID (the
        # connector's list_schemas()/list_objects() drive that same
        # two-level picker on the frontend). See execution/worker.py's
        # run_netsuite_search_partition() and NetSuiteSoapConnector.
        schema_name = (cfg.get("schema") or "").strip() or None
        object_name = (cfg.get("object") or "").strip() or None
        if not schema_name or not object_name:
            raise RunValidationError("This Source Dataset node needs both a NetSuite record type and a saved search selected.")
        source_kind = "NETSUITE_SEARCH"
    elif object_type == "FILE":
        # object_name here is a file-matching pattern (glob, or "re:<regex>"
        # — same convention connectors/files list_files() already uses),
        # not a single object — a FILE run can plan multiple partitions,
        # one per matched file. Left blank, pipeline_executor passes None
        # through to connector.list_files(), which each connector defaults
        # sensibly (LocalFileConnector falls back to its own configured
        # file_pattern; SftpConnector to "*") — never silently empty.
        object_name = (cfg.get("object") or "").strip() or None
        source_kind = "FILE"
        run_archive_dir = archive_dir or cfg.get("archiveDir") or None
        run_error_dir = error_dir or cfg.get("errorDir") or None
    else:
        raise RunValidationError(
            f"Object type '{object_type}' does not support extraction yet — only Table, View, Custom Query and File do."
        )

    run = PipelineRun(
        organization_id=organization_id,
        pipeline_id=pipeline.id,
        pipeline_version=version.version_number,
        node_id=node_id,
        connection_id=connection_id,
        schema_name=schema_name,
        object_name=object_name,
        custom_sql=custom_sql,
        sql_parameters=sql_parameters or None,
        key_column=key_column if object_type in ("TABLE", "VIEW") else None,
        source_kind=source_kind,
        archive_dir=run_archive_dir,
        error_dir=run_error_dir,
        chunk_size=chunk_size or cfg.get("batchSize") or DEFAULT_CHUNK_SIZE,
        worker_count=worker_count or DEFAULT_WORKER_COUNT,
        partition_count=partition_count or DEFAULT_PARTITION_COUNT,
        staging_location=pipeline_executor.STAGING_BASE_PATH,
        status=RunStatus.PENDING,
        created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def get_run(db: AsyncSession, run_id: int, organization_id: int | None) -> PipelineRun | None:
    stmt = select(PipelineRun).where(PipelineRun.id == run_id)
    if organization_id is not None:
        stmt = stmt.where(PipelineRun.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_partitions(db: AsyncSession, run_id: int) -> list[PipelineRunPartition]:
    result = await db.execute(
        select(PipelineRunPartition).where(PipelineRunPartition.run_id == run_id).order_by(PipelineRunPartition.partition_index)
    )
    return list(result.scalars().all())


async def list_runs(db: AsyncSession, pipeline_id: int, organization_id: int | None) -> tuple[list[PipelineRun], int]:
    stmt = select(PipelineRun).where(PipelineRun.pipeline_id == pipeline_id)
    if organization_id is not None:
        stmt = stmt.where(PipelineRun.organization_id == organization_id)
    result = await db.execute(stmt)
    runs = list(result.scalars().all())
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return runs, len(runs)


async def resume_run(db: AsyncSession, run: PipelineRun) -> PipelineRun:
    if pipeline_executor.is_running(run.id):
        raise RunValidationError("This run is already in progress.")
    if run.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
        raise RunValidationError(f"Only a failed or cancelled run can be resumed (current status: {run.status.value}).")
    run.status = RunStatus.PENDING
    run.error_message = None
    run.completed_at = None
    await db.commit()
    await db.refresh(run)
    return run


async def cancel_run(db: AsyncSession, run: PipelineRun) -> PipelineRun:
    requested = pipeline_executor.request_cancel(run.id)
    if not requested:
        # Nothing actually running in this process (e.g. it already
        # finished, or the process restarted) — reflect that directly.
        if run.status == RunStatus.RUNNING:
            run.status = RunStatus.CANCELLED
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(run)
    return run


def compute_metrics(run: PipelineRun, partitions: list[PipelineRunPartition]) -> dict:
    partitions_complete = sum(1 for p in partitions if p.status == PartitionStatus.COMPLETE)
    elapsed = None
    throughput = None
    eta = None
    if run.started_at:
        # SQLite round-trips datetimes as naive even though they were
        # stored via datetime.now(timezone.utc) — both ends of this
        # subtraction must be normalized the same way, or a completed
        # run's completed_at (naive) vs. a live datetime.now(timezone.utc)
        # (aware) fallback would raise TypeError.
        end_raw = run.completed_at or datetime.now(timezone.utc)
        end = end_raw if end_raw.tzinfo else end_raw.replace(tzinfo=timezone.utc)
        started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
        elapsed = max(0.0, (end - started).total_seconds())
        if elapsed > 0 and run.records_read > 0:
            throughput = round(run.records_read / elapsed, 1)
            if run.status == RunStatus.RUNNING and partitions and len(partitions) > 0:
                frac_done = partitions_complete / len(partitions)
                if 0 < frac_done < 1:
                    eta = round(elapsed * (1 - frac_done) / frac_done, 1)
    return {
        "status": run.status.value,
        "records_read": run.records_read,
        "records_written": run.records_written,
        "throughput_rps": throughput,
        "elapsed_seconds": elapsed,
        "eta_seconds": eta,
        "partitions_complete": partitions_complete,
        "partitions_total": len(partitions),
    }
