"""
transformation_executor — real background execution for Data Format
Transformation Engine jobs (models/transformation_job.py). Same single-
process, in-memory asyncio.create_task runner as execution/
sql_export_executor.py (same disclosed limitation: a process restart
loses the in-flight task; row_count on the job row reflects however far
it actually got, never faked as further along).

Reading: resolves the job's source into the SAME RowSource abstraction
reconciliation/anomaly/validation already share (reconciliation/
engine.py's ConnectorRowSource for a database TABLE, FileRowSource for an
uploaded file) — get_columns() once, then iter_rows() in bounded chunks.
Adding a new file format to connectors/files/ (see that package's
registry.py) makes it available here for free, no changes needed in this
module.

Writing: DOWNLOAD_FILE targets go through services/sql_export_writers.py's
real ChunkWriter classes (the same ones SQL Workspace's background export
already uses — CSV/JSON/XLSX/PARQUET/XML, chunked straight to disk).
CONNECTION targets (a real database TABLE) go through loading/writers/'s
BulkWriter — ensure_table() from a small sample (real column dtypes, not
guessed) then write_batch_with_fallback() for every chunk — the exact
same pattern services/document_storage_service.py's push_document()
established in Phase 3.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.connectors.base import ConnectorError, ConnectorValidationError, scrub_secrets
from app.database import tenant_engine_registry
from app.loading.writers.base import write_batch_with_fallback
from app.loading.writers.registry import get_bulk_writer
from app.models.transformation_job import TransformationJob, TransformationJobStatus, TransformEndpointKind
from app.reconciliation.engine import ConnectorRowSource, FileRowSource
from app.services import connection_service, uploaded_file_service
from app.services.sql_export_writers import extension_for, make_writer
from app.services.transformation_service import TRANSFORM_EXPORT_BASE_PATH

CHUNK_SIZE = 5000

_running_tasks: dict[int, asyncio.Task] = {}
_cancelled: set[int] = set()


def is_running(job_id: int) -> bool:
    return job_id in _running_tasks


def request_cancel(job_id: int) -> bool:
    if job_id not in _running_tasks:
        return False
    _cancelled.add(job_id)
    return True


def start_job(job_id: int, organization_id: int | None) -> None:
    """Fire-and-forget — the API route returns the job's PENDING status
    immediately; the frontend polls GET /transformation/jobs/{id}."""
    task = asyncio.create_task(_execute(job_id, organization_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda _t: _running_tasks.pop(job_id, None))


async def _mark_failed(job_id: int, message: str, sessionmaker) -> None:
    async with sessionmaker() as db:
        job = await db.get(TransformationJob, job_id)
        job.status = TransformationJobStatus.FAILED
        job.error_message = message
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()


async def _build_source(db, job: TransformationJob, organization_id: int | None):
    if job.source_kind == TransformEndpointKind.UPLOADED_FILE:
        record = await uploaded_file_service.get_by_id(db, job.source_uploaded_file_id, organization_id)
        if record is None:
            raise ConnectorValidationError("The source uploaded file no longer exists or no longer belongs to this organization.")
        connector = uploaded_file_service.build_connector(record)
        return FileRowSource(connector, record.filename), connector
    conn = await connection_service.get_connection(db, job.source_connection_id, organization_id)
    if conn is None:
        raise ConnectorValidationError("The source connection no longer exists or no longer belongs to this organization.")
    connector = connection_service.build_connector(conn)
    return ConnectorRowSource(connector, job.source_schema, job.source_object), connector


async def _execute(job_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        job = await db.get(TransformationJob, job_id)
        target_format = job.target_format.value if hasattr(job.target_format, "value") else job.target_format
        target_kind = job.target_kind
        try:
            source, source_connector = await _build_source(db, job, job.organization_id)
        except ConnectorError as exc:
            job.status = TransformationJobStatus.FAILED
            job.error_message = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return
        target_conn = None
        if target_kind == TransformEndpointKind.CONNECTION:
            target_conn = await connection_service.get_connection(db, job.target_connection_id, job.organization_id)
            if target_conn is None:
                job.status = TransformationJobStatus.FAILED
                job.error_message = "The target connection no longer exists or no longer belongs to this organization."
                job.completed_at = datetime.now(timezone.utc)
                await db.commit()
                return
        job.status = TransformationJobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)
        target_connection_type = target_conn.connection_type.value if target_conn else None
        target_schema, target_object = job.target_schema, job.target_object
        format_options = job.format_options
        await db.commit()

    out_dir = Path(TRANSFORM_EXPORT_BASE_PATH) / str(organization_id or "platform")
    file_writer = None
    full_path = None
    rel_path = None
    target_bulk_writer = None
    target_secrets: dict = {}
    row_count = 0
    table_ready = False

    try:
        if target_kind == TransformEndpointKind.DOWNLOAD_FILE:
            out_dir.mkdir(parents=True, exist_ok=True)
            filename = f"transform_{job_id}.{extension_for(target_format)}"
            full_path = out_dir / filename
            rel_path = str(Path(str(organization_id or "platform")) / filename)
            file_writer = await asyncio.to_thread(make_writer, target_format, full_path, format_options)
        else:
            target_connector = connection_service.build_connector(target_conn)
            target_secrets = target_connector.secrets
            target_bulk_writer = get_bulk_writer(target_connection_type, target_connector)

        columns = await asyncio.to_thread(source.get_columns)
        # RowSource.iter_rows() is a plain synchronous generator (reads a
        # DB cursor or a file stream under the hood) — iterating it
        # directly with a bare `for` would block the whole event loop for
        # each chunk's I/O. Advancing it one chunk at a time via
        # asyncio.to_thread(next, ...) keeps each blocking read off the
        # event loop, the same way this codebase already drives
        # SqlStream.fetch_chunk() (execution/sql_export_executor.py) —
        # RowSource's own generator shape just needs `next()` instead of
        # a direct method call to get the same effect.
        row_gen = source.iter_rows(columns, chunk_size=CHUNK_SIZE)

        while True:
            if job_id in _cancelled:
                break
            chunk = await asyncio.to_thread(next, row_gen, None)
            if not chunk:
                break

            if file_writer is not None:
                row_values = [[r.get(c) for c in columns] for r in chunk]
                await asyncio.to_thread(file_writer.write_rows, columns, row_values)
            else:
                if not table_ready:
                    sample_df = pd.DataFrame(chunk, columns=columns)
                    await asyncio.to_thread(target_bulk_writer.ensure_table, target_schema, target_object, sample_df)
                    table_ready = True
                await asyncio.to_thread(
                    write_batch_with_fallback, target_bulk_writer, target_schema, target_object, columns, chunk, None,
                )

            row_count += len(chunk)
            async with sessionmaker() as db:
                j = await db.get(TransformationJob, job_id)
                j.row_count = row_count
                await db.commit()

        if job_id in _cancelled:
            if file_writer is not None:
                await asyncio.to_thread(file_writer.close)
                file_writer = None
                full_path.unlink(missing_ok=True)
            async with sessionmaker() as db:
                j = await db.get(TransformationJob, job_id)
                j.status = TransformationJobStatus.CANCELLED
                j.row_count = row_count
                j.completed_at = datetime.now(timezone.utc)
                await db.commit()
            return

        if file_writer is not None:
            await asyncio.to_thread(file_writer.close)
            file_writer = None

        file_size = full_path.stat().st_size if full_path is not None and full_path.exists() else None
        async with sessionmaker() as db:
            j = await db.get(TransformationJob, job_id)
            j.status = TransformationJobStatus.SUCCEEDED
            j.row_count = row_count
            j.file_path = rel_path
            j.file_size_bytes = file_size
            j.completed_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception as exc:
        message = scrub_secrets(str(exc), source_connector.secrets if source_connector is not None else {}, target_secrets)
        await _mark_failed(job_id, message, sessionmaker)
    finally:
        if file_writer is not None:
            try:
                await asyncio.to_thread(file_writer.close)
            except Exception:
                pass
            if full_path is not None:
                try:
                    full_path.unlink(missing_ok=True)
                except Exception:
                    pass
        _cancelled.discard(job_id)
