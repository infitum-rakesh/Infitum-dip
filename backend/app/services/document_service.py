"""
DocumentProcessingService — business logic for the Documents repository.

Architecture (per module spec):

    Documents
        v
    Document Processing Service   <-- this module
        v
    Document Intelligence Pipeline (Image Scanner -> OCR -> AI Extraction -> Dataset)

**OCR is real** (see app/ocr/engine.py — Tesseract via pytesseract, PDF
pages rasterized with PyMuPDF): `process_document()` below actually opens
the uploaded file and extracts real text/confidence/bounding boxes, not
fabricated output.

**AI Extraction is also real** (see _run_ai_extraction() below): it calls
the same LLM engine as the Document Intelligence wizard
(services/extraction_service.py) — auto-detects a schema from the
document's real OCR text, then extracts genuine field values, via
whichever AI Provider connection is configured for the organization. If
no AI Provider connection exists yet, this now falls back to the
**Custom Extraction Engine** (app/extraction/custom_engine.py) — a real,
deterministic, rule-based extractor, NOT a language model — so the stage
still produces genuine structured output with zero configuration
required. The stage is only genuinely skipped when there's no OCR text
to work from at all (recorded as such in the timeline).

Image Scanner and Validation-stage timing remain simulated — a real
image-quality analyzer doesn't exist yet, and Validation only formats the
now-real extraction/confidence numbers above it. Simulated stages are
clearly marked below so it's obvious which numbers are real and which are
placeholders.
"""
import asyncio
import base64
import datetime as _dt
import decimal
import hashlib
import json
import mimetypes
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal

from app.connectors.base import ConnectorError
from app.core.config import get_settings
from app.database import tenant_engine_registry
from app.models.connection import Connection, ConnectionCategory, ConnectionStatus
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.user import User
from app.ocr.engine import OcrEngineError, OcrNotApplicableError, render_pages_as_png
from app.ocr.language_config import LANGUAGE_CONFIG
from app.ocr.language_errors import LanguageErrorCode, LanguageProcessingError
from app.ocr.multilingual_ocr_service import detect_language_only, evaluate_ocr_quality, run_multilingual_ocr
from app.services import extraction_service
from app.services import document_taxonomies
from app.services import document_storage_service
from app.services import document_webhook_service
from app.services import evidence_service
from app.services import settings_service
from app.services.connection_service import build_connector, get_connection

settings = get_settings()

# ── Upload validation ────────────────────────────────────────────

EXTENSION_TYPE_MAP: dict[str, DocumentType] = {
    "pdf": DocumentType.PDF,
    "png": DocumentType.IMAGE,
    "jpg": DocumentType.IMAGE,
    "jpeg": DocumentType.IMAGE,
    "tiff": DocumentType.IMAGE,
    "tif": DocumentType.IMAGE,
    "bmp": DocumentType.IMAGE,
    "docx": DocumentType.WORD,
    "xlsx": DocumentType.EXCEL,
    "csv": DocumentType.EXCEL,
    "txt": DocumentType.TEXT,
}

PROCESSING_STATUSES = {DocumentStatus.SCANNING, DocumentStatus.OCR_PROCESSING, DocumentStatus.AI_EXTRACTION}


class DocumentValidationError(Exception):
    """Raised for invalid uploads (bad extension, too large)."""


class DocumentLanguageConfigError(Exception):
    """Raised when a caller sends an unsupported OCR language selection."""


def normalize_ocr_language_config(language_config: dict | None) -> dict | None:
    if not language_config:
        return None
    mode = str(language_config.get("mode") or "auto").lower()
    selected = language_config.get("selected_languages") or language_config.get("languages") or []
    if isinstance(selected, str):
        selected = [c.strip() for c in selected.split(",") if c.strip()]
    selected = list(dict.fromkeys(str(c).strip().lower() for c in selected if str(c).strip()))
    primary = str(language_config.get("primary_language") or language_config.get("language") or "auto").lower()

    if mode not in ("auto", "explicit"):
        raise DocumentLanguageConfigError("Unsupported OCR language mode. Use 'auto' or 'explicit'.")
    if primary != "auto" and primary not in LANGUAGE_CONFIG:
        raise DocumentLanguageConfigError(f"Unsupported OCR language '{primary}'.")
    unsupported = [c for c in selected if c not in LANGUAGE_CONFIG]
    if unsupported:
        raise DocumentLanguageConfigError(f"Unsupported OCR language(s): {', '.join(unsupported)}.")
    if mode == "explicit" and primary == "auto" and len(selected) == 1:
        primary = selected[0]
    if mode == "explicit" and primary != "auto" and primary not in selected:
        selected.insert(0, primary)
    selected = list(dict.fromkeys(["en", *selected]))
    return {"mode": mode, "selected_languages": selected, "primary_language": primary}


def resolve_document_language_config(doc: Document, di_settings: dict) -> tuple[list[str], str, dict]:
    stored = normalize_ocr_language_config(doc.language_config) if doc.language_config else None
    if stored:
        supported = stored["selected_languages"] or ["en"]
        primary = stored["primary_language"] or "auto"
        return supported, primary, stored
    supported = di_settings.get("supported_languages") or ["en"]
    primary = di_settings.get("primary_language") or "auto"
    return supported, primary, {"mode": "org_default", "selected_languages": supported, "primary_language": primary}


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def guess_doc_type(filename: str) -> DocumentType:
    return EXTENSION_TYPE_MAP.get(_ext(filename), DocumentType.OTHER)


def _validate_upload(filename: str, size_bytes: int) -> None:
    ext = _ext(filename)
    if ext not in EXTENSION_TYPE_MAP:
        allowed = ", ".join(sorted(EXTENSION_TYPE_MAP)).upper()
        raise DocumentValidationError(f"Unsupported file type '.{ext}'. Allowed: {allowed}.")
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if size_bytes > max_bytes:
        raise DocumentValidationError(f"File exceeds the {settings.max_upload_size_mb} MB upload limit.")
    if size_bytes == 0:
        raise DocumentValidationError("File is empty.")


# ── Upload / storage ─────────────────────────────────────────────

