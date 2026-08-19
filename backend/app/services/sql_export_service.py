"""
sql_export_service — the API-facing layer for SQL Workspace background
exports (models/sql_export_job.py). Owns job creation/listing/lookup and
the path-sandboxed resolution a download route needs; the real streaming
work itself lives in execution/sql_export_executor.py (asyncio.create_task,
same fire-and-forget pattern as load_executor.py).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution import sql_export_executor
from app.execution.sql_export_executor import SQL_EXPORT_BASE_PATH
from app.models.sql_export_job import SqlExportFormat, SqlExportStatus, SqlWorkspaceExportJob
from app.models.user import User
from app.services import connection_service

VALID_FORMATS = {f.value for f in SqlExportFormat}


class SqlExportServiceError(Exception):
    """Bad request — a referenced connection doesn't exist (or isn't
    owned by this org), or an unknown export format was requested."""


class SqlExportNotFound(SqlExportServiceError):
    pass


async def create_export_job(
    db: AsyncSession, *, organization_id: int | None, connection_id: int, user_id: int | None,
    sql_text: str, export_format: str,
) -> SqlWorkspaceExportJob:
    if export_format not in VALID_FORMATS:
        raise SqlExportServiceError(f"format must be one of {sorted(VALID_FORMATS)}.")
    if not (sql_text or "").strip():
        raise SqlExportServiceError("SQL text is empty.")
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise SqlExportServiceError(f"Connection {connection_id} was not found.")

    job = SqlWorkspaceExportJob(
        organization_id=organization_id, connection_id=connection_id, sql_text=sql_text,
        format=SqlExportFormat(export_format), status=SqlExportStatus.PENDING, created_by=user_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    sql_export_executor.start_export(job.id)
    return job


async def get_export_job(db: AsyncSession, job_id: int, organization_id: int | None) -> SqlWorkspaceExportJob | None:
    stmt = select(SqlWorkspaceExportJob).where(SqlWorkspaceExportJob.id == job_id)
    if organization_id is not None:
        stmt = stmt.where(SqlWorkspaceExportJob.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_export_jobs(
    db: AsyncSession, organization_id: int | None, *, user_id: int | None, own_only: bool,
    connection_id: int | None, page: int, page_size: int,
) -> tuple[list[SqlWorkspaceExportJob], int]:
    stmt = select(SqlWorkspaceExportJob)
    count_stmt = select(func.count()).select_from(SqlWorkspaceExportJob)
    if organization_id is not None:
        stmt = stmt.where(SqlWorkspaceExportJob.organization_id == organization_id)
        count_stmt = count_stmt.where(SqlWorkspaceExportJob.organization_id == organization_id)
    if connection_id is not None:
        stmt = stmt.where(SqlWorkspaceExportJob.connection_id == connection_id)
        count_stmt = count_stmt.where(SqlWorkspaceExportJob.connection_id == connection_id)
    if own_only:
        stmt = stmt.where(SqlWorkspaceExportJob.created_by == user_id)
        count_stmt = count_stmt.where(SqlWorkspaceExportJob.created_by == user_id)

    total = (await db.execute(count_stmt)).scalar_one()
    bounded_size = max(1, min(page_size or 20, 100))
    stmt = stmt.order_by(desc(SqlWorkspaceExportJob.created_at)).offset(max(0, page - 1) * bounded_size).limit(bounded_size)
    rows = list((await db.execute(stmt)).scalars().all())
    return rows, total


def ensure_owner_or_elevated(job: SqlWorkspaceExportJob, user_id: int | None, elevated: bool) -> None:
    if elevated:
        return
    if job.created_by != user_id:
        raise SqlExportServiceError("Only this export's owner can cancel or download it.")


async def cancel_export_job(db: AsyncSession, job: SqlWorkspaceExportJob) -> bool:
    """Real cancellation — signals the running asyncio task via the
    executor's in-memory registry (same _cancelled-set pattern as
    load_executor.py). Returns False (not an error) if the job already
    reached a terminal status or was never actually running — never a
    fabricated success for a job that wasn't really in flight."""
    if job.status not in (SqlExportStatus.PENDING, SqlExportStatus.RUNNING):
        return False
    return sql_export_executor.request_cancel(job.id)


def resolve_download_path(job: SqlWorkspaceExportJob) -> Path | None:
    """Path-sandboxed resolution — same defense-in-depth pattern as
    api/pipeline_jobs.py's download_output_file: resolve against the real
    base directory and refuse anything that would escape it, even though
    file_path is only ever set by the executor itself (never client input)."""
    if not job.file_path:
        return None
    base = Path(SQL_EXPORT_BASE_PATH).resolve()
    resolved = (base / job.file_path).resolve()
    if base not in resolved.parents or not resolved.is_file():
        return None
    return resolved


async def user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    result = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row.id: row.full_name for row in result.all()}
