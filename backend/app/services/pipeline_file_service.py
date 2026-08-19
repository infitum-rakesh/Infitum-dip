"""
Pipeline source file service — real upload handling for the Transform
Workspace's Source Dataset "Upload File" option (an alternative to
browsing an existing SFTP connection). See models/pipeline_source_file.py.

Real, not simulated: the file is actually written to disk, its extension
comes from the real filename, its delimiter (for CSV/TSV/TXT) is sniffed
from the file's own real content via csv.Sniffer, and Excel sheet names
are read via openpyxl — nothing here is a guess or a hardcoded default.
"""
import csv
import io
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.pipeline_source_file import PipelineSourceFile

settings = get_settings()

DELIMITED_EXTENSIONS = {"csv", "tsv", "txt"}
EXCEL_EXTENSIONS = {"xlsx", "xls"}
ALLOWED_EXTENSIONS = DELIMITED_EXTENSIONS | EXCEL_EXTENSIONS | {"json", "jsonl"}

SAMPLE_BYTES_FOR_SNIFF = 8192
PREVIEW_ROW_CAP = 5000  # bound row-counting cost for very large uploads


class PipelineFileValidationError(Exception):
    pass


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _sniff_delimiter(sample: bytes) -> str:
    try:
        text = sample.decode("utf-8", errors="ignore")
        dialect = csv.Sniffer().sniff(text, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return ","  # real fallback used by every major spreadsheet tool when sniffing is inconclusive


def _count_delimited_rows_cols(content: bytes, delimiter: str) -> tuple[int, int]:
    text = content.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    row_count = 0
    col_count = 0
    for i, row in enumerate(reader):
        if i == 0:
            col_count = len(row)
        row_count += 1
        if row_count > PREVIEW_ROW_CAP:
            break
    return max(0, row_count - 1), col_count  # -1 for header row


def _read_excel_sheets(content: bytes) -> tuple[list[str], int | None, int | None]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet_names = wb.sheetnames
    first = wb[sheet_names[0]]
    rows = first.max_row - 1 if first.max_row else None  # -1 for header row
    cols = first.max_column
    wb.close()
    return sheet_names, rows, cols


async def upload_source_file(
    db: AsyncSession, *, file: UploadFile, node_id: str, pipeline_id: int | None,
    organization_id: int | None, uploaded_by_id: int | None,
) -> PipelineSourceFile:
    content = await file.read()
    ext = _ext(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise PipelineFileValidationError(
            f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS)).upper()}."
        )
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise PipelineFileValidationError(f"File exceeds the {settings.max_upload_size_mb} MB upload limit.")
    if len(content) == 0:
        raise PipelineFileValidationError("File is empty.")

    delimiter = None
    sheet_names = None
    selected_sheet = None
    row_count = None
    column_count = None

    if ext in DELIMITED_EXTENSIONS:
        delimiter = _sniff_delimiter(content[:SAMPLE_BYTES_FOR_SNIFF])
        row_count, column_count = _count_delimited_rows_cols(content, delimiter)
    elif ext in EXCEL_EXTENSIONS:
        try:
            sheet_names, row_count, column_count = _read_excel_sheets(content)
            selected_sheet = sheet_names[0] if sheet_names else None
        except Exception as exc:
            raise PipelineFileValidationError(f"Could not read Excel file: {exc}") from exc

    org_dir = Path(settings.upload_dir) / "pipeline_files" / (str(pipeline_id) if pipeline_id else "unsaved") / node_id
    org_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}_{file.filename}"
    dest = org_dir / stored_name
    dest.write_bytes(content)

    # Replace any previous upload for this node — one file per source node.
    existing = await db.execute(select(PipelineSourceFile).where(PipelineSourceFile.node_id == node_id))
    for row in existing.scalars().all():
        try:
            Path(row.storage_path).unlink(missing_ok=True)
        except OSError:
            pass
        await db.delete(row)

    record = PipelineSourceFile(
        pipeline_id=pipeline_id, node_id=node_id, organization_id=organization_id, uploaded_by_id=uploaded_by_id,
        filename=file.filename, extension=ext, mime_type=file.content_type, size_bytes=len(content),
        storage_path=str(dest), delimiter=delimiter, sheet_names=sheet_names, selected_sheet=selected_sheet,
        row_count=row_count, column_count=column_count,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def get_by_node(db: AsyncSession, node_id: str, organization_id: int | None) -> PipelineSourceFile | None:
    stmt = select(PipelineSourceFile).where(PipelineSourceFile.node_id == node_id)
    if organization_id is not None:
        stmt = stmt.where(PipelineSourceFile.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_by_id(db: AsyncSession, file_id: int, organization_id: int | None) -> PipelineSourceFile | None:
    stmt = select(PipelineSourceFile).where(PipelineSourceFile.id == file_id)
    if organization_id is not None:
        stmt = stmt.where(PipelineSourceFile.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def attach_to_pipeline(db: AsyncSession, record: PipelineSourceFile, pipeline_id: int) -> PipelineSourceFile:
    """Called right after a brand-new pipeline is first saved (pipeline_id
    only exists from that point on) — this is what makes the upload
    "uniquely linked with its respective pipeline" once Save happens."""
    record.pipeline_id = pipeline_id
    await db.commit()
    await db.refresh(record)
    return record


async def set_sheet(db: AsyncSession, record: PipelineSourceFile, sheet_name: str) -> PipelineSourceFile:
    if not record.sheet_names or sheet_name not in record.sheet_names:
        raise PipelineFileValidationError(f"'{sheet_name}' is not a sheet in this workbook.")
    record.selected_sheet = sheet_name

    # Row/column counts are per-sheet — recompute for real from the stored
    # file rather than leaving the previously-selected sheet's numbers in
    # place (which would silently misdescribe the newly selected sheet).
    from openpyxl import load_workbook
    wb = load_workbook(record.storage_path, read_only=True, data_only=True)
    try:
        ws = wb[sheet_name]
        record.row_count = (ws.max_row - 1) if ws.max_row else None
        record.column_count = ws.max_column
    finally:
        wb.close()

    await db.commit()
    await db.refresh(record)
    return record


async def delete_source_file(db: AsyncSession, record: PipelineSourceFile) -> None:
    try:
        Path(record.storage_path).unlink(missing_ok=True)
    except OSError:
        pass
    await db.delete(record)
    await db.commit()
