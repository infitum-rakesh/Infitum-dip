"""
sql_export_executor — real background execution for SQL Workspace export
jobs (models/sql_export_job.py). Same single-process, in-memory
asyncio.create_task runner as execution/load_executor.py, same disclosed
limitation (a process restart loses the in-flight task; the job row on
disk reflects however far it actually got — row_count is real progress,
never faked as further along).

Streams the query via connector.open_sql_stream() (the same bounded-
memory primitive Phase 3's pipeline extraction, reconciliation, and load
runs already use) in SQL_EXPORT_CHUNK_SIZE-row chunks straight to a real
file under SQL_EXPORT_BASE_PATH — no row cap, unlike the browser-bound
/run endpoint. Progress (row_count) is committed to the DB every chunk so
a client polling GET /sql-workspace/exports/{id} sees real numbers
climbing, not a static "running" spinner.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app.connectors.base import ConnectorValidationError
from app.database import tenant_engine_registry
from app.models.sql_export_job import SqlExportStatus, SqlWorkspaceExportJob
from app.services import connection_service
from app.services.sql_export_writers import extension_for, make_writer

SQL_EXPORT_BASE_PATH = "sql_exports"  # relative to the backend process cwd — same convention as pipeline_job_executor.OUTPUT_BASE_PATH
SQL_EXPORT_CHUNK_SIZE = 5000  # matches TargetLoadRun's default chunk_size

_running_tasks: dict[int, asyncio.Task] = {}
_cancelled: set[int] = set()


def is_running(job_id: int) -> bool:
    return job_id in _running_tasks


def request_cancel(job_id: int) -> bool:
    if job_id not in _running_tasks:
        return False
    _cancelled.add(job_id)
    return True


def start_export(job_id: int, organization_id: int | None) -> None:
    """Fire-and-forget — the API route that calls this does not await it;
    it returns the job's PENDING status immediately and the frontend
    polls GET /sql-workspace/exports/{id}. organization_id comes from the
    job row the caller already has in hand (see tenant_engine_registry)."""
    task = asyncio.create_task(_execute(job_id, organization_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda _t: _running_tasks.pop(job_id, None))


async def _mark_failed(job_id: int, message: str, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        job = await db.get(SqlWorkspaceExportJob, job_id)
        job.status = SqlExportStatus.FAILED
        job.error_message = message
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()


async def _execute(job_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        job = await db.get(SqlWorkspaceExportJob, job_id)
        conn = await connection_service.get_connection(db, job.connection_id, job.organization_id)
        if conn is None:
            job.status = SqlExportStatus.FAILED
            job.error_message = "The connection for this export was not found or no longer belongs to this organization."
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return
        job.status = SqlExportStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)
        connector = connection_service.build_connector(conn)
        fmt = job.format.value if hasattr(job.format, "value") else job.format
        sql_text = job.sql_text
        await db.commit()

    out_dir = Path(SQL_EXPORT_BASE_PATH) / str(job.organization_id or "platform")
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"export_{job_id}.{extension_for(fmt)}"
    full_path = out_dir / filename
    rel_path = str(Path(str(job.organization_id or "platform")) / filename)

    stream = None
    writer = None
    row_count = 0
    try:
        stream = await asyncio.to_thread(connector.open_sql_stream, sql_text, {})
        writer = make_writer(fmt, full_path)

        while True:
            if job_id in _cancelled:
                await asyncio.to_thread(writer.close)
                writer = None
                full_path.unlink(missing_ok=True)  # never expose a partial file as a real download — see module docstring
                async with sessionmaker() as db:
                    j = await db.get(SqlWorkspaceExportJob, job_id)
                    j.status = SqlExportStatus.CANCELLED
                    j.row_count = row_count
                    j.completed_at = datetime.now(timezone.utc)
                    await db.commit()
                return

            rows = await asyncio.to_thread(stream.fetch_chunk, SQL_EXPORT_CHUNK_SIZE)
            if not rows:
                break
            columns = list(rows[0].keys())
            row_values = [[r[c] for c in columns] for r in rows]
            await asyncio.to_thread(writer.write_rows, columns, row_values)
            row_count += len(rows)

            async with sessionmaker() as db:
                j = await db.get(SqlWorkspaceExportJob, job_id)
                j.row_count = row_count
                await db.commit()

            if len(rows) < SQL_EXPORT_CHUNK_SIZE:
                break

        await asyncio.to_thread(writer.close)
        writer = None
        file_size = full_path.stat().st_size if full_path.exists() else None

        async with sessionmaker() as db:
            j = await db.get(SqlWorkspaceExportJob, job_id)
            j.status = SqlExportStatus.SUCCEEDED
            j.row_count = row_count
            j.file_path = rel_path
            j.file_size_bytes = file_size
            j.completed_at = datetime.now(timezone.utc)
            await db.commit()
    except ConnectorValidationError as exc:
        await _mark_failed(job_id, str(exc), organization_id)
    except Exception as exc:
        message = connector._scrub(str(exc)) if connector is not None else str(exc)[:300]
        await _mark_failed(job_id, message, organization_id)
    finally:
        if stream is not None:
            await asyncio.to_thread(stream.close)
        if writer is not None:
            # Only reached if an exception hit before the normal close()
            # above — still close so the (now-failed) file handle isn't
            # left open, but the file itself is disclosed as incomplete
            # via status=FAILED, never presented as a real download.
            try:
                await asyncio.to_thread(writer.close)
            except Exception:
                pass
            try:
                full_path.unlink(missing_ok=True)
            except Exception:
                pass
        _cancelled.discard(job_id)
