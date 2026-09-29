"""
transformation_service — CRUD for TransformationJob (Data Format
Transformation Engine, Phase 4). See models/transformation_job.py's
module docstring for the overall design; execution/transformation_executor.py
does the actual read/convert/write work in the background.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transformation_job import (
    TransformationJob, TransformationJobStatus, TransformEndpointKind, TransformFormat,
)
from app.models.user import User
from app.services import connection_service, uploaded_file_service
from app.database.connection import AsyncSessionLocal

TRANSFORM_EXPORT_BASE_PATH = "transform_exports"  # relative to backend process cwd — same convention as SQL_EXPORT_BASE_PATH


class TransformationServiceError(Exception):
    """Bad request — an invalid format/endpoint combination, or a referenced upload/connection that doesn't exist."""


async def create_job(
    db: AsyncSession, *, organization_id: int | None, name: str, user_id: int | None,
    source_kind: str, source_format: str,
    source_uploaded_file_id: int | None = None,
    source_connection_id: int | None = None, source_schema: str | None = None, source_object: str | None = None,
    target_kind: str, target_format: str,
    target_connection_id: int | None = None, target_schema: str | None = None, target_object: str | None = None,
    format_options: dict | None = None,
) -> TransformationJob:
    source_kind_e, source_fmt_e = TransformEndpointKind(source_kind), TransformFormat(source_format)
    target_kind_e, target_fmt_e = TransformEndpointKind(target_kind), TransformFormat(target_format)

    if source_kind_e == TransformEndpointKind.UPLOADED_FILE:
        if source_fmt_e == TransformFormat.TABLE:
            raise TransformationServiceError("An uploaded file can't be read as TABLE format — pick a real file format, or switch the source to a database connection.")
        if not source_uploaded_file_id:
            raise TransformationServiceError("Select a source file to convert.")
        record = await uploaded_file_service.get_by_id(db, source_uploaded_file_id, organization_id)
        if record is None:
            raise TransformationServiceError(f"Uploaded file {source_uploaded_file_id} was not found.")
    elif source_kind_e == TransformEndpointKind.CONNECTION:
        if source_fmt_e != TransformFormat.TABLE:
            raise TransformationServiceError("A database connection source can only be read as TABLE format.")
        if not source_connection_id or not source_object:
            raise TransformationServiceError("Select a source connection and object (table) to convert.")
        conn = await connection_service.get_connection(db, source_connection_id, organization_id)
        if conn is None:
            raise TransformationServiceError(f"Connection {source_connection_id} was not found.")
    else:
        raise TransformationServiceError(f"A source can't be {source_kind_e.value} — pick an uploaded file or a database connection.")

    if target_kind_e == TransformEndpointKind.DOWNLOAD_FILE:
        if target_fmt_e == TransformFormat.TABLE:
            raise TransformationServiceError("A downloadable output can't be TABLE format — pick a real file format, or switch the target to a database connection.")
    elif target_kind_e == TransformEndpointKind.CONNECTION:
        if target_fmt_e != TransformFormat.TABLE:
            raise TransformationServiceError("A database connection target can only be written as TABLE format.")
        if not target_connection_id or not target_object:
            raise TransformationServiceError("Select a target connection and object (table) to write into.")
        conn = await connection_service.get_connection(db, target_connection_id, organization_id)
        if conn is None:
            raise TransformationServiceError(f"Connection {target_connection_id} was not found.")
    else:
        raise TransformationServiceError(f"A target can't be {target_kind_e.value} — pick a downloadable file or a database connection.")

    job = TransformationJob(
        organization_id=organization_id, name=name.strip() or "Untitled Conversion",
        source_kind=source_kind_e, source_format=source_fmt_e,
        source_uploaded_file_id=source_uploaded_file_id,
        source_connection_id=source_connection_id, source_schema=source_schema, source_object=source_object,
        target_kind=target_kind_e, target_format=target_fmt_e,
        target_connection_id=target_connection_id, target_schema=target_schema, target_object=target_object,
        format_options=format_options or None,
        status=TransformationJobStatus.PENDING, created_by=user_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def get_job(db: AsyncSession, job_id: int, organization_id: int | None) -> TransformationJob | None:
    stmt = select(TransformationJob).where(TransformationJob.id == job_id)
    if organization_id is not None:
        stmt = stmt.where(TransformationJob.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_jobs(
    db: AsyncSession, organization_id: int | None, *, page: int, page_size: int,
) -> tuple[list[TransformationJob], int]:
    stmt = select(TransformationJob)
    count_stmt = select(func.count()).select_from(TransformationJob)
    if organization_id is not None:
        stmt = stmt.where(TransformationJob.organization_id == organization_id)
        count_stmt = count_stmt.where(TransformationJob.organization_id == organization_id)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(TransformationJob.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = list((await db.execute(stmt)).scalars().all())
    return rows, total


def ensure_owner_or_elevated(job: TransformationJob, user_id: int | None, is_elevated: bool) -> None:
    if is_elevated or (job.created_by is not None and job.created_by == user_id):
        return
    raise TransformationServiceError("You don't have access to this conversion job.")


def resolve_download_path(job: TransformationJob) -> Path | None:
    """Path-sandboxed resolution — same defense-in-depth pattern as
    sql_export_service.resolve_download_path()."""
    if not job.file_path:
        return None
    base = Path(TRANSFORM_EXPORT_BASE_PATH).resolve()
    resolved = (base / job.file_path).resolve()
    if base not in resolved.parents or not resolved.is_file():
        return None
    return resolved


async def user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}
