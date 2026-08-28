"""
uploaded_file_service — real upload handling for the "Upload File" option
on Validation's object picker and Reconciliation's Source/Target pickers.
See models/uploaded_dataset_file.py.

Deliberately reuses the SAME connectors/files/ stack SFTP and Local File
pipeline sources already use, via a real LocalFileConnector built directly
in-process (config={"base_path": <this upload's own directory>}, no
secrets, no Connection row) — not a bespoke reader. That means:
  - column names come from a real header read (connectors/files/base.py's
    get_columns), never guessed;
  - delimiter handling for .csv/.tsv is whatever connectors/files/
    delimited.py already hardcodes per extension (comma / tab) — this
    layer does NOT sniff delimiters the way pipeline_file_service.py does
    for the pandas-based Transform Workspace path; a real, disclosed
    difference between the two upload surfaces, not an oversight;
  - row counting is a real, bounded full read of the file (uploads are
    capped at settings.max_upload_size_mb, so this is cheap) rather than
    the "unsupported, returns None" default FileConnectorBase.row_count()
    uses for browsing-time previews of arbitrarily large remote files.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError
from app.connectors.files.local import LocalFileConnector
from app.connectors.files.registry import get_reader
from app.core.config import get_settings
from app.models.uploaded_dataset_file import UploadedDatasetFile

settings = get_settings()

ROW_COUNT_CAP = 500_000  # a real, disclosed bound — matches reconciliation/engine.py's MAX_RECONCILE_ROWS scale


class UploadedFileValidationError(Exception):
    pass


def _ext(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def build_connector(record: UploadedDatasetFile) -> LocalFileConnector:
    """The one place a stored UploadedDatasetFile becomes a real,
    connector-interface object — used by both reconciliation and
    validation's file-backed code paths so there's exactly one way this
    happens, not one per caller."""
    return LocalFileConnector(config={"base_path": record.storage_dir}, secrets={}, cache_key=f"uploaded:{record.id}")


def _real_row_count(connector: LocalFileConnector, filename: str) -> int:
    n = 0
    stream = connector.open_row_stream(filename)
    try:
        while True:
            rows = stream.fetch_chunk(5000)
            if not rows:
                break
            n += len(rows)
            if n > ROW_COUNT_CAP:
                break
    finally:
        stream.close()
    return n


async def upload_dataset_file(
    db: AsyncSession, *, file: UploadFile, organization_id: int | None, uploaded_by_id: int | None,
) -> UploadedDatasetFile:
    content = await file.read()
    ext = _ext(file.filename)
    try:
        get_reader(file.filename)  # validates the extension is real-streamable; raises ConnectorUnsupportedError otherwise
    except ConnectorError as exc:
        raise UploadedFileValidationError(str(exc)) from exc

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise UploadedFileValidationError(f"File exceeds the {settings.max_upload_size_mb} MB upload limit.")
    if len(content) == 0:
        raise UploadedFileValidationError("File is empty.")

    storage_dir = Path(settings.upload_dir) / "dataset_files" / uuid.uuid4().hex
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / file.filename).write_bytes(content)

    connector = LocalFileConnector(config={"base_path": str(storage_dir)}, secrets={})
    try:
        columns = connector.get_columns(file.filename)
        row_count = _real_row_count(connector, file.filename)
    except ConnectorError as exc:
        raise UploadedFileValidationError(f"Could not read '{file.filename}': {exc}") from exc

    record = UploadedDatasetFile(
        organization_id=organization_id, uploaded_by_id=uploaded_by_id,
        filename=file.filename, extension=ext.lstrip("."), mime_type=file.content_type, size_bytes=len(content),
        storage_dir=str(storage_dir), row_count=row_count, column_count=len(columns),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def get_by_id(db: AsyncSession, file_id: int, organization_id: int | None) -> UploadedDatasetFile | None:
    stmt = select(UploadedDatasetFile).where(UploadedDatasetFile.id == file_id)
    if organization_id is not None:
        stmt = stmt.where(UploadedDatasetFile.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_columns(record: UploadedDatasetFile) -> list[str]:
    connector = build_connector(record)
    try:
        return [c.name for c in connector.get_columns(record.filename)]
    except ConnectorError as exc:
        raise UploadedFileValidationError(str(exc)) from exc


async def delete_uploaded_file(db: AsyncSession, record: UploadedDatasetFile) -> None:
    try:
        path = Path(record.storage_dir)
        for child in path.glob("*"):
            child.unlink(missing_ok=True)
        path.rmdir()
    except OSError:
        pass
    await db.delete(record)
    await db.commit()