async def save_upload(
    db: AsyncSession,
    *,
    file: UploadFile,
    organization_id: int | None,
    uploaded_by_id: int | None,
    language_config: dict | None = None,
) -> tuple[Document, int | None]:
    """Returns (doc, duplicate_of_id). duplicate_of_id is the id of an
    existing document in the same organization with byte-identical content
    (same SHA-256), if one exists — informational only, never blocks the
    upload: a genuine re-upload (reprocessing after a fix, re-submitting a
    scan) is a normal, legitimate thing to do. Callers surface this so the
    user can tell "this is the same file as doc_X" instead of silently
    getting a second, unexplained row."""
    content = await file.read()
    _validate_upload(file.filename, len(content))
    normalized_language_config = normalize_ocr_language_config(language_config)
    content_hash = hashlib.sha256(content).hexdigest()

    duplicate_of_id = None
    dup_stmt = select(Document.id).where(Document.content_hash == content_hash)
    if organization_id is not None:
        dup_stmt = dup_stmt.where(Document.organization_id == organization_id)
    dup_result = await db.execute(dup_stmt.order_by(Document.id.asc()).limit(1))
    duplicate_of_id = dup_result.scalar_one_or_none()

    org_dir = Path(settings.upload_dir) / (str(organization_id) if organization_id else "unassigned")
    org_dir.mkdir(parents=True, exist_ok=True)

    stored_name = f"{uuid.uuid4().hex}_{file.filename}"
    dest = org_dir / stored_name
    dest.write_bytes(content)

    doc = Document(
        organization_id=organization_id,
        uploaded_by_id=uploaded_by_id,
        filename=file.filename,
        doc_type=guess_doc_type(file.filename),
        mime_type=file.content_type,
        size_bytes=len(content),
        page_count=1,
        storage_path=str(dest),
        content_hash=content_hash,
        language_config=normalized_language_config,
        status=DocumentStatus.UPLOADED,
        timeline=[_stage_record("Uploaded", "completed", datetime.now(timezone.utc), datetime.now(timezone.utc))],
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc, duplicate_of_id


async def save_from_source(
    db: AsyncSession, *, content: bytes, filename: str, organization_id: int | None,
    source_connection_id: int | None = None, source_path: str | None = None, source_schedule_id: int | None = None,
) -> Document:
    """Scheduled Document Intelligence Workflow's counterpart to
    save_upload() above — same validation/storage/content-hash logic,
    bytes-based (already downloaded by a connector — see
    document_schedule_service.py) instead of a FastAPI UploadFile, and
    stamps the new source_* columns so Document Processing History (spec
    section 44) can show where this document actually came from. Deliberately
    NOT deduped against an existing same-hash Document row the way
    save_upload() is informational-only about — schedule-side dedup happens
    one layer up, against DocumentSourceRegistry (see needs_processing()),
    which is the durable per-SOURCE-identity ledger the spec actually asks
    for; by the time this function is called the caller has already decided
    this file needs a real Document row.

    source_connection_id/source_path are optional (ZB6) — a ZIP-batch-
    originated document has no real Connection at all, so these stay NULL
    rather than being stamped with a fake sentinel connection id, which
    would misrepresent Document Processing History. DocumentBatchFile.
    document_id is the real reverse-join for "which batch produced this
    Document" in that case."""
    _validate_upload(filename, len(content))
    content_hash = hashlib.sha256(content).hexdigest()

    org_dir = Path(settings.upload_dir) / (str(organization_id) if organization_id else "unassigned")
    org_dir.mkdir(parents=True, exist_ok=True)

    stored_name = f"{uuid.uuid4().hex}_{filename}"
    dest = org_dir / stored_name
    dest.write_bytes(content)

    doc = Document(
        organization_id=organization_id,
        uploaded_by_id=None,
        filename=filename,
        doc_type=guess_doc_type(filename),
        # Real bug found live: this used to hardcode None (there's no
        # UploadFile.content_type available on this bytes-based path), which
        # left GET /documents/{id}/file falling back to
        # "application/octet-stream" — browsers force-download an
        # octet-stream response instead of rendering it inline, so every
        # document created via a ZIP batch or a document schedule (both of
        # which only ever call save_from_source(), never save_upload())
        # downloaded instead of previewing. mimetypes.guess_type() derives
        # the same thing save_upload() gets for free from the browser.
        mime_type=mimetypes.guess_type(filename)[0],
        size_bytes=len(content),
        page_count=1,
        storage_path=str(dest),
        content_hash=content_hash,
        source_connection_id=source_connection_id,
        source_path=source_path,
        source_schedule_id=source_schedule_id,
        status=DocumentStatus.UPLOADED,
        timeline=[_stage_record("Discovered", "completed", datetime.now(timezone.utc), datetime.now(timezone.utc))],
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def save_database_record(
    db: AsyncSession, *, row: dict, organization_id: int | None,
    source_connection_id: int, source_path: str, file_name: str, source_schedule_id: int | None,
) -> Document:
    """PAY18 — DATABASE-kind document schedules' counterpart to
    save_from_source() above: there is no uploaded/downloaded FILE at all
    here (the "document" is a query result row), so this deliberately
    does NOT go through _validate_upload()'s extension allow-list — a
    ".json" snapshot isn't one of the OCR-capable extensions manual
    upload accepts, and shouldn't be; that allow-list exists to gate what
    OCR can attempt, which is irrelevant here (see process_document()'s
    preextracted_fields branch: this document's pipeline never calls
    read_file()/run_ocr() at all).

    The row is still persisted to disk as an indented JSON snapshot
    (content_hash'd exactly like every other stored document) purely as
    a genuine evidence/audit artifact — "what did the source system
    actually return for this record" — mirroring why save_upload() keeps
    the original bytes rather than only the extracted fields."""
    # ensure_ascii=False — this snapshot is a genuine evidence artifact of
    # the source row; a multilingual field value in it must stay real
    # UTF-8 glyphs, not \uXXXX escapes (see connection.py's json_serializer
    # comment for the same class of bug fixed at the DB-column level).
    content = json.dumps(row, indent=2, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    content_hash = hashlib.sha256(content).hexdigest()

    org_dir = Path(settings.upload_dir) / (str(organization_id) if organization_id else "unassigned")
    org_dir.mkdir(parents=True, exist_ok=True)

    stored_name = f"{uuid.uuid4().hex}_{file_name}.json"
    dest = org_dir / stored_name
    dest.write_bytes(content)

    doc = Document(
        organization_id=organization_id,
        uploaded_by_id=None,
        filename=f"{file_name}.json",
        doc_type=DocumentType.OTHER,
        mime_type="application/json",
        size_bytes=len(content),
        page_count=1,
        storage_path=str(dest),
        content_hash=content_hash,
        source_connection_id=source_connection_id,
        source_path=source_path,
        source_schedule_id=source_schedule_id,
        status=DocumentStatus.UPLOADED,
        timeline=[_stage_record("Discovered (Database Record)", "completed", datetime.now(timezone.utc), datetime.now(timezone.utc))],
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


# Normalizes an arbitrary source column name down to the same snake_case
# key shape every document_taxonomies.py field name already uses (e.g. a
# SQL alias of "Invoice Number" or "InvoiceNumber" both resolve to
# "invoice_number") — so a DATABASE schedule's SELECT ... AS aliases don't
# have to be written in exactly that shape to be recognized.
def _normalize_field_key(name: str) -> str:
    s = "".join(ch if ch.isalnum() else "_" for ch in str(name).strip())
    s = "".join(f"_{ch.lower()}" if ch.isupper() else ch for ch in s)
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_").lower()


def _json_safe_value(value):
    """A real DB driver hands back native Python types for DATE/NUMERIC/
    etc columns (datetime.date, decimal.Decimal, ...) — genuinely correct
    values, but not directly JSON-serializable, and doc.extraction_result/
    doc.dataset are both persisted as JSON columns. Every other field
    producer in this file (_reshape_extraction_fields, apply_manual_
    extraction) only ever sees strings the AI model or a human typed, so
    this gap is specific to a DATABASE-kind schedule's raw query rows —
    normalizes to the same plain str/int/float/bool shape those already
    use, never dropping or truncating the real value."""
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def build_fields_from_record(row: dict, document_type: str | None) -> list[dict]:
    """PAY18 — turns one DATABASE query result row directly into the same
    field shape _run_ai_extraction()/apply_manual_extraction() produce,
    with NO AI model and NO OCR involved: the source system's own columns
    ARE the structured data, so there's nothing to extract FROM — mapping
    is a straight rename onto the canonical taxonomy's field names (see
    document_taxonomies.py), the same honesty standard the Custom
    Extraction Engine already applies to unstructured documents (real
    deterministic logic, not a language model, clearly labeled as such —
    see doc.extraction_model = "STRUCTURED_SOURCE" in process_document()).

    A column whose normalized name matches a known taxonomy field is
    mapped onto it; every other column is still included as its own
    scalar field (normalized name, not silently dropped) so an org's
    genuinely extra columns remain visible — same principle as AI
    Extraction surfacing fields beyond the canonical list.

    A `line_items` column (list of dicts, or a JSON-encoded string
    thereof) becomes the taxonomy's real repeating table field instead of
    an opaque scalar blob — the one genuinely repeating structure invoice
    intake needs (see document_taxonomies.INVOICE_LINE_ITEMS_TABLE)."""
    table_def = None
    if document_type:
        taxonomy = document_taxonomies.TAXONOMIES.get(document_type)
        if taxonomy:
            table_def = taxonomy.get("table")

    fields: list[dict] = []
    for raw_key, raw_value in row.items():
        key = _normalize_field_key(raw_key)
        if not key:
            continue

        if table_def and key == table_def["name"]:
            table_rows_raw = raw_value
            if isinstance(table_rows_raw, str):
                try:
                    table_rows_raw = json.loads(table_rows_raw) if table_rows_raw.strip() else []
                except (json.JSONDecodeError, AttributeError):
                    table_rows_raw = []
            table_columns = [c["name"] for c in table_def["columns"]]
            rows = []
            for r in (table_rows_raw or []):
                if isinstance(r, dict):
                    rows.append({c: ("" if r.get(c) is None else _json_safe_value(r.get(c))) for c in table_columns})
            fields.append({
                "name": table_def["name"], "type": "table", "columns": table_columns, "rows": rows,
                "value": f"{len(rows)} row{'s' if len(rows) != 1 else ''}" if rows else "",
                "confidence": 100.0, "source_page": 1, "bbox": None,
                "validation_status": "valid" if rows else "invalid",
                "suggested_correction": None, "missing": len(rows) == 0,
            })
            continue

        missing = raw_value is None or raw_value == ""
        value = "" if missing else _json_safe_value(raw_value)
        fields.append({
            "name": key, "type": "scalar", "value": value,
            "confidence": 100.0, "source_page": 1, "bbox": None,
            "validation_status": "valid" if not missing else "invalid",
            "suggested_correction": None, "missing": missing,
        })
    return fields


def read_file(document: Document) -> bytes:
    return Path(document.storage_path).read_bytes()


def delete_file(document: Document) -> None:
    try:
        os.remove(document.storage_path)
    except OSError:
        pass


# ── Query / list ─────────────────────────────────────────────────

def _stage_record(stage, status, started, ended, confidence=None, error=None) -> dict:
    return {
        "stage": stage,
        "status": status,
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "duration_seconds": round((ended - started).total_seconds(), 2) if started and ended else None,
        "confidence": confidence,
        "error": error,
    }


async def uploader_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


DATE_PRESETS = {"today", "yesterday", "last_7_days", "last_30_days"}


def _matches_date_preset(uploaded_at: datetime, preset: str | None, date_from: str | None, date_to: str | None) -> bool:
    if not preset or preset == "all":
        return True
    now = datetime.now(timezone.utc)
    ts = uploaded_at if uploaded_at.tzinfo else uploaded_at.replace(tzinfo=timezone.utc)
    if preset == "today":
        return ts.date() == now.date()
    if preset == "yesterday":
        return (now.date() - ts.date()).days == 1
    if preset == "last_7_days":
        return (now - ts).days <= 7
    if preset == "last_30_days":
        return (now - ts).days <= 30
    if preset == "custom":
        if date_from and ts.date() < datetime.fromisoformat(date_from).date():
            return False
        if date_to and ts.date() > datetime.fromisoformat(date_to).date():
            return False
        return True
    return True


async def list_documents(
    db: AsyncSession,
    *,
    organization_id: int | None,
    requesting_user_id: int,
    search: str = "",
    doc_type: str | None = None,
    status_filter: str | None = None,
    date_preset: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    owner: str | None = None,  # current_user | organization | specific_user
    owner_user_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
    sort_by: str = "uploaded_at",
    sort_dir: str = "desc",
) -> tuple[list[Document], int, dict[int, str]]:
    stmt = select(Document)
    if organization_id is not None:
        stmt = stmt.where(Document.organization_id == organization_id)
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    if status_filter:
        stmt = stmt.where(Document.status == status_filter)
    if owner == "current_user":
        stmt = stmt.where(Document.uploaded_by_id == requesting_user_id)
    elif owner == "specific_user" and owner_user_id:
        stmt = stmt.where(Document.uploaded_by_id == owner_user_id)

    result = await db.execute(stmt)
    docs = list(result.scalars().all())

    uploader_ids = {d.uploaded_by_id for d in docs if d.uploaded_by_id}
    name_map = await uploader_name_map(db, uploader_ids)

    # Free-text search + date filter applied in Python (consistent with the
    # rest of this codebase's data_catalog_service, which is also
    # Python-filtered rather than SQL-filtered).
    def matches(d: Document) -> bool:
        if not _matches_date_preset(d.uploaded_at, date_preset, date_from, date_to):
            return False
        if not search:
            return True
        needle = search.lower()
        haystacks = [
            d.filename, str(d.id), d.doc_type.value if d.doc_type else "",
            d.status.value if d.status else "", d.pipeline or "",
            name_map.get(d.uploaded_by_id, ""),
        ]
        if d.extraction_result:
            for f in d.extraction_result.get("fields", []):
                haystacks.append(str(f.get("value", "")))
                haystacks.append(str(f.get("name", "")))
        return any(needle in h.lower() for h in haystacks if h)

    filtered = [d for d in docs if matches(d)]

    reverse = sort_dir == "desc"
    filtered.sort(key=lambda d: getattr(d, sort_by, d.uploaded_at) or d.uploaded_at, reverse=reverse)

    total = len(filtered)
    start = (page - 1) * page_size
    page_items = filtered[start:start + page_size]
    return page_items, total, name_map


async def get_document(db: AsyncSession, document_id: int, organization_id: int | None) -> Document | None:
    stmt = select(Document).where(Document.id == document_id)
    if organization_id is not None:
        stmt = stmt.where(Document.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_stats(db: AsyncSession, organization_id: int | None) -> dict:
    stmt = select(Document)
    if organization_id is not None:
        stmt = stmt.where(Document.organization_id == organization_id)
    result = await db.execute(stmt)
    docs = list(result.scalars().all())

    return {
        "total_documents": len(docs),
        "processing": sum(1 for d in docs if d.status in PROCESSING_STATUSES),
        "completed": sum(1 for d in docs if d.status == DocumentStatus.COMPLETED),
        "needs_review": sum(1 for d in docs if d.status == DocumentStatus.NEEDS_REVIEW),
        "failed": sum(1 for d in docs if d.status == DocumentStatus.FAILED),
        "storage_used_bytes": sum(d.size_bytes for d in docs),
    }


async def get_facets(db: AsyncSession, organization_id: int | None) -> dict:
    stmt = select(Document)
    if organization_id is not None:
        stmt = stmt.where(Document.organization_id == organization_id)
    result = await db.execute(stmt)
    docs = list(result.scalars().all())

    uploader_ids = {d.uploaded_by_id for d in docs if d.uploaded_by_id}
    name_map = await uploader_name_map(db, uploader_ids)
    uploaders = [{"id": uid, "full_name": name} for uid, name in name_map.items()]
    pipelines = sorted({d.pipeline for d in docs if d.pipeline})
    return {"uploaders": uploaders, "pipelines": pipelines}


# ── Update / delete ──────────────────────────────────────────────

async def update_document(db: AsyncSession, doc: Document, *, filename: str | None, folder: str | None) -> Document:
    if filename:
        doc.filename = filename
    if folder is not None:
        doc.folder = folder
    await db.commit()
    await db.refresh(doc)
    return doc


async def delete_document(db: AsyncSession, doc: Document) -> None:
    """Hard-deletes the Document row and its physical file. Real bug found
    live: this used to leave dangling references behind in two other
    tables — `ReviewQueueItem.document_id` and `DocumentBatchFile.
    document_id`/`duplicate_of_document_id` are plain FK columns with no
    ON DELETE behavior configured (and SQLite doesn't enforce FKs by
    default anyway), so a deleted document's review item stayed forever,
    and any ZIP batch that had processed it kept a "view" link pointing at
    a document that no longer existed (404) — including a batch's
    duplicate-detection record, which could look like a phantom "existing"
    match for a later re-upload even though the exact match was actually
    the deleted document that the same later content just happens not to
    equal (the dedup query itself is fine; the confusion is from BatchDetail
    silently offering a dead link with nothing to explain it). Cleaned up
    explicitly here rather than adding DB-level cascade, since TenantBase
    models make no assumption about which DB engine enforces FKs.
    """
    from app.models.review_queue import ReviewQueueItem
    from app.models.document_batch import DocumentBatchFile

    await db.execute(delete(ReviewQueueItem).where(ReviewQueueItem.document_id == doc.id))
    await db.execute(
        update(DocumentBatchFile).where(DocumentBatchFile.document_id == doc.id).values(document_id=None)
    )
    await db.execute(
        update(DocumentBatchFile).where(DocumentBatchFile.duplicate_of_document_id == doc.id)
        .values(duplicate_of_document_id=None)
    )
    delete_file(doc)
    await db.delete(doc)
    await db.commit()


async def send_to_review(db: AsyncSession, doc: Document) -> Document:
    doc.status = DocumentStatus.NEEDS_REVIEW
    await db.commit()
    await db.refresh(doc)
    return doc


class ManualExtractionError(Exception):
    pass


async def apply_manual_extraction(
    db: AsyncSession, doc: Document, *, fields_in: list[dict], tables_in: list[dict],
) -> Document:
    """Lets a user build structured data by hand — reading the document's
    real OCR text (always available once OCR has run, even when AI
    Extraction found nothing or was never configured) and typing in the
    fields/rows themselves — for exactly the case AI Extraction can't cover
    on its own: no AI Provider connection configured, or the model
    genuinely came back with nothing usable. The result is stored in the
    identical shape _run_ai_extraction() produces (fields with type
    scalar/table), so build_dataset() and every downstream consumer
    (Review Queue, Structured Dataset tab, "Continue to Transformation")
    treat manually- and AI-authored data the same way — a human directly
    asserting a value is by definition 100% confidence, not a fabricated
    number standing in for a real one."""
    if not fields_in and not tables_in:
        raise ManualExtractionError("Provide at least one field or table row to save.")

    fields: list[dict] = []
    for f in fields_in:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        value = f.get("value") if f.get("value") is not None else ""
        fields.append({
            "name": name, "type": "scalar", "value": value, "confidence": 100.0,
            "source_page": 1, "bbox": None, "validation_status": "valid",
            "suggested_correction": None, "missing": value == "",
        })

    for t in tables_in:
        name = (t.get("name") or "").strip()
        columns = [c for c in (t.get("columns") or []) if c]
        if not name or not columns:
            continue
        rows = [{c: (r.get(c) or "") for c in columns} for r in (t.get("rows") or [])]
        fields.append({
            "name": name, "type": "table", "columns": columns, "rows": rows,
            "value": f"{len(rows)} row{'s' if len(rows) != 1 else ''}" if rows else "",
            "confidence": 100.0, "source_page": 1, "bbox": None,
            "validation_status": "valid" if rows else "invalid",
            "suggested_correction": None, "missing": len(rows) == 0,
        })

    if not fields:
        raise ManualExtractionError("Provide at least one field with a name.")

    doc.extraction_result = {"fields": fields, "business_rule_flag": False, "manual": True}
    doc.dataset = build_dataset(fields)
    doc.extraction_percent = 100.0
    doc.status = DocumentStatus.COMPLETED
    doc.error_message = None
    now = datetime.now(timezone.utc)
    doc.timeline = list(doc.timeline or []) + [_stage_record("Manual Structuring", "completed", now, now, confidence=100.0)]
    await db.commit()
    await db.refresh(doc)

    from app.services import review_queue_service as review_svc
    await review_svc.auto_resolve_for_document(db, doc.id)
    await db.refresh(doc)
    return doc


# ── Simulated Document Intelligence pipeline ──────────────────────
# See module docstring: Image Scanner and Validation-stage timing remain
# simulated (no real image-quality analyzer exists yet). AI Extraction is
# REAL as of this fix — see _pick_ai_connections()/_run_ai_extraction()
# below, which call the same LLM engine as the Document Intelligence
# wizard (extraction_service.py) instead of generating fabricated field
# values from a fixed template. Kept in this section only because it
# feeds off the same synthetic timeline cursor as the still-simulated
# stages around it.

PIPELINE_NAMES = ["Invoice Pipeline", "Receipt Pipeline", "Contract Pipeline", "General Document Pipeline"]

# Substrings matched against extracted field names (lowercased) to find
# "the total amount" field for the business-rule check below. Real AI
# Extraction proposes its own field names per document (there's no fixed
# schema any more), so this can't be an exact-name lookup like the old
# simulation's `f["name"] == "Total"` — matching is heuristic but the
# fields it matches against are genuinely extracted, not fabricated.
_TOTAL_FIELD_HINTS = ("total", "amount_due", "grand_total", "balance_due")


async def _pick_ai_connections(db: AsyncSession, organization_id: int | None) -> list[Connection]:
    """Every ACTIVE AI_PROVIDER connection for this org, in id order (same
    connections configured on the Connections page / offered by the
    Document Intelligence wizard's AI Agent picker). Used — instead of
    just grabbing the first one — so that a connection which is
    genuinely broken at RUNTIME (e.g. an Anthropic key whose account has
    run out of API credit, a rate limit, a transient network error) isn't
    a hard stop for extraction when another configured connection (say,
    a local Ollama) can actually serve the request. Callers try these in
    order and only move to the next on a real failure — see
    _run_ai_extraction()/run_manual_ai_extraction() below. Empty list
    (not None) when none are configured, so callers can genuinely skip
    the stage rather than fabricate field values."""
    stmt = select(Connection).where(
        Connection.category == ConnectionCategory.AI_PROVIDER,
        Connection.status == ConnectionStatus.ACTIVE,
        Connection.is_active.is_(True),
    )
    stmt = stmt.where(Connection.organization_id == organization_id) if organization_id is not None \
        else stmt.where(Connection.organization_id.is_(None))
    stmt = stmt.order_by(Connection.id.asc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _conn_label(conn: Connection) -> str:
    """Connection.name is a free-text field users can leave blank — fall
    back to the connection type so error messages/timeline entries never
    read as a bare ': <error>' with no identifying label."""
    return conn.name or f"{conn.connection_type.value} connection #{conn.id}"


# ── AI-vision OCR fallback ──────────────────────────────────────────
# Real fallback for documents Tesseract genuinely can't read (missing
# binary, corrupt render, or — most commonly — a page it processes
# without error but finds no recognizable text on: handwriting, a script
# Tesseract's bundled language pack doesn't cover, heavy noise/skew, a
# photo of a whiteboard, etc.). Sends the ACTUAL rendered page image to a
# vision-capable AI Provider connection and asks it to transcribe what's
# really on it — never a template, never invented content. See
# connectors/base.py's supports_vision()/generate_vision().

_VISION_OCR_SYSTEM = (
    "You are an OCR transcription assistant. Read the image and transcribe ALL visible text "
    "exactly as it appears, preserving line breaks, numbers and punctuation faithfully. "
    "Do not summarize, describe, or add any commentary — output ONLY the transcribed text. "
    "If the image genuinely has no readable text, output nothing."
)


async def _pick_vision_connection(db: AsyncSession, organization_id: int | None) -> Connection | None:
    """Like _pick_ai_connections above, but filtered to a connection whose
    CONFIGURED MODEL genuinely supports vision input — checked for real
    via each connector's own supports_vision() (see connectors/ai/*), never
    assumed from the connection type alone. Tries every active AI Provider
    connection in id order and returns the first one that qualifies, so an
    org with e.g. a text-only Ollama connection AND a vision-capable
    Anthropic connection still gets a real fallback."""
    stmt = select(Connection).where(
        Connection.category == ConnectionCategory.AI_PROVIDER,
        Connection.status == ConnectionStatus.ACTIVE,
        Connection.is_active.is_(True),
    )
    stmt = stmt.where(Connection.organization_id == organization_id) if organization_id is not None \
        else stmt.where(Connection.organization_id.is_(None))
    stmt = stmt.order_by(Connection.id.asc())
    result = await db.execute(stmt)
    for conn in result.scalars().all():
        try:
            connector = build_connector(conn)
        except ConnectorError:
            continue
        if connector.supports_vision():
            return conn
    return None


# Maps the org Settings "OCR Provider" dropdown's real values to the real
# ConnectionType each one runs through — see connectors/ocr/*. "tesseract"
# (the default) and any legacy/unrecognized value (e.g. an old org row
# still holding the stale placeholder "dip-ocr-v2" from before this was
# wired up) both mean "use the built-in local engine", not an error.
OCR_PROVIDER_CONNECTION_TYPES = {
    "aws-textract": "AWS_TEXTRACT",
    "google-vision": "GOOGLE_VISION",
    "azure-form-recognizer": "AZURE_FORM_RECOGNIZER",
}


async def _pick_ocr_connection(db: AsyncSession, organization_id: int | None, connection_type: str) -> Connection | None:
    """First active OCR_PROVIDER connection of the exact type the org's
    ocr_provider setting names. Unlike _pick_vision_connection above there's
    no capability probe needed — an OCR_PROVIDER connection of type
    AWS_TEXTRACT genuinely does Textract OCR, full stop."""
    stmt = select(Connection).where(
        Connection.category == ConnectionCategory.OCR_PROVIDER,
        Connection.connection_type == connection_type,
        Connection.status == ConnectionStatus.ACTIVE,
        Connection.is_active.is_(True),
    )
    stmt = stmt.where(Connection.organization_id == organization_id) if organization_id is not None \
        else stmt.where(Connection.organization_id.is_(None))
    stmt = stmt.order_by(Connection.id.asc()).limit(1)
    result = await db.execute(stmt)
    return result.scalars().first()


async def _run_cloud_ocr(doc: Document, file_bytes: bytes, conn: Connection) -> dict:
    """Real cloud OCR — renders each page the exact same way the Tesseract
    path does (render_pages_as_png(), also used by the AI-vision fallback
    below) and sends each page image to the connector's real ocr() method
    (a genuine Textract/Vision/Document Intelligence API call — see
    connectors/ocr/*). Raises OcrEngineError on a real failure, same as
    the Tesseract path — a cloud provider outage or bad credentials fails
    the document for real rather than silently falling back to Tesseract,
    which would defeat the point of the org having chosen this provider."""
    connector = build_connector(conn)
    pages = render_pages_as_png(file_bytes, doc.doc_type)  # may raise OcrNotApplicableError/OcrEngineError — let it propagate
    if not pages:
        raise OcrEngineError(f"{conn.name}: no pages could be rendered for OCR.")

    all_regions, all_texts, all_confs, page_dims = [], [], [], []
    for page_num, png_bytes, width, height in pages:
        try:
            result = await asyncio.to_thread(connector.ocr, png_bytes, mime_type="image/png")
        except ConnectorError as exc:
            raise OcrEngineError(f"{conn.name} OCR failed: {exc}") from exc
        for region in result.get("regions", []):
            all_regions.append({**region, "page": page_num})
            if region.get("confidence") is not None:
                all_confs.append(region["confidence"])
        if result.get("full_text"):
            all_texts.append(result["full_text"])
        page_dims.append({"page": page_num, "width": width, "height": height})

    overall_confidence = round(sum(all_confs) / len(all_confs), 1) if all_confs else None
    return {
        "full_text": "\n\n".join(all_texts),
        "confidence": overall_confidence,
        "regions": all_regions,
        "pages": page_dims,
        "page_count": len(pages),
        "source": conn.connection_type.value.lower(),
        "ocr_connection_name": conn.name,
    }


async def _run_vision_ocr_fallback(db: AsyncSession, doc: Document, file_bytes: bytes) -> dict | None:
    """Real AI-vision OCR — the actual page image(s) are sent to a real
    vision model and its real transcription becomes the OCR result.
    Returns None (not a fabricated result) when no vision-capable AI
    Provider connection is configured, so the caller can fall through to
    a genuine failure instead. Raises OcrEngineError (same type the
    Tesseract path raises) if a connection IS configured but the real
    call fails, so the document still ends up honestly FAILED rather than
    silently succeeding with nothing.

    confidence is deliberately None — a vision LLM has no per-word
    confidence score the way Tesseract does, and reporting one would be
    fabricated. regions holds one real, full-page pseudo-region per page
    (not per-line boxes — a vision model doesn't return coordinates) so
    the OCR review UI's existing region-based text panel still displays
    the real transcribed text; its bbox spans the whole page, honestly
    representing "no finer-grained boxes available", not real detected
    line positions."""
    conn = await _pick_vision_connection(db, doc.organization_id)
    if conn is None:
        return None
    connector = build_connector(conn)

    pages = render_pages_as_png(file_bytes, doc.doc_type)  # may raise OcrNotApplicableError/OcrEngineError — let it propagate
    if not pages:
        return None

    all_regions, all_texts, page_dims = [], [], []
    for page_num, png_bytes, width, height in pages:
        try:
            text = await asyncio.to_thread(
                connector.generate_vision, "Transcribe all text in this image.",
                image_b64=base64.b64encode(png_bytes).decode(), mime_type="image/png",
                system=_VISION_OCR_SYSTEM, max_tokens=4000,
            )
        except ConnectorError as exc:
            raise OcrEngineError(f"AI-vision OCR fallback ({conn.name}) failed: {exc}") from exc
        text = (text or "").strip()
        if text:
            all_regions.append({"page": page_num, "text": text, "bbox": [0, 0, width, height], "confidence": None})
            all_texts.append(text)
        page_dims.append({"page": page_num, "width": width, "height": height})

    return {
        "full_text": "\n\n".join(all_texts),
        "confidence": None,
        "regions": all_regions,
        "pages": page_dims,
        "page_count": len(pages),
        "source": "ai_vision",
        "vision_connection_name": conn.name,
    }


def _schema_covered_names(schema: list[dict]) -> list[str]:
    """Every name a gap-fill pass should treat as already covered — each
    field's own name, PLUS a table field's sub-column names (otherwise the
    gap-fill model doesn't know e.g. "quantity" is already covered as a
    line_items column, and proposes it again as a redundant top-level
    field — see build_dataset()'s de-dup comment for what that caused
    live before this helper existed)."""
    names = []
    for f in schema:
        names.append(f["name"])
        if f.get("type") == "table":
            names.extend(c["name"] for c in f.get("columns", []))
    return names


def _reshape_extraction_fields(raw_fields: list[dict], doc: Document | None = None) -> list[dict]:
    """Reshapes extraction_service.extract_fields()'s raw per-field output
    into the persisted field shape (adds validation_status/missing/bbox/
    suggested_correction). Shared by the automatic pipeline
    (_run_ai_extraction below) and the manual "Run AI Extraction" action
    (run_manual_ai_extraction below) so both produce identical field shapes
    regardless of which path triggered extraction.

    bbox (INV4 — evidence linking, see services/evidence_service.py):
    a field from the Custom Extraction Engine already carries a real,
    matched-OCR-row bbox in f["bbox"] (custom_engine.py's label-match —
    wired straight through here, unchanged). A field from real AI
    extraction has no bbox of its own (no vision-grounded call was
    made), so when `doc` is passed and has OCR regions, this heuristically
    searches them for a region containing the field's own value and uses
    THAT region's real bbox — never a guessed/invented box, and left
    None (never fabricated) when nothing genuinely matches."""
    ocr_regions = (doc.ocr_result or {}).get("regions") if doc and doc.ocr_result else None
    fields = []
    for f in raw_fields:
        conf = float(f.get("confidence") or 0.0)
        if f.get("type") == "table":
            # Repeating-row data (e.g. invoice line items) — kept as real
            # rows instead of flattened into one scalar, which is the exact
            # gap that made tabular documents extract as a single blank/
            # wrong row before this. build_dataset() below expands these
            # into genuine multi-row dataset output.
            rows = f.get("rows") or []
            missing = len(rows) == 0
            validation_status = "invalid" if missing else ("valid" if conf >= 90 else ("warning" if conf >= 80 else "invalid"))
            fields.append({
                "name": f["name"],
                "type": "table",
                "columns": f.get("columns", []),
                "rows": rows,
                "value": f"{len(rows)} row{'s' if len(rows) != 1 else ''}" if rows else "",  # human-readable summary — anything that only ever read f["value"] as text (e.g. Review Queue's plain listing) still gets something sane instead of a missing key
                "confidence": round(conf, 1),
                "source_page": 1,
                # Table evidence is inherently row-level (one bbox per line
                # item, not one for the whole field) — genuinely out of
                # scope for this single-bbox slot; left None rather than
                # attaching just the header row's box, which would mislead
                # a viewer into highlighting the wrong region for a cell.
                "bbox": None,
                "validation_status": validation_status,
                "suggested_correction": None,
                "missing": missing,
            })
            continue
        missing = f.get("value") in (None, "")
        validation_status = "invalid" if missing else ("valid" if conf >= 90 else ("warning" if conf >= 80 else "invalid"))
        bbox = f.get("bbox")  # real, already-matched bbox — Custom Extraction Engine only
        if bbox is None and not missing and ocr_regions:
            bbox = evidence_service.find_region_bbox(f.get("value"), ocr_regions)
        fields.append({
            "name": f["name"],
            "type": "scalar",
            "value": "" if missing else f.get("value"),
            "confidence": round(conf, 1),
            "source_page": f.get("page") or 1,
            "bbox": bbox,
            "validation_status": validation_status,
            "suggested_correction": None,  # no real "corrected" value to offer — not fabricated
            "missing": missing,
        })
    return fields


async def _run_custom_engine_extraction(db: AsyncSession, doc: Document, *, document_type: str | None = None) -> tuple[list[dict], float | None, str | None, dict]:
    """Real, zero-configuration fallback extraction via the Custom
    Extraction Engine (app/extraction/custom_engine.py) — a deterministic
    rule-based extractor, NOT a language model. Called whenever no AI
    Provider connection is configured for the organization, from both the
    automatic pipeline (_run_ai_extraction above) and the manual "Run AI
    Extraction" action (run_manual_ai_extraction below), via
    extraction_service's connection_id == CUSTOM_ENGINE_CONNECTION_ID
    sentinel so neither of those callers needs any separate code path."""
    try:
        schema = await extraction_service.detect_schema(
            db, doc, extraction_service.CUSTOM_ENGINE_CONNECTION_ID, doc.organization_id, document_type=document_type,
        )
        result = await extraction_service.extract_fields(
            db, doc, schema, extraction_service.CUSTOM_ENGINE_CONNECTION_ID, doc.organization_id,
        )
    except extraction_service.ExtractionError as exc:
        return [], None, f"Custom Extraction Engine: {exc}", {}
    fields = _reshape_extraction_fields(result["fields"], doc)
    return fields, result.get("overallConfidence"), None, {
        "pages_considered": result.get("pages_considered") or [],
        "total_ocr_pages": result.get("total_ocr_pages"),
    }


async def _run_ai_extraction(
    db: AsyncSession, doc: Document, *, document_type: str | None = None,
) -> tuple[list[dict], float | None, str | None, dict | None, str | None, dict]:
    """Real AI Extraction for the Documents module's automatic pipeline —
    the same LLM engine as the Document Intelligence wizard
    (extraction_service.py): auto-detects a schema from the document's
    real OCR text, then extracts real field values, via whichever AI
    Provider connection is configured for this organization. Returns
    (fields, confidence, skip_reason, raw_ai_response, model_name) —
    skip_reason is set
    (fields=[]) when there's no OCR text yet or no AI Provider connection
    configured, so the stage is genuinely skipped (like OCR skips non-
    applicable file types above) instead of falling back to fabricated
    values. raw_ai_response is the AI model's own verbatim JSON response
    (persisted onto Document.ai_raw_response by the caller — see INV3) —
    None whenever no real AI call happened (skipped, or Custom Extraction
    Engine fallback, which isn't AI-based at all). model_name (INV22) is
    the real connection's model identifier (e.g. "gpt-4o-mini") when a
    genuine AI call ran, "CUSTOM_ENGINE" for the rule-based fallback (a
    real, honest label — something deterministic genuinely did the
    extraction), or None when nothing ran — persisted onto Document.
    extraction_model by the caller so Lineage's AI_MODEL node can show a
    real value.

    document_type (e.g. "INVOICE" — see document_taxonomies.py), when the
    caller already knows it (currently only the scheduler's invoice-typed
    document schedules — see schedule_service.py), skips ad-hoc schema
    detection in favor of the canonical taxonomy directly, same as the
    manual "Run AI Extraction" picker (run_manual_ai_extraction) already
    does. document_type=None (the default) is the original ad-hoc
    auto-detect behavior, unchanged for every other caller.

    If the ad-hoc schema-detection call itself fails, OR it succeeds but
    genuinely finds nothing (every proposed field comes back empty — the
    model's own 5-12-field guess just didn't match this document), this
    automatically retries with document_taxonomies.py's comprehensive
    GENERIC schema instead of stopping there — no user action needed. This
    is what actually fixes "schema not found automatically → nothing
    extracted"; the manual per-document "Run AI Extraction" picker
    (run_manual_ai_extraction) is for when a user wants to deliberately
    choose a specific type (e.g. INVOICE) rather than the generic net.

    If a connection genuinely fails at runtime (ad-hoc AND the GENERIC
    fallback both error — e.g. an Anthropic connection whose account is
    out of API credit, a rate limit, a network error), this moves on to
    the NEXT configured AI Provider connection (see _pick_ai_connections)
    rather than giving up: one broken connection no longer blocks
    extraction when another working one is also configured for the org.

    Runs inside db.no_autoflush: the caller (process_document) sets
    doc.status/doc.ocr_result etc. earlier in the SAME uncommitted
    transaction, and the several plain SELECTs below (_pick_ai_connections,
    discover_known_field_names, get_connection inside extraction_service)
    would otherwise each trigger SQLAlchemy's autoflush of those pending
    changes first — a real, observed cause of a spurious "database is
    locked" failure (a concurrent writer briefly held SQLite's single-
    writer lock right as one of these autoflushes fired). Suppressing
    autoflush here just defers that write to process_document's own
    single explicit commit at the end, where it belongs — no data is
    lost, only the accidental extra flush points are removed.

    No AI Provider connection configured -> falls back to the Custom
    Extraction Engine (see _run_custom_engine_extraction below) instead
    of skipping the stage outright."""
    if not doc.ocr_result or not (doc.ocr_result.get("full_text") or "").strip():
        return [], None, "No OCR text available for AI Extraction.", None, None, {}

    with db.no_autoflush:
        candidates = await _pick_ai_connections(db, doc.organization_id)
        if not candidates:
            fields, conf, skip_reason, extraction_meta = await _run_custom_engine_extraction(db, doc, document_type=document_type)
            return fields, conf, skip_reason, None, "CUSTOM_ENGINE", extraction_meta

        last_error: str | None = None
        for conn in candidates:
            ad_hoc_error: str | None = None
            result: dict | None = None
            try:
                schema = await extraction_service.detect_schema(
                    db, doc, conn.id, doc.organization_id, document_type=document_type,
                )
                result = await extraction_service.extract_fields(db, doc, schema, conn.id, doc.organization_id)
            except extraction_service.ExtractionError as exc:
                ad_hoc_error = str(exc)

            found_something = result is not None and any(
                (f.get("type") == "table" and f.get("rows")) or (f.get("type") != "table" and f.get("value") not in (None, ""))
                for f in result.get("fields", [])
            )

            if not found_something:
                try:
                    extra_names = await discover_known_field_names(db, doc.organization_id, document_type="GENERIC")
                    fallback_schema = document_taxonomies.build_seed_schema("GENERIC", extra_names)
                    fallback_result = await extraction_service.extract_fields(db, doc, fallback_schema, conn.id, doc.organization_id)
                    # Gap-fill (find_gap_fields) is deliberately NOT run here —
                    # a real, observed failure live: asked to propose a
                    # plausible-sounding field beyond the (already
                    # comprehensive) GENERIC taxonomy, the model proposed one
                    # (e.g. "supplier_email") that doesn't actually appear in
                    # the document at all, and the follow-up extract_fields()
                    # call for it then fabricated a value rather than
                    # returning null — inventing contact info for a real
                    # business is exactly the kind of fake data this module
                    # exists to avoid. The canonical taxonomy's own coverage
                    # is the ceiling now, not this second, less-grounded pass.
                    result = fallback_result
                except extraction_service.ExtractionError as exc:
                    if result is None:
                        # Both the ad-hoc call AND the fallback genuinely failed
                        # on THIS connection — try the next configured
                        # connection (if any) instead of stopping here.
                        last_error = f"{_conn_label(conn)}: {ad_hoc_error or str(exc)}"
                        continue
                    # Ad-hoc technically succeeded (no exception) but found nothing,
                    # and the fallback then hit a real error — keep the ad-hoc
                    # (empty) result rather than losing everything to the fallback's
                    # failure; the document still ends up correctly flagged
                    # NEEDS_REVIEW upstream since extraction_percent will be low/None.

            fields = _reshape_extraction_fields(result["fields"], doc)
            extraction_meta = {
                "pages_considered": result.get("pages_considered") or [],
                "total_ocr_pages": result.get("total_ocr_pages"),
            }
            return fields, result.get("overallConfidence"), None, result.get("raw_model_response"), result.get("model"), extraction_meta

        # Every configured AI Provider connection genuinely failed.
        return [], None, last_error or "AI Extraction failed on every configured AI Provider connection.", None, None, {}


MAX_MINED_FIELD_NAMES = 20  # hard cap — see discover_known_field_names() docstring


async def discover_known_field_names(
    db: AsyncSession, organization_id: int | None, document_type: str | None = None, limit_documents: int = 200,
) -> set[str]:
    """Real, data-driven schema enrichment: reads this organization's own
    already-extracted documents and unions scalar field names they
    produced. Merged into the canonical taxonomy in
    extraction_service.detect_schema()'s document_type path — so a new
    invoice's seed schema genuinely reflects fields this organization's
    own past invoices have actually had (e.g. a vendor-specific reference
    code no generic template would know about), not just a static list.
    Table field names are excluded (the canonical taxonomy already defines
    line_items) to avoid seeding a second, redundant table field.

    Two guards keep this from drowning the canonical taxonomy in noise —
    a real, observed failure mode: an org with a handful of one-off ad-hoc
    (document_type=None) extractions from unrelated documents (a food-
    delivery receipt, a marketplace invoice, ...) was polluting every
    later INVOICE extraction with ~100 near-duplicate/irrelevant field
    names (billing_address next to bill_to_address_line1, gstin next to
    vendor_tax_id, ...), which measurably hurt the model's accuracy on the
    real canonical fields:

    1. document_type scoping — when the caller names a document_type
       (e.g. "INVOICE"), only mine field names from PAST documents that
       were themselves extracted against that SAME document_type (see
       doc.extraction_result["document_type"], set by
       run_manual_ai_extraction()). Ad-hoc (document_type=None)
       extractions never contribute here — they're exactly the
       unrelated-document noise this guard exists to keep out. This
       naturally starts empty for an org's first INVOICE extraction and
       only grows as genuinely comparable invoices accumulate.
    2. Frequency cap — even within-type, only the MAX_MINED_FIELD_NAMES
       names seen across the most past documents are kept, ranked by
       how many distinct documents actually produced them. A field seen
       on one stray document is far less likely to be a genuine
       recurring concept than one several past invoices share."""
    stmt = select(Document.extraction_result).where(Document.extraction_result.is_not(None))
    if organization_id is not None:
        stmt = stmt.where(Document.organization_id == organization_id)
    stmt = stmt.order_by(Document.id.desc()).limit(limit_documents)
    result = await db.execute(stmt)
    counts: dict[str, int] = {}
    for (extraction_result,) in result.all():
        extraction_result = extraction_result or {}
        if document_type is not None and extraction_result.get("document_type") != document_type:
            continue
        for f in extraction_result.get("fields", []):
            if isinstance(f, dict) and f.get("name") and f.get("type") != "table":
                name = str(f["name"])
                counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts, key=lambda n: (-counts[n], n))
    return set(ranked[:MAX_MINED_FIELD_NAMES])


async def run_manual_ai_extraction(
    db: AsyncSession, doc: Document, *, document_type: str | None = None, connection_id: int | None = None,
) -> Document:
    """Backs the explicit "Run AI Extraction" action on Document Details /
    Review Queue — see api/documents.py's /run-extraction route. Unlike
    the automatic pipeline, this is user-triggered on demand (addresses
    "no AI extraction option while reviewing"), can be re-run as many
    times as needed, and accepts an optional document_type (e.g. "INVOICE")
    to steer extraction at a comprehensive canonical schema instead of an
    ad-hoc LLM guess (see document_taxonomies.py). When document_type is
    given, a gap-fill pass also asks the model to name anything genuinely
    present but not in the canonical list, and extracts those too — real
    per-document adaptability on top of a consistent baseline.

    connection_id lets the user explicitly pick which AI Provider
    connection to use (addresses "no option to change AI type in
    document screen") — e.g. to route around a connection that's
    genuinely broken (out of API credit, rate-limited) without touching
    Connections settings. That explicit choice is respected as-is: if it
    fails, the error is raised directly rather than silently falling back
    to a different connection the user didn't ask for. When connection_id
    is omitted (the default, "Auto"), every active AI Provider connection
    is tried in order until one genuinely succeeds — see
    _pick_ai_connections() — so a single broken connection no longer
    blocks extraction outright when a working one is also configured.

    connection_id == extraction_service.CUSTOM_ENGINE_CONNECTION_ID (the
    "Custom (No AI Required)" option in the picker) routes straight to
    the Custom Extraction Engine, explicitly requested. "Auto" with no
    AI Provider connection configured now falls back to the same engine
    too, instead of raising — matching the automatic pipeline's
    behavior (_run_ai_extraction above)."""
    if not doc.ocr_result or not (doc.ocr_result.get("full_text") or "").strip():
        raise ManualExtractionError("This document has no OCR text yet — run OCR first (Process/Reprocess).")

    use_custom_engine = connection_id == extraction_service.CUSTOM_ENGINE_CONNECTION_ID
    candidates: list[Connection] = []
    if not use_custom_engine:
        if connection_id is not None:
            conn = await get_connection(db, connection_id, doc.organization_id)
            if conn is None:
                raise ManualExtractionError("Selected AI connection not found.")
            if conn.category != ConnectionCategory.AI_PROVIDER:
                raise ManualExtractionError(f"'{_conn_label(conn)}' is not an AI Provider connection.")
            candidates = [conn]
        else:
            candidates = await _pick_ai_connections(db, doc.organization_id)
            if not candidates:
                # No AI Provider connection configured for "Auto" -> fall
                # back to the Custom Extraction Engine instead of failing
                # outright, same as the automatic pipeline does.
                use_custom_engine = True

    errors: list[str] = []
    result: dict | None = None
    used_conn: Connection | None = None
    if use_custom_engine:
        extra_names = None
        if document_type:
            extra_names = await discover_known_field_names(db, doc.organization_id, document_type=document_type)
        try:
            schema = await extraction_service.detect_schema(
                db, doc, extraction_service.CUSTOM_ENGINE_CONNECTION_ID, doc.organization_id,
                document_type=document_type, extra_field_names=extra_names,
            )
            result = await extraction_service.extract_fields(
                db, doc, schema, extraction_service.CUSTOM_ENGINE_CONNECTION_ID, doc.organization_id,
            )
        except extraction_service.ExtractionError as exc:
            errors.append(f"Custom Extraction Engine: {exc}")
    else:
        for conn in candidates:
            try:
                extra_names = None
                if document_type:
                    extra_names = await discover_known_field_names(db, doc.organization_id, document_type=document_type)
                schema = await extraction_service.detect_schema(
                    db, doc, conn.id, doc.organization_id, document_type=document_type, extra_field_names=extra_names,
                )
                result = await extraction_service.extract_fields(db, doc, schema, conn.id, doc.organization_id)
                # Gap-fill (find_gap_fields) is deliberately NOT run for a known
                # canonical document_type — see the matching comment in
                # _run_ai_extraction() above for the real, observed fabrication
                # this caused (a plausible-sounding field name proposed beyond
                # the taxonomy, then given a made-up value instead of null). The
                # canonical taxonomy (document_taxonomies.py) is comprehensive
                # by design; this second, less-grounded pass isn't worth that risk.
                used_conn = conn
                break
            except extraction_service.ExtractionError as exc:
                errors.append(f"{_conn_label(conn)}: {exc}")
                result = None
                continue

    if result is None:
        if len(candidates) > 1:
            raise ManualExtractionError("AI Extraction failed on every configured AI Provider connection — " + "; ".join(errors))
        raise ManualExtractionError(errors[0] if errors else "AI Extraction failed.")

    fields = _reshape_extraction_fields(result["fields"], doc)
    if not fields:
        raise ManualExtractionError("The Custom Extraction Engine didn't find any usable fields in this document." if use_custom_engine else "The AI model didn't find any usable fields in this document.")

    doc.extraction_result = {"fields": fields, "business_rule_flag": False, "document_type": document_type}
    doc.ai_raw_response = result.get("raw_model_response")
    # INV22 — same convention as the automatic pipeline's _run_ai_extraction():
    # "CUSTOM_ENGINE" is a real, honest label (deterministic rule-based
    # extraction genuinely ran), not a fabricated model name.
    doc.extraction_model = "CUSTOM_ENGINE" if use_custom_engine else result.get("model")
    doc.dataset = build_dataset(fields)
    doc.extraction_percent = result.get("overallConfidence")
    overall_conf = result.get("overallConfidence") or 0.0
    doc.status = DocumentStatus.COMPLETED if overall_conf >= 90 else DocumentStatus.NEEDS_REVIEW
    doc.error_message = None
    now = datetime.now(timezone.utc)
    conn_suffix = f" via {'Custom Extraction Engine' if use_custom_engine else _conn_label(used_conn)}" if (use_custom_engine or used_conn) else ""
    label = f"AI Extraction (Manual — {document_type}{conn_suffix})" if document_type else f"AI Extraction (Manual{conn_suffix})"
    doc.timeline = list(doc.timeline or []) + [_stage_record(label, "completed", now, now, confidence=overall_conf)]
    await db.commit()
    await db.refresh(doc)

    from app.services import review_queue_service as review_svc
    if doc.status == DocumentStatus.NEEDS_REVIEW:
        await review_svc.create_from_document(db, doc)
    else:
        await review_svc.auto_resolve_for_document(db, doc.id)
    await db.refresh(doc)

    # Invoice Intelligence materialization (INV5) — same explicit-pin
    # gate as process_document() above; never fails the manual extraction
    # action itself.
    if document_type == "INVOICE" and doc.extraction_result and doc.extraction_result.get("fields"):
        try:
            from app.services.invoice import invoice_processing_service, invoice_validation_service, invoice_decision_service
            invoice = await invoice_processing_service.materialize_invoice(db, doc)
            await invoice_validation_service.seed_default_rules(db, doc.organization_id)
            validation_results = await invoice_validation_service.evaluate_invoice(db, invoice)
            await invoice_decision_service.decide(db, invoice, validation_results)
        except Exception:  # noqa: BLE001 - see process_document()'s matching comment
            pass

    return doc


def build_dataset(fields: list[dict]) -> dict:
    """Builds the schema/table/json dataset views from a fields list. Shared
    by process_document() and Review Queue's approve-with-corrections flow
    (review_queue_service) so dataset shape stays defined in exactly one place.

    When one of the fields is a genuine repeating table (type == "table",
    e.g. invoice line items — see _run_ai_extraction()/extraction_service.py),
    its rows become the dataset's real rows, with every scalar field's value
    repeated across each of them — the standard denormalized-export shape,
    and the fix for documents whose real content is tabular (multiple
    line-item rows) previously always producing exactly one flattened row
    no matter how many rows the source document actually had. Documents
    with no table field are completely unaffected — identical single-row
    output to before this existed."""
    # De-duplicate field names (case-insensitive) before anything else —
    # two fields sharing a name is possible when a comprehensive canonical
    # schema (document_taxonomies.py) is combined with a gap-fill pass
    # (extraction_service.find_gap_fields()) that doesn't know about a
    # table's own sub-column names; observed live producing React
    # "duplicate key" warnings and, worse, a genuinely ambiguous dataset
    # column. Keeps the FIRST occurrence — the canonical/table definition
    # wins over a redundant later guess.
    deduped: list[dict] = []
    seen_names: set[str] = set()
    for f in fields:
        key = f["name"].strip().lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        deduped.append(f)
    fields = deduped

    table_field = next((f for f in fields if f.get("type") == "table" and f.get("rows")), None)
    if table_field is None:
        return {
            "schema": [{"field": f["name"], "type": "string"} for f in fields],
            "table": {
                "columns": [f["name"] for f in fields],
                "rows": [[f.get("value") for f in fields]],
            },
            "json": {f["name"].lower().replace(" ", "_"): f.get("value") for f in fields},
        }

    # Any OTHER field (scalar, or a second table this pass doesn't expand —
    # rare, and documented rather than silently dropped) becomes a header
    # column whose value repeats on every row. Also drop any header field
    # whose name collides with one of the table's OWN column names (case-
    # insensitive) — the table's column is the real, per-row value; a
    # same-named header field would just be a redundant, less specific
    # duplicate of it.
    table_columns = table_field.get("columns", [])
    table_columns_lower = {c.lower() for c in table_columns}
    header_fields = [f for f in fields if f is not table_field and f["name"].lower() not in table_columns_lower]
    header_names = [f["name"] for f in header_fields]
    all_columns = header_names + table_columns

    rows = [
        [f.get("value") for f in header_fields] + [row.get(c, "") for c in table_columns]
        for row in table_field["rows"]
    ]

    return {
        "schema": [{"field": c, "type": "string"} for c in all_columns],
        "table": {"columns": all_columns, "rows": rows},
        "json": {
            **{f["name"].lower().replace(" ", "_"): f.get("value") for f in header_fields},
            table_field["name"].lower().replace(" ", "_"): table_field["rows"],
        },
    }


def _stage(stage: str, status: str, started: datetime, seconds: float, confidence: float | None = None, error: str | None = None) -> tuple[dict, datetime]:
    ended = started + timedelta(seconds=seconds)
    return _stage_record(stage, status, started, ended, confidence, error), ended


def friendly_db_error(exc: Exception) -> str:
    """A raw sqlite3.OperationalError's str() includes the full failed SQL
    statement and every bound parameter value (see this function's
    callers) — genuinely useful in a server log, but alarming and useless
    shown verbatim as a document's error_message in the UI, or as an API
    error response. Collapse the one transient case this codebase is
    actually prone to (concurrent writers briefly colliding on SQLite's
    single-writer lock — see database/connection.py's busy_timeout
    comment) to one clear, actionable sentence; anything else still
    passes through unmodified so a genuinely new failure mode isn't
    hidden from whoever reads it. Public (not just this module's own
    process_document()) so route handlers can give the same honest,
    non-alarming message for the identical lock collision hitting a
    plain save_upload() — see api/invoices.py and api/documents.py."""
    text = str(exc)
    if "database is locked" in text.lower():
        return "The document database was temporarily busy processing other documents — please try again in a moment."
    return text


async def process_document(
    db: AsyncSession, doc: Document, *, reprocess: bool = False,
    confidence_threshold: float = 90.0, accuracy_threshold: float = 90.0,
    document_type: str | None = None,
    invoice_auto_post: bool = False, invoice_erp_connection_id: int | None = None,
    skip_invoice_pipeline: bool = False,
    preextracted_fields: list[dict] | None = None,
    language_config: dict | None = None,
    storage_override: dict | None = None,
) -> Document:
    """Runs the Document Intelligence pipeline synchronously and persists
    timeline/OCR/extraction/dataset results onto the document. OCR (stage 2)
    and AI Extraction (stage 3) are real; Image Scanner/Validation timing
    remain simulated — see module docstring.

    confidence_threshold/accuracy_threshold (spec: Scheduled Document
    Intelligence Workflow) control the PASS/REVIEW_REQUIRED boundary below —
    defaults exactly match manual scan's previous hardcoded 90/90 so every
    existing caller is unaffected; only schedule_service.py's document-
    schedule dispatch passes non-default values, per-schedule.

    document_type (e.g. "INVOICE", see document_taxonomies.py) pins AI
    Extraction to a known canonical taxonomy instead of ad-hoc auto-detect
    — see _run_ai_extraction()'s own docstring. None (the default) is the
    original auto-detect behavior; only an invoice-typed
    DocumentScheduleConfig.document_type passes a real value here (INV11).

    skip_invoice_pipeline (INV24) — when True, keeps document_type="INVOICE"
    pinning the AI Extraction taxonomy for accuracy but skips the automatic
    materialize/validate/decide chain below that would otherwise run
    unconditionally for an INVOICE-typed document. Every existing caller
    (manual upload, Scheduler, Review Queue) leaves this False and is
    completely unaffected; only Pipeline Builder's own "Invoice Processing"
    node (execution/pipeline_job_executor.py) passes True, so that its
    separate "Invoice Validation" node can run that same real chain exactly
    once as its own distinct pipeline stage instead of it happening twice
    or being silently absorbed into this one.

    preextracted_fields (PAY18 — DATABASE-kind document schedules) — when
    given, this document's "content" is already structured data from a
    non-file source (see document_service.build_fields_from_record()),
    not an image/PDF to scan. Image Scanner/OCR are skipped outright
    (there is no image at all — read_file()/run_ocr() are never called)
    and AI Extraction is replaced with these fields directly at 100%
    confidence, labeled doc.extraction_model = "STRUCTURED_SOURCE" (a
    real, honest label for "no model ran, the source system's own
    columns already were the structured data" — same spirit as
    "CUSTOM_ENGINE" for the rule-based OCR-text fallback). Every stage
    from Structured Dataset onward is completely unchanged — the same
    validation/business-rule/invoice-materialization chain runs
    regardless of where the fields came from.

    storage_override (Part 21 — ZIP Batch Upload's per-batch Storage
    config) is passed straight through to document_storage_service.
    push_document()'s own `override` param — None (every caller except
    document_batch_service.py) means the org-wide Processed Data Storage
    config applies exactly as before this feature existed; see that
    function's docstring for the override dict's shape."""
    doc_id = doc.id  # saved before anything can fail — used to re-fetch a clean instance if the except block below needs to recover from a broken session
    rnd = random.Random()
    now = datetime.now(timezone.utc)
    timeline = [] if reprocess else list(doc.timeline or [])
    if not timeline:
        timeline.append(_stage_record("Uploaded", "completed", now, now))

    try:
        if language_config is not None:
            doc.language_config = normalize_ocr_language_config(language_config)
        cursor = now
        # Multilingual Document Processing, Phase 6 — real OCR quality/
        # script-mismatch flags (see multilingual_ocr_service.evaluate_
        # ocr_quality()). Declared here so it's always in scope for the
        # needs_review computation below regardless of which branch below
        # runs; stays [] for the structured-source/no-OCR path and the
        # OcrNotApplicableError path — there's no OCR result to evaluate.
        ocr_quality_flags: list[dict] = []
        extraction_meta: dict = {}
        if preextracted_fields is not None:
            # PAY18 — structured, non-file source: no image, no OCR, no AI
            # model. Image Scanner/OCR are recorded as genuinely skipped
            # (not faked with a random confidence number that implies a
            # scan happened) and AI Extraction is recorded as "completed"
            # because real, structured field values genuinely ARE
            # available at this point — just not produced by a model.
            scan_conf = None
            timeline.append(_stage_record("Image Scanner", "skipped", cursor, cursor, error="Structured source (database record) — no image to scan."))
            ocr_conf = None
            doc.ocr_result = None
            doc.ocr_percent = None
            timeline.append(_stage_record("OCR", "skipped", cursor, cursor, error="Structured source (database record) — OCR does not apply."))

            doc.status = DocumentStatus.AI_EXTRACTION
            ai_start = cursor
            fields = preextracted_fields
            extraction_conf = 100.0
            doc.ai_raw_response = None
            doc.extraction_model = "STRUCTURED_SOURCE"
            rec, cursor = _stage("AI Extraction", "completed", ai_start, 0.05, extraction_conf)
            rec["note"] = "Fields mapped directly from the source system's own columns — no OCR or AI model involved."
            timeline.append(rec)
            doc.extraction_percent = extraction_conf
        else:
            # 1. Image Scanner
            doc.status = DocumentStatus.SCANNING
            scan_conf = round(rnd.uniform(90, 99.5), 1)
            rec, cursor = _stage("Image Scanner", "completed", cursor, rnd.uniform(0.8, 2.5), scan_conf)
            timeline.append(rec)

            # 2. OCR — real, see app/ocr/engine.py. Not simulated: opens the
            # actual uploaded file and runs Tesseract on it. The rest of this
            # timeline runs on a *synthetic* clock (each stage's "started_at" is
            # the previous stage's simulated end, not real wall-clock time — see
            # _stage() above), so OCR's real wall-clock duration is measured
            # separately (time.monotonic()) and then applied on top of that same
            # synthetic cursor — keeps the timeline internally consistent
            # (monotonically increasing) while the recorded OCR duration is
            # genuinely real, not a random placeholder.
            doc.status = DocumentStatus.OCR_PROCESSING
            ocr_start = cursor
            # Commit the status flip now, before the genuinely slow real work
            # below (Tesseract, possibly an AI-vision fallback call). Without
            # this, autoflush on the very next db.execute() (e.g.
            # settings_service.get_or_create() just below) stages this UPDATE
            # inside the still-open transaction from this session's LAST
            # commit — and since process_document() previously committed only
            # once at the very end, that transaction (and the SQLite writer
            # lock it holds) stayed open for the full OCR+AI Extraction
            # duration, causing concurrent uploads to hit real "database is
            # locked" errors (see friendly_db_error's docstring and the
            # except block below — this shrinks the window that causes it,
            # it doesn't remove SQLite's single-writer limit entirely).
            await db.commit()
            real_ocr_start = time.monotonic()
            try:
                file_bytes = read_file(doc)
                # Real dispatch on the org's Settings > Document Intelligence >
                # OCR Provider value — "tesseract" (the default) and any legacy
                # value need no Connection and fall straight through to the
                # existing Tesseract-first path below. A cloud provider name
                # (aws-textract/google-vision/azure-form-recognizer) genuinely
                # runs through that vendor's real API instead — see
                # _run_cloud_ocr()/OCR_PROVIDER_CONNECTION_TYPES above. If the
                # org picked a cloud provider but hasn't configured a matching
                # OCR_PROVIDER connection yet, this fails with a real, specific
                # error rather than silently using Tesseract anyway — using a
                # provider the org didn't choose would be exactly the kind of
                # fake functionality this codebase's OCR engine was built to
                # avoid (see ocr/engine.py's own docstring).
                org_settings = await settings_service.get_or_create(db, doc.organization_id) if doc.organization_id else None
                # Multilingual Document Processing, Phase 4 — real deep-merged
                # document_intelligence settings (supported_languages/
                # primary_language get their DEFAULT_SETTINGS values even for
                # an org row that predates this feature; see
                # settings_service.effective_document_intelligence_settings()).
                di_settings = settings_service.effective_document_intelligence_settings(org_settings)
                supported_languages, primary_language, effective_language_config = resolve_document_language_config(doc, di_settings)
                provider_setting = di_settings.get("ocr_provider")
                cloud_connection_type = OCR_PROVIDER_CONNECTION_TYPES.get(provider_setting or "")
                if cloud_connection_type:
                    ocr_conn = await _pick_ocr_connection(db, doc.organization_id, cloud_connection_type)
                    if ocr_conn is None:
                        raise OcrEngineError(
                            f"Settings > Document Intelligence has OCR Provider set to '{provider_setting}', but no "
                            f"active {cloud_connection_type.replace('_', ' ').title()} connection is configured — add "
                            "one on the Connections page, or switch the OCR Provider setting back to Tesseract."
                        )
                    async with _ocr_semaphore:
                        ocr_result = await _run_cloud_ocr(doc, file_bytes, ocr_conn)
                    # Cloud providers (Textract/Vision/Form Recognizer) already do
                    # their own multilingual recognition — we don't re-OCR, only
                    # report what language the result came back in (see
                    # multilingual_ocr_service.detect_language_only's docstring).
                    ocr_result["language"] = detect_language_only(ocr_result)
                else:
                    try:
                        # Real multilingual OCR — see ocr/multilingual_ocr_service.py's
                        # own docstring for the two-pass "lightweight combined pass,
                        # detect per page, re-OCR the pages that need a different
                        # language model" flow. Reduces to a single plain-English
                        # Tesseract pass (byte-identical to the old run_ocr() call
                        # this replaces) for every org that hasn't opted into another
                        # language yet — the shipped default.
                        async with _ocr_semaphore:
                            ocr_result = await asyncio.to_thread(
                                run_multilingual_ocr, file_bytes, doc.doc_type,
                                supported_languages=supported_languages, primary_language=primary_language,
                            )
                        if not (ocr_result["full_text"] or "").strip():
                            # Tesseract ran without error but genuinely found no
                            # readable text (handwriting, an unsupported script, a
                            # photo of a mostly-blank/graphic page, ...) — try the
                            # real AI-vision fallback before giving up on this page.
                            async with _ocr_semaphore:
                                vision_result = await _run_vision_ocr_fallback(db, doc, file_bytes)
                            if vision_result is not None:
                                vision_result["language"] = detect_language_only(vision_result)
                                ocr_result = vision_result
                    except LanguageProcessingError as exc:
                        if exc.code == LanguageErrorCode.OCR_LANGUAGE_MODEL_MISSING:
                            raise
                        async with _ocr_semaphore:
                            vision_result = await _run_vision_ocr_fallback(db, doc, file_bytes)
                        if vision_result is None:
                            raise
                        vision_result["language"] = detect_language_only(vision_result)
                        ocr_result = vision_result
                    except OcrEngineError:
                        # Tesseract itself failed for real (missing binary, corrupt
                        # render, a configured language's model genuinely not
                        # installed, ...) — this is exactly the "if OCR not work, try
                        # available AI agent" case. Only re-raise if the fallback
                        # itself has nothing to offer (no vision connection
                        # configured) or also genuinely fails — a real
                        # OCR_LANGUAGE_MODEL_MISSING error must still surface (not
                        # be silently swallowed) when there's no fallback to try.
                        async with _ocr_semaphore:
                            vision_result = await _run_vision_ocr_fallback(db, doc, file_bytes)
                        if vision_result is None:
                            raise
                        vision_result["language"] = detect_language_only(vision_result)
                        ocr_result = vision_result
                real_elapsed = time.monotonic() - real_ocr_start
                ocr_conf = ocr_result["confidence"] if ocr_result["confidence"] is not None else 0.0
                # Multilingual Document Processing, Phase 6 — real OCR quality/
                # script-mismatch validation, using the org's OWN configured
                # thresholds (ocr_confidence_threshold/language_mismatch_threshold),
                # not the generic extraction confidence_threshold checked below.
                # A genuinely bad OCR result never gets to look like a quiet
                # success just because the AI/Custom-Engine extraction stage
                # downstream happened to produce plausible-looking field values
                # from garbled text.
                ocr_quality_flags = evaluate_ocr_quality(
                    ocr_result,
                    ocr_confidence_threshold=di_settings.get("ocr_confidence_threshold", 85),
                    language_mismatch_threshold=di_settings.get("language_mismatch_threshold", 80),
                    supported_languages=supported_languages,
                    primary_language=primary_language,
                )
                doc.ocr_result = {
                    "full_text": ocr_result["full_text"],
                    "confidence": ocr_result["confidence"],
                    "regions": ocr_result["regions"],
                    "pages": ocr_result["pages"],
                    "source": ocr_result.get("source", "tesseract"),
                    # Multilingual Document Processing, Phase 4 — real per-page +
                    # document-level detected language(s), never a translation (see
                    # ocr/multilingual_ocr_service.py). Stored in this existing JSON
                    # column, no migration needed.
                    "language": ocr_result.get("language"),
                    "language_config": effective_language_config,
                    # Phase 6 — real named quality issues (see evaluate_ocr_quality()
                    # above); [] when the OCR result is genuinely clean.
                    "quality_flags": ocr_quality_flags,
                }
                doc.ocr_percent = ocr_result["confidence"]
                if ocr_result["page_count"]:
                    doc.page_count = ocr_result["page_count"]
                rec, cursor = _stage("OCR", "completed", ocr_start, real_elapsed, ocr_result["confidence"])
                if ocr_result.get("source") == "ai_vision":
                    rec["note"] = f"Tesseract found no readable text — transcribed via AI vision ({ocr_result.get('vision_connection_name', 'AI connection')})."
                elif ocr_result.get("ocr_connection_name"):
                    rec["note"] = f"OCR'd via {ocr_result['source']} ({ocr_result['ocr_connection_name']}) — org's configured OCR Provider."
                timeline.append(rec)
            except OcrNotApplicableError as exc:
                # Word/Excel/text/other — these carry content natively; OCR
                # genuinely doesn't apply, so this is skipped (not faked) and
                # the pipeline continues.
                real_elapsed = time.monotonic() - real_ocr_start
                ocr_conf = None
                doc.ocr_result = None
                doc.ocr_percent = None
                cursor = ocr_start + timedelta(seconds=real_elapsed)
                timeline.append(_stage_record("OCR", "skipped", ocr_start, cursor, error=str(exc)))
            # OcrEngineError that survives the vision-fallback attempt above
            # (Tesseract failed AND no vision fallback was available/worked)
            # is deliberately NOT caught here — it propagates to the outer
            # try/except below, which marks the whole document FAILED with the
            # real error rather than continuing to layer simulated stages on
            # top of a real OCR failure.

            # 3. AI Extraction — real, see _run_ai_extraction() above (same LLM
            # engine as the Document Intelligence wizard). Real elapsed time is
            # measured the same way OCR's is above: applied on top of the
            # synthetic cursor so the timeline stays monotonically increasing
            # while the recorded duration is genuine, not a random placeholder.
            doc.status = DocumentStatus.AI_EXTRACTION
            ai_start = cursor
            # Same reasoning as the OCR commit above — persist the OCR
            # result + status flip now, before the slow real LLM call(s) in
            # _run_ai_extraction(), instead of holding the writer lock open
            # underneath a network round trip that can legitimately take
            # tens of seconds.
            await db.commit()
            real_ai_start = time.monotonic()
            async with _ai_semaphore:
                fields, extraction_conf, ai_skip_reason, ai_raw_response, ai_model_name, extraction_meta = await _run_ai_extraction(
                    db, doc, document_type=document_type,
                )
            doc.ai_raw_response = ai_raw_response
            doc.extraction_model = ai_model_name
            real_ai_elapsed = time.monotonic() - real_ai_start
            if ai_skip_reason:
                cursor = ai_start + timedelta(seconds=real_ai_elapsed)
                timeline.append(_stage_record("AI Extraction", "skipped", ai_start, cursor, error=ai_skip_reason))
                doc.extraction_percent = None
            else:
                rec, cursor = _stage("AI Extraction", "completed", ai_start, real_ai_elapsed, extraction_conf)
                timeline.append(rec)
                doc.extraction_percent = extraction_conf

        # 4. Structured Dataset — packages the real extracted fields above;
        # skipped (not an empty fabricated table) when AI Extraction itself
        # was skipped.
        if fields:
            rec, cursor = _stage("Structured Dataset", "completed", cursor, rnd.uniform(0.3, 1.0), extraction_conf)
            timeline.append(rec)
            doc.dataset = build_dataset(fields)
        else:
            timeline.append(_stage_record("Structured Dataset", "skipped", cursor, cursor, error="No extracted fields to build a dataset from."))
            doc.dataset = None

        # 5. Validation — includes a simple business-rule check (a
        # total-amount field above an auto-approval threshold always needs
        # a human look, regardless of how confident extraction was about
        # the number). Field name is matched heuristically since real AI
        # Extraction proposes its own field names per document — see
        # _TOTAL_FIELD_HINTS.
        total_field = next(
            (f for f in fields if f.get("type") != "table" and any(h in f["name"].lower().replace(" ", "_") for h in _TOTAL_FIELD_HINTS)), None,
        )
        business_rule_flag = False
        if total_field and not total_field["missing"]:
            try:
                numeric_total = float(str(total_field["value"]).replace(",", "").replace("$", "").strip())
                business_rule_flag = numeric_total > settings.review_business_rule_total_threshold
            except ValueError:
                pass
        # Accuracy score — spec section 22's "required fields present /
        # schema validation" concept made numeric: the share of extracted
        # fields whose own validation_status isn't "invalid". Real, derived
        # from the same per-field validation_status the AI/OCR extraction
        # stage already computed — not a fabricated second score. Stored
        # inside extraction_result (additive dict key) rather than a new
        # Document column, same convention as business_rule_flag above.
        scoreable_fields = [f for f in fields if f.get("type") != "table"]
        accuracy_score = round(100 * sum(1 for f in scoreable_fields if f.get("validation_status") != "invalid") / len(scoreable_fields), 1) \
            if scoreable_fields else 100.0
        doc.extraction_result = (
            {"fields": fields, "business_rule_flag": business_rule_flag, "accuracy_score": accuracy_score, "document_type": document_type, "page_coverage": extraction_meta}
            if fields else None
        )

        pages_considered = set((extraction_meta or {}).get("pages_considered") or [])
        total_ocr_pages = (extraction_meta or {}).get("total_ocr_pages") or (doc.ocr_result or {}).get("page_count") or doc.page_count
        extraction_incomplete = bool(fields and total_ocr_pages and len(pages_considered) < int(total_ocr_pages))

        conf_components = [c for c in (scan_conf, ocr_conf, extraction_conf) if c is not None]
        overall_conf = round(sum(conf_components) / len(conf_components), 1)
        # confidence_threshold/accuracy_threshold default to 90 — the exact
        # value manual scan hardcoded before this parameter existed, so
        # every existing caller (manual scan, bulk upload, Review Queue
        # reprocess — none of which pass this yet) sees byte-identical
        # behavior. Only the Scheduled Document Intelligence Workflow
        # (schedule_service.py) passes a schedule-configured value.
        extraction_needs_review = (
            overall_conf < confidence_threshold
            or (fields and accuracy_score < accuracy_threshold)
            or any(f["validation_status"] == "invalid" for f in fields)
            or business_rule_flag
            or extraction_incomplete
        )
        # Multilingual Document Processing, Phase 6 — a real OCR quality/
        # language issue (low confidence, mismatch, empty/unicode error)
        # forces review even when every extracted field superficially looks
        # "valid" — the AI/Custom Engine can produce plausible-looking
        # values from garbled OCR text, and that must never pass as a quiet
        # success.
        needs_review = extraction_needs_review or bool(ocr_quality_flags)
        val_status = "completed" if not needs_review else "failed"
        validation_error = "Extraction did not consider every OCR page — flagged for manual review." if extraction_incomplete \
            else "Low confidence on one or more fields — flagged for manual review." if extraction_needs_review \
            else (ocr_quality_flags[0]["message"] if ocr_quality_flags else None)
        rec, cursor = _stage("Validation", val_status, cursor, rnd.uniform(0.2, 0.8), overall_conf, error=validation_error)
        timeline.append(rec)

        doc.confidence = overall_conf
        doc.pipeline = doc.pipeline or rnd.choice(PIPELINE_NAMES)
        doc.status = DocumentStatus.NEEDS_REVIEW if needs_review else DocumentStatus.COMPLETED
        doc.processing_started_at = now
        doc.processing_completed_at = cursor
        doc.processing_time_seconds = round((cursor - now).total_seconds(), 2)
        doc.error_message = None

    except Exception as exc:  # pragma: no cover - defensive; simulation shouldn't raise
        # A real failure mid-pipeline (e.g. a genuine SQLite "database is
        # locked" collision — bulk upload runs several documents' full
        # pipelines concurrently, each committing once at the end, and
        # busy_timeout doesn't guarantee zero contention under real load —
        # see database/connection.py) leaves this session's transaction in
        # SQLAlchemy's "pending rollback" state: any further use, including
        # the plain `doc.status = FAILED` + commit() this except block used
        # to do directly, re-raises PendingRollbackError instead of
        # persisting anything — silently stranding the document at
        # whatever status was last committed (observed live: two documents
        # stuck at SCANNING forever after a concurrent-bulk-upload lock
        # collision, cause found in this exact except block). Roll back
        # first to clear that state, then re-fetch a clean instance before
        # writing the failure — same document row, fresh unbroken session.
        await db.rollback()
        recovered = await db.get(Document, doc_id)
        if recovered is None:
            return doc  # row vanished mid-run (e.g. deleted concurrently) — nothing left to persist to
        doc = recovered
        doc.status = DocumentStatus.FAILED
        friendly_error = friendly_db_error(exc)
        doc.error_message = friendly_error
        timeline.append(_stage_record("Processing", "failed", cursor, datetime.now(timezone.utc), error=friendly_error))

    doc.timeline = timeline
    await db.commit()
    await db.refresh(doc)

    # Invoice Intelligence materialization (INV5) — only for a document
    # this run explicitly pinned to the INVOICE taxonomy (document_type
    # param above), never inferred/guessed from content. A failure here
    # must never fail the document's own processing result — Document
    # Intelligence stays fully usable even if invoice normalization has
    # a real bug on some edge-case document.
    if document_type == "INVOICE" and not skip_invoice_pipeline and doc.extraction_result and doc.extraction_result.get("fields"):
        try:
            from app.services.invoice import invoice_processing_service, invoice_validation_service, invoice_decision_service
            from app.models.invoice_intelligence.core import ApInvoiceDecision, ApInvoiceSource
            invoice = await invoice_processing_service.materialize_invoice(
                db, doc, source=ApInvoiceSource.SCHEDULE if doc.source_schedule_id else ApInvoiceSource.MANUAL_UPLOAD,
                source_schedule_id=doc.source_schedule_id,
            )
            await invoice_validation_service.seed_default_rules(db, doc.organization_id)
            validation_results = await invoice_validation_service.evaluate_invoice(db, invoice)
            decision = await invoice_decision_service.decide(db, invoice, validation_results)

            # Scheduler auto-post (INV11) — only ever fires on the
            # decision engine's own highest-confidence outcome. A
            # REVIEW_REQUIRED/REJECT/RETRY invoice is completely
            # unaffected by invoice_auto_post regardless of the schedule's
            # setting; this is never a way to skip review, only to skip
            # the manual "click Post to ERP" step on invoices the decision
            # engine already trusts. Reuses invoice_erp_service.
            # post_invoice() unmodified — same idempotent posting,
            # retry/circuit-breaker, and auto-reconciliation as a manual
            # post.
            if decision == ApInvoiceDecision.STRAIGHT_THROUGH and invoice_auto_post and invoice_erp_connection_id:
                from app.services.invoice import invoice_erp_service
                await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=invoice_erp_connection_id)
        except Exception:  # noqa: BLE001 - see comment above; never fail document processing over this
            pass

    # Processed Data Storage (Phase 3) — a real, auditable EXTERNAL push of
    # this document's OCR/extraction/dataset output, configured per org
    # (see services/document_storage_service.py's module docstring for how
    # this differs from Bring-Your-Own-Database). No-ops instantly for the
    # default (and overwhelmingly common) case where no org has opted in.
    # Only COMPLETED/NEEDS_REVIEW carry real processed output worth
    # pushing — a FAILED document has nothing new to export. Never fails
    # this function: push_document() itself catches everything.
    if doc.status in (DocumentStatus.COMPLETED, DocumentStatus.NEEDS_REVIEW):
        await document_storage_service.push_document(db, doc, organization_id=doc.organization_id, override=storage_override)

        # External API / webhook push (Phase 6, spec item #7) — a
        # genuinely separate destination from the DB/file push above
        # (see document_webhook_service.py's module docstring). Runs
        # independently: a webhook failure never affects the DB/file
        # push's own outcome, and vice versa. No-ops instantly when no
        # org has configured/enabled a webhook.
        await document_webhook_service.push_document(db, doc, organization_id=doc.organization_id)

    return doc


# ── Background execution ─────────────────────────────────────────────
# process_document() above is genuinely real work — Tesseract OCR plus up
# to two sequential live LLM calls (schema detection + field extraction),
# each with its own real network round trip and a real per-provider
# timeout of 60-120s (see connectors/ai/*). Run synchronously inside one
# HTTP request/response cycle, that can legitimately take well over a
# minute — confirmed live: a real document in this build measured
# processing_time_seconds ~63s end to end. This app's apiClient uses a
# 30s client-side timeout (a reasonable default for ordinary CRUD calls),
# so a synchronous call here reliably produced "No response from server.
# Check your connection." even on a fully successful run — not a network
# problem, a request that outlives the client's patience for one.
#
# Fixed the same way every other genuinely long-running job in this
# codebase is (PipelineJob, TargetLoadRun): fire-and-forget via
# asyncio.create_task, with the API route returning immediately once the
# document's status flips to a real "in progress" value, and the frontend
# polling GET /documents/{id} (see OCR.jsx) until it lands on a terminal
# status instead of awaiting one long call.

_running_tasks: dict[int, asyncio.Task] = {}
# Overall document-worker bound — SQLite's single-writer limit always wins
# regardless of DOCUMENT_WORKER_COUNT (see the "database is locked" note
# above); non-SQLite deployments use the configured value directly (default
# 4, same as this semaphore's previous hardcoded value).
_background_processing_semaphore = asyncio.Semaphore(
    1 if settings.database_url.startswith("sqlite") else settings.document_worker_count
)
# ZIP Batch Upload (ZB5) — finer sub-limits ACQUIRED INSIDE process_document()
# around just the OCR calls and just the AI-extraction call respectively, in
# ADDITION to the outer _background_processing_semaphore above. Lets an
# operator express "8 documents processing but only 4 concurrent AI
# requests" (spec section 57/58) instead of one single all-or-nothing cap.
# Process-wide and fixed at import time — a Semaphore can't be resized
# per-request, so these read the env-backed Settings, never the per-org
# runtime-editable settings blob.
_ocr_semaphore = asyncio.Semaphore(max(1, settings.ocr_concurrency))
_ai_semaphore = asyncio.Semaphore(max(1, settings.ai_concurrency))


def is_processing(document_id: int) -> bool:
    return document_id in _running_tasks


async def start_processing(db: AsyncSession, doc: Document, *, reprocess: bool, document_type: str | None = None, language_config: dict | None = None) -> Document:
    """Kicks off process_document() as a background task and returns
    immediately — the route handler awaits THIS, not the pipeline itself.
    If this document is already being processed, does not start a second
    overlapping run; just returns its current (still in-progress) state,
    same idempotency principle load_executor.start_load() uses.

    document_type (INV8) lets a caller that already knows what kind of
    document this is — e.g. api/invoices.py's upload route — pin AI
    Extraction to a known canonical taxonomy from the very first
    automatic run, same as process_document()'s own document_type param
    (INV11's invoice-typed schedules already pass this at the
    process_document() layer directly; this is the same seam exposed one
    level up, for a caller that only has start_processing()'s
    non-blocking entry point). None (the default) keeps every existing
    caller's original auto-detect behavior unchanged."""
    if is_processing(doc.id):
        return doc
    if language_config is not None:
        doc.language_config = normalize_ocr_language_config(language_config)
    doc.status = DocumentStatus.SCANNING
    doc.error_message = None
    await db.commit()
    await db.refresh(doc)

    document_id = doc.id
    task = asyncio.create_task(_process_in_background(document_id, reprocess, doc.organization_id, document_type, doc.language_config))
    _running_tasks[document_id] = task
    task.add_done_callback(lambda _t, did=document_id: _running_tasks.pop(did, None))
    return doc


async def _process_in_background(document_id: int, reprocess: bool, organization_id: int | None, document_type: str | None = None, language_config: dict | None = None) -> None:
    """Runs detached from the request that triggered it, so it needs its
    own DB session — the request's session is gone by the time this
    actually executes. process_document() already catches everything it
    can meaningfully attribute to a specific stage; this outer catch is
    belt-and-suspenders for anything that somehow still escapes (e.g. the
    document row itself vanishing mid-run), so a document can never be
    left stuck showing an in-progress status forever. organization_id
    comes from the doc row the caller (start_processing) already has in
    hand — see tenant_engine_registry.get_background_sessionmaker."""
    async with _background_processing_semaphore:
        sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
        async with sessionmaker() as db:
            doc = await db.get(Document, document_id)
            if doc is None:
                return
            try:
                doc = await process_document(db, doc, reprocess=reprocess, document_type=document_type, language_config=language_config)
            except Exception as exc:  # noqa: BLE001 - see docstring
            # process_document() already recovers from a broken session
            # internally (see its own except block) — this only runs if
            # something STILL escapes that, so roll back defensively here
            # too before writing anything; a bare commit on an already-
            # broken session just raises PendingRollbackError again and
            # leaves the row stuck silently instead of marked FAILED.
                await db.rollback()
                doc = await db.get(Document, document_id)
                if doc is None:
                    return
                doc.status = DocumentStatus.FAILED
                doc.error_message = friendly_db_error(exc)[:2000]
                await db.commit()
                return

        # Review Queue sync — previously done by the API route right after
        # awaiting process_document() inline; moved here since the route
        # no longer waits for the pipeline to finish. Local import avoids
        # a module-load-time dependency for the (much more common) code
        # paths in this file that never touch the review queue.
            from app.services import review_queue_service as review_svc
            if doc.status == DocumentStatus.NEEDS_REVIEW:
                item = await review_svc.create_from_document(db, doc)
                if item is None:
                # Multilingual Document Processing, Phase 6 — create_from_document()
                # only sees extraction-field issues; a document flagged for
                # review purely over a real OCR quality/language problem (see
                # evaluate_ocr_quality()) needs this fallback so it still gets
                # a visible Review Queue entry rather than none at all. Same
                # "returns None if an open item already exists" idempotency
                # guard as create_from_document() itself.
                    ocr_flags = (doc.ocr_result or {}).get("quality_flags") or []
                    if ocr_flags:
                        await review_svc.create_from_ocr_quality(db, doc, ocr_flags)
            else:
                await review_svc.auto_resolve_for_document(db, doc.id)
