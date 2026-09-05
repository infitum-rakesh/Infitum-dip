"""
anomaly_export_executor — real background execution for AnomalyReport
rows (models/anomaly.py). Same single-process asyncio.create_task runner
as execution/sql_export_executor.py (same disclosed limitation: a process
restart loses the in-flight task; the job row on disk reflects however
far it actually got).

Unlike a SQL export, the data here already lives in this tenant's own
DB (AnomalyExecution/AnomalyColumnResult/AnomalyRecord) — no external
connector round trip, just DB-side LIMIT/OFFSET chunking through
anomaly_service.list_all_records(), the same bounded-memory discipline
(never the whole record set materialized in Python at once) applied to a
local query instead of a remote one.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.anomaly.reporting import extension_for, make_writer
from app.database import tenant_engine_registry
from app.models.anomaly import (
    AnomalyColumnResult,
    AnomalyExecution,
    AnomalyJob,
    AnomalyRecord,
    AnomalyReport,
    ReportStatus,
)

# NOTE: deliberately does NOT import services/anomaly_service.py — that
# module imports THIS one (to call start_report()), and services/
# anomaly_service.py already imports anomaly/engine.py the same way
# sql_export_service.py imports execution/sql_export_executor.py without
# a reverse dependency. Queries here are self-contained SQLAlchemy calls,
# not delegated to the service layer — same reasoning sql_export_
# executor.py already applies (it doesn't call into sql_workspace_
# service.py either).

ANOMALY_REPORT_BASE_PATH = "anomaly_reports"  # relative to backend process cwd — same convention as SQL_EXPORT_BASE_PATH
ANOMALY_REPORT_CHUNK_SIZE = 5000

_running_tasks: dict[int, asyncio.Task] = {}


def start_report(report_id: int, organization_id: int | None) -> None:
    """Fire-and-forget — the API route returns the report's PENDING
    status immediately; the frontend polls GET /anomaly/reports/{id}."""
    task = asyncio.create_task(_execute(report_id, organization_id))
    _running_tasks[report_id] = task
    task.add_done_callback(lambda _t: _running_tasks.pop(report_id, None))


async def _mark_failed(sessionmaker, report_id: int, message: str) -> None:
    async with sessionmaker() as db:
        report = await db.get(AnomalyReport, report_id)
        report.status = ReportStatus.FAILED
        report.error_message = message
        report.completed_at = datetime.now(timezone.utc)
        await db.commit()


async def _execute(report_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)

    async with sessionmaker() as db:
        report = await db.get(AnomalyReport, report_id)
        if report is None:
            return
        execution = await db.get(AnomalyExecution, report.execution_id)
        if execution is None:
            report.status = ReportStatus.FAILED
            report.error_message = "The anomaly execution for this report no longer exists."
            report.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return
        job = await db.get(AnomalyJob, execution.job_id)
        column_results = list((
            await db.execute(select(AnomalyColumnResult).where(AnomalyColumnResult.execution_id == execution.id).order_by(AnomalyColumnResult.id))
        ).scalars().all())
        fmt = report.format.value if hasattr(report.format, "value") else report.format

        report.status = ReportStatus.RUNNING  # AnomalyReport has no started_at column — only created_at/completed_at
        await db.commit()

    out_dir = Path(ANOMALY_REPORT_BASE_PATH) / str(execution.organization_id or "platform")
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"anomaly_report_{report_id}.{extension_for(fmt)}"
    full_path = out_dir / filename
    rel_path = str(Path(str(execution.organization_id or "platform")) / filename)

    column_name_by_result_id = {cr.id: cr.column_name for cr in column_results}
    writer = None
    row_count = 0
    try:
        writer = make_writer(fmt, full_path)
        await asyncio.to_thread(writer.write_summary, execution, job)
        await asyncio.to_thread(writer.write_column_results, column_results)

        offset = 0
        while True:
            async with sessionmaker() as db:
                chunk = list((
                    await db.execute(
                        select(AnomalyRecord).where(AnomalyRecord.execution_id == execution.id)
                        .order_by(AnomalyRecord.id).offset(offset).limit(ANOMALY_REPORT_CHUNK_SIZE)
                    )
                ).scalars().all())
            if not chunk:
                break
            await asyncio.to_thread(writer.write_records, chunk, column_name_by_result_id)
            row_count += len(chunk)
            offset += len(chunk)

            async with sessionmaker() as db:
                r = await db.get(AnomalyReport, report_id)
                r.row_count = row_count
                await db.commit()

            if len(chunk) < ANOMALY_REPORT_CHUNK_SIZE:
                break

        await asyncio.to_thread(writer.close)
        writer = None
        file_size = full_path.stat().st_size if full_path.exists() else None

        async with sessionmaker() as db:
            r = await db.get(AnomalyReport, report_id)
            r.status = ReportStatus.SUCCEEDED
            r.row_count = row_count
            r.file_path = rel_path
            r.file_size_bytes = file_size
            r.completed_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception as exc:
        await _mark_failed(sessionmaker, report_id, str(exc)[:500])
    finally:
        if writer is not None:
            # Only reached if an exception hit before the normal close()
            # above — close so the file handle isn't left open, but the
            # file itself is disclosed as incomplete via status=FAILED,
            # never presented as a real download.
            try:
                await asyncio.to_thread(writer.close)
            except Exception:
                pass
            try:
                full_path.unlink(missing_ok=True)
            except Exception:
                pass
