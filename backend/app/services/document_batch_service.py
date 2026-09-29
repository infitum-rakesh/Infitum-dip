"""
ZIP Batch Upload — extraction + registration orchestration (ZB4).

Takes a COMPLETED BatchUpload (assembled + checksummed, see
batch_upload_service.py) and turns it into ONE DocumentBatch run plus MANY
DocumentBatchFile rows — never one pipeline or task per file's own separate
concept, and never a second processing engine: each REGISTERED
DocumentBatchFile is just a claimable pointer to a real extracted file on
disk, materialized into a real Document row and run through the existing
document_service.process_document() pipeline later (ZB6).

Flow, each stage persisted as a real DocumentBatch.status transition:

    UPLOADED -> VALIDATING -> EXTRACTING -> REGISTERING -> QUEUED

Idempotency (spec section 6/17/38): before doing any work, checks for an
existing DocumentBatch in the same organization with the same zip_sha256 —
uploading the identical archive twice returns the existing batch rather
than re-extracting/re-registering it. Within one archive, duplicate FILES
are detected by comparing each extracted entry's real SHA-256 against
existing Document.content_hash rows in the same organization (configurable
SKIP_DUPLICATE default / PROCESS_DUPLICATE / CREATE_NEW_VERSION) — the
classification walk is deliberately sequential (not parallelized) so two
identical files within the same ZIP can't both slip past the "does this
hash already exist" check before either commits.

Critical batch-level failures (corrupted/oversized/malicious ZIP) mark the
whole DocumentBatch FAILED and re-raise — per spec section 34, only these
kinds of errors are allowed to fail the entire batch; a single bad
extracted FILE (unsupported type, oversized, empty) is isolated as one
UNSUPPORTED DocumentBatchFile row and never aborts registration of the
rest.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database import tenant_engine_registry
from app.database.connection import AsyncSessionLocal
from app.models.document import Document, DocumentStatus
from app.models.document_batch import (
    ALLOWED_FILE_TRANSITIONS, BatchUpload, BatchUploadStatus, DocumentBatch, DocumentBatchEvent, DocumentBatchFile,
    DocumentBatchFileStatus, DocumentBatchPipelineType, DocumentBatchStatus, DuplicatePolicy,
)
from app.models.document_storage import ProcessedDataStorageFileFormat, ProcessedDataStorageLoadMode, ProcessedDataStorageType
from app.services import document_service, review_queue_service
from app.services.batch_storage_service import get_batch_storage
from app.services.zip_security_service import ExtractedEntry, ZipSecurityError, safe_extract, validate_zip

logger = logging.getLogger("document_batch_service")

# Retry policy (spec section 39) — mirrors bulk_migration_service.py's own
# exponential-backoff constants exactly.
_MAX_FILE_RETRIES = 3
_BACKOFF_BASE_SECONDS = 10
_BACKOFF_MAX_SECONDS = 90
_CLAIM_CANDIDATES = 10
_HEARTBEAT_INTERVAL_SECONDS = 30

# Non-retryable — a permanent/data problem retrying can never fix.
_NON_RETRYABLE_EXCEPTION_NAMES = {
    "DocumentValidationError",     # bad extension / empty / oversized — data problem
    "LanguageProcessingError",     # OCR_LANGUAGE_MODEL_MISSING — a configuration/installation gap, not transient
    "ConnectorValidationError",
}

settings = get_settings()


class DocumentBatchErrorCode(str, enum.Enum):
    UPLOAD_NOT_READY = "UPLOAD_NOT_READY"
    ZIP_VALIDATION_FAILED = "ZIP_VALIDATION_FAILED"
    ZIP_EXTRACTION_FAILED = "ZIP_EXTRACTION_FAILED"
    INVALID_BATCH_STATE = "INVALID_BATCH_STATE"


class DocumentBatchError(Exception):
    def __init__(self, code: DocumentBatchErrorCode, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


async def get_batch(db: AsyncSession, batch_id: int, organization_id: int | None) -> DocumentBatch | None:
    """Org-scoped lookup — never returns another tenant's batch row. Same
    `organization_id is None` == "no scoping" convention as
    document_service.get_document() (ZB10's own API layer passes the
    caller's _effective_org_id(user) straight through here, and that
    helper returns None for SUPER_ADMIN specifically so they see across
    every org — an unconditional equality check would instead wrongly
    restrict a super_admin to rows where organization_id IS NULL)."""
    stmt = select(DocumentBatch).where(DocumentBatch.id == batch_id)
    if organization_id is not None:
        stmt = stmt.where(DocumentBatch.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_batch_files(db: AsyncSession, batch_id: int) -> list[DocumentBatchFile]:
    result = await db.execute(
        select(DocumentBatchFile).where(DocumentBatchFile.batch_id == batch_id).order_by(DocumentBatchFile.id.asc())
    )
    return list(result.scalars().all())


# ── API-layer query helpers (ZB10) — thin, route-shaping queries only,
# same "no new business logic in the route file" precedent as
# bulk_migration.py's own list_runs()/list_events() (see that router's
# docstring precedent) ──────────────────────────────────────────────────


async def list_batches(
    db: AsyncSession, organization_id: int | None, pipeline_type: DocumentBatchPipelineType, *,
    status_filter: str | None = None, page: int = 1, page_size: int = 20,
) -> tuple[list[DocumentBatch], int]:
    stmt = select(DocumentBatch).where(DocumentBatch.pipeline_type == pipeline_type)
    if organization_id is not None:
        stmt = stmt.where(DocumentBatch.organization_id == organization_id)
    if status_filter:
        stmt = stmt.where(DocumentBatch.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(DocumentBatch.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(stmt)).scalars().all()
    return list(items), total


async def list_batch_files_paginated(
    db: AsyncSession, batch_id: int, *, status_filter: str | None = None, page: int = 1, page_size: int = 50,
) -> tuple[list[DocumentBatchFile], int]:
    stmt = select(DocumentBatchFile).where(DocumentBatchFile.batch_id == batch_id)
    if status_filter:
        stmt = stmt.where(DocumentBatchFile.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(DocumentBatchFile.id.asc()).offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(stmt)).scalars().all()
    return list(items), total


async def list_batch_events(db: AsyncSession, batch_id: int, *, since_id: int = 0, limit: int = 200) -> tuple[list[DocumentBatchEvent], int]:
    """Incremental polling shape — same since_id cursor convention as
    bulk_migration.py's list_events() route, backed by DocumentBatchEvent's
    own autoincrement id (see that model's docstring)."""
    stmt = (
        select(DocumentBatchEvent)
        .where(DocumentBatchEvent.batch_id == batch_id, DocumentBatchEvent.id > since_id)
        .order_by(DocumentBatchEvent.id.asc()).limit(limit)
    )
    events = (await db.execute(stmt)).scalars().all()
    new_since_id = events[-1].id if events else since_id
    return list(events), new_since_id


async def get_batch_metrics(db: AsyncSession, batch: DocumentBatch) -> dict:
    counts_raw = (await db.execute(
        select(DocumentBatchFile.status, func.count()).where(DocumentBatchFile.batch_id == batch.id).group_by(DocumentBatchFile.status)
    )).all()
    status_counts = {(k.value if hasattr(k, "value") else k): v for k, v in counts_raw}
    elapsed_seconds = None
    if batch.started_at:
        end = batch.completed_at or datetime.now(timezone.utc)
        elapsed_seconds = (_naive_utc(end) - _naive_utc(batch.started_at)).total_seconds()
    return {
        "batch_id": batch.id, "status_counts": status_counts,
        "total_entries": batch.total_entries, "valid_files": batch.valid_files, "duplicate_files": batch.duplicate_files,
        "unsupported_files": batch.unsupported_files, "completed_files": batch.completed_files,
        "failed_files": batch.failed_files, "review_files": batch.review_files, "elapsed_seconds": elapsed_seconds,
    }


async def list_batch_errors_paginated(db: AsyncSession, batch_id: int, *, page: int = 1, page_size: int = 50) -> tuple[list[DocumentBatchFile], int]:
    """"Errors" = any file row with a non-null error_code — see
    export_batch_errors_csv()'s docstring for why that's the right filter
    (covers both genuine processing FAILUREs and classification-time
    rejects, which all carry a real error_code/error_message)."""
    stmt = select(DocumentBatchFile).where(DocumentBatchFile.batch_id == batch_id, DocumentBatchFile.error_code.is_not(None))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(DocumentBatchFile.id.asc()).offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(stmt)).scalars().all()
    return list(items), total


async def export_batch_errors_csv(db: AsyncSession, batch_id: int) -> tuple[bytes, str, str]:
    """Same shape as audit_log_service.export_entries() — csv module +
    io.StringIO built in the service layer, route just wraps it in a
    Response with a Content-Disposition header. "Errors" = any file row
    with a non-null error_code — covers genuine processing FAILUREs as
    well as classification-time rejects (UNSUPPORTED/NESTED_ARCHIVE_NOT_
    SUPPORTED), all of which carry a real error_code/error_message."""
    import csv
    import io

    rows = (await db.execute(
        select(DocumentBatchFile)
        .where(DocumentBatchFile.batch_id == batch_id, DocumentBatchFile.error_code.is_not(None))
        .order_by(DocumentBatchFile.id.asc())
    )).scalars().all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["zip_relative_path", "original_filename", "status", "error_code", "error_message", "attempt_number"])
    for f in rows:
        status_value = f.status.value if hasattr(f.status, "value") else f.status
        writer.writerow([f.zip_relative_path, f.original_filename, status_value, f.error_code or "", (f.error_message or "").replace("\n", " "), f.attempt_number])
    return buf.getvalue().encode("utf-8"), "text/csv", f"batch-{batch_id}-errors.csv"


async def _find_existing_batch_for_zip(db: AsyncSession, organization_id: int | None, zip_sha256: str) -> DocumentBatch | None:
    """Real bug found live: this used to return the FIRST batch matching
    (org, zip_sha256) unconditionally, with no check that it actually
    finished usably. A batch created before delete_document()'s referential-
    cleanup fix (or one hit by any other since-fixed bug) can be terminal
    and "COMPLETED" while every one of its settled files still has
    document_id=NULL — so re-uploading the exact same ZIP forever handed
    back that same broken, undead batch instead of ever giving the user a
    working run. Now only short-circuits onto a batch that is (a) actually
    terminal and (b) has no settled, non-skipped file missing its document
    link — the two conditions that distinguish "genuinely already
    processed, don't redo the work" (the intended idempotency, spec
    section 6/17/38) from "processed under conditions that no longer hold."
    """
    result = await db.execute(
        select(DocumentBatch).where(
            DocumentBatch.organization_id == organization_id,
            DocumentBatch.zip_sha256 == zip_sha256,
            DocumentBatch.status.in_([DocumentBatchStatus.COMPLETED, DocumentBatchStatus.COMPLETED_WITH_ERRORS]),
        ).order_by(DocumentBatch.id.asc())
    )
    for candidate in result.scalars().all():
        broken = await db.execute(
            select(func.count()).select_from(DocumentBatchFile).where(
                DocumentBatchFile.batch_id == candidate.id,
                DocumentBatchFile.status.in_([DocumentBatchFileStatus.COMPLETED, DocumentBatchFileStatus.REVIEW_REQUIRED]),
                DocumentBatchFile.document_id.is_(None),
            )
        )
        if broken.scalar_one() == 0:
            return candidate
    return None


async def _find_duplicate_document(db: AsyncSession, organization_id: int | None, sha256: str) -> Document | None:
    result = await db.execute(
        select(Document).where(Document.organization_id == organization_id, Document.content_hash == sha256)
    )
    return result.scalars().first()


async def _fail_batch(db: AsyncSession, batch: DocumentBatch, code: DocumentBatchErrorCode, message: str) -> None:
    batch.status = DocumentBatchStatus.FAILED
    batch.error_code = code.value
    batch.error_message = message
    await db.commit()


async def create_batch_from_upload(
    db: AsyncSession, *, upload: BatchUpload, pipeline_type: DocumentBatchPipelineType,
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.SKIP_DUPLICATE,
    auto_process: bool = True, created_by: int | None = None,
    storage_type: str | None = None, storage_target_connection_id: int | None = None,
    storage_target_schema: str | None = None, storage_target_table: str | None = None,
    storage_load_mode: str | None = None, storage_file_format: str | None = None,
) -> DocumentBatch:
    """storage_type/storage_target_* (Part 21 — "DATA STORAGE FOR ZIP
    PROCESSING") persist this batch's own Processed Data Storage override,
    straight onto the DocumentBatch row — "persisted with the processing
    batch/job" per that spec's own closing line. storage_type=None (the
    default — the upload dialog's own "SQLite" default choice) means no
    override at all; see models/document_batch.py's column docstring and
    document_storage_service._effective_config()."""
    if upload.status != BatchUploadStatus.COMPLETED or not upload.sha256:
        raise DocumentBatchError(
            DocumentBatchErrorCode.UPLOAD_NOT_READY,
            f"Upload {upload.id} is {upload.status.value}; it must be COMPLETED before a batch can be created from it.",
        )

    existing = await _find_existing_batch_for_zip(db, upload.organization_id, upload.sha256)
    if existing is not None:
        return existing

    resolved_storage_type = ProcessedDataStorageType(storage_type) if storage_type else None
    if resolved_storage_type is not None and resolved_storage_type != ProcessedDataStorageType.SQLITE and not storage_target_connection_id:
        raise DocumentBatchError(
            DocumentBatchErrorCode.INVALID_BATCH_STATE,
            "Select a target connection before choosing Database or File as this batch's Storage type.",
        )

    batch = DocumentBatch(
        organization_id=upload.organization_id, upload_id=upload.id, pipeline_type=pipeline_type,
        original_filename=upload.filename, zip_sha256=upload.sha256, duplicate_policy=duplicate_policy,
        status=DocumentBatchStatus.VALIDATING, auto_process=auto_process, created_by=created_by,
        storage_type=resolved_storage_type, storage_target_connection_id=storage_target_connection_id,
        storage_target_schema=storage_target_schema, storage_target_table=storage_target_table,
        storage_load_mode=ProcessedDataStorageLoadMode(storage_load_mode) if storage_load_mode else None,
        storage_file_format=ProcessedDataStorageFileFormat(storage_file_format) if storage_file_format else None,
    )
    db.add(batch)
    await db.commit()
    await db.refresh(batch)

    storage = get_batch_storage()
    assembled_path = storage.assembled_path(upload.organization_id, upload.id)

    try:
        validate_zip(assembled_path, max_zip_size_mb=settings.max_zip_size_mb)
    except ZipSecurityError as exc:
        await _fail_batch(db, batch, DocumentBatchErrorCode.ZIP_VALIDATION_FAILED, f"{exc.code.value}: {exc.message}")
        raise

    batch.status = DocumentBatchStatus.EXTRACTING
    await db.commit()

    dest_root = storage.batch_extracted_root(upload.organization_id, batch.id)
    try:
        entries = safe_extract(
            assembled_path, dest_root,
            max_extracted_size_mb=settings.max_extracted_size_mb,
            max_files_per_zip=settings.max_files_per_zip,
            max_single_file_size_mb=settings.max_single_file_size_mb,
            max_compression_ratio=settings.max_compression_ratio,
            max_directory_depth=settings.max_zip_depth,
            allow_nested_archives=settings.allow_nested_zip,
        )
    except ZipSecurityError as exc:
        await _fail_batch(db, batch, DocumentBatchErrorCode.ZIP_EXTRACTION_FAILED, f"{exc.code.value}: {exc.message}")
        raise

    batch.status = DocumentBatchStatus.REGISTERING
    await db.commit()

    counters = {"total": 0, "valid": 0, "duplicate": 0, "unsupported": 0}
    pending_rows: list[DocumentBatchFile] = []
    max_upload_bytes = document_service.settings.max_upload_size_mb * 1024 * 1024

    async def _flush_pending():
        if not pending_rows:
            return
        db.add_all(pending_rows)
        await db.commit()
        pending_rows.clear()

    for entry in entries:  # type: ExtractedEntry
        counters["total"] += 1

        if entry.classification == "nested_archive":
            pending_rows.append(DocumentBatchFile(
                batch_id=batch.id, zip_relative_path=entry.zip_relative_path,
                original_filename=entry.zip_relative_path.rsplit("/", 1)[-1],
                normalized_filename=entry.zip_relative_path.rsplit("/", 1)[-1],
                file_extension="zip", file_size=0, sha256="", storage_key="",
                status=DocumentBatchFileStatus.NESTED_ARCHIVE_NOT_SUPPORTED,
                error_code="NESTED_ARCHIVE_NOT_SUPPORTED",
                error_message="Nested ZIP archives are not extracted by default (allow_nested_archives is off).",
            ))
            counters["unsupported"] += 1
            if len(pending_rows) >= settings.batch_insert_size:
                await _flush_pending()
            continue

        original_filename = entry.zip_relative_path.rsplit("/", 1)[-1]
        ext = _extension_of(original_filename)
        row_kwargs = dict(
            batch_id=batch.id, zip_relative_path=entry.zip_relative_path,
            original_filename=original_filename, normalized_filename=original_filename,
            file_extension=ext or None, file_size=entry.size, sha256=entry.sha256,
            storage_key=str(entry.extracted_path),
        )

        if ext not in document_service.EXTENSION_TYPE_MAP:
            pending_rows.append(DocumentBatchFile(
                **row_kwargs, status=DocumentBatchFileStatus.UNSUPPORTED,
                error_code="UNSUPPORTED_FILE_TYPE",
                error_message=f"'.{ext}' is not a supported document type.",
            ))
            counters["unsupported"] += 1
        elif entry.size == 0:
            pending_rows.append(DocumentBatchFile(
                **row_kwargs, status=DocumentBatchFileStatus.UNSUPPORTED,
                error_code="EMPTY_FILE", error_message="File is empty.",
            ))
            counters["unsupported"] += 1
        elif entry.size > max_upload_bytes:
            pending_rows.append(DocumentBatchFile(
                **row_kwargs, status=DocumentBatchFileStatus.UNSUPPORTED,
                error_code="FILE_TOO_LARGE",
                error_message=f"File is {entry.size:,} bytes, exceeding the configured per-document MAX_UPLOAD_SIZE_MB={document_service.settings.max_upload_size_mb}.",
            ))
            counters["unsupported"] += 1
        else:
            duplicate = await _find_duplicate_document(db, upload.organization_id, entry.sha256)
            if duplicate is not None and duplicate_policy == DuplicatePolicy.SKIP_DUPLICATE:
                pending_rows.append(DocumentBatchFile(
                    **row_kwargs, status=DocumentBatchFileStatus.SKIPPED_DUPLICATE,
                    duplicate_of_document_id=duplicate.id,
                ))
                counters["duplicate"] += 1
            else:
                pending_rows.append(DocumentBatchFile(
                    **row_kwargs, status=DocumentBatchFileStatus.REGISTERED,
                    duplicate_of_document_id=duplicate.id if duplicate is not None else None,
                ))
                counters["valid"] += 1

        if len(pending_rows) >= settings.batch_insert_size:
            await _flush_pending()

    await _flush_pending()

    batch.total_entries = counters["total"]
    batch.valid_files = counters["valid"]
    batch.duplicate_files = counters["duplicate"]
    batch.unsupported_files = counters["unsupported"]
    batch.status = DocumentBatchStatus.QUEUED if counters["valid"] > 0 else DocumentBatchStatus.COMPLETED
    await db.commit()
    await db.refresh(batch)

    if batch.status == DocumentBatchStatus.QUEUED and auto_process:
        await start_processing_for_batch(db, batch)

    return batch


# ── Worker pool / task executor (ZB6) ───────────────────────────────
#
# claim_task() uses the same portable atomic CAS as
# bulk_migration_service.claim_task() (UPDATE ... WHERE status IN (...) +
# rowcount check, not SELECT ... FOR UPDATE) — SQLite has no row-level
# locking, and this idiom works identically across SQLite/Postgres/MySQL/
# SQL Server. Each claimed DocumentBatchFile is executed with ONE
# long-lived session held for its whole lifetime (mirrors
# document_service._process_in_background()'s own per-document session,
# NOT bulk_migration_task_executor's per-chunk-session shape — a single
# document's OCR+AI call is one logical unit of work, not a resumable
# stream of row batches).
#
# Heartbeat: a background coroutine touches last_heartbeat every
# _HEARTBEAT_INTERVAL_SECONDS while a file is claimed, cancelled the
# moment that file's processing finishes (success or failure) — chosen
# over a fixed stale-timeout because OCR+AI duration is unbounded (a
# large multi-page PDF or a slow cloud OCR/AI call can legitimately run
# for minutes); a fixed threshold would falsely reclaim genuinely-slow-
# but-alive work. See recover_stuck_document_batches() (ZB8) for the
# periodic sweep that actually acts on a stale heartbeat.


async def claim_task(db: AsyncSession, batch_id: int, worker_id: str) -> DocumentBatchFile | None:
    """REGISTERED (ZB4's fresh-registration resting state) and QUEUED
    (ALLOWED_FILE_TRANSITIONS' own documented REGISTERED->QUEUED->PROCESSING
    graph) are both treated as claimable here — there's no real behavioral
    difference between them at the file level (the batch's own QUEUED
    status already captures "the whole batch is queued"), so this keeps
    claim_task's actual runtime behavior consistent with the model's
    documented transition graph without a pointless extra DB write per
    file to move REGISTERED->QUEUED before it can ever be claimed."""
    now = datetime.now(timezone.utc)
    eligible = (
        (DocumentBatchFile.status.in_([DocumentBatchFileStatus.REGISTERED, DocumentBatchFileStatus.QUEUED]))
        | ((DocumentBatchFile.status == DocumentBatchFileStatus.RETRY_PENDING) & (DocumentBatchFile.retry_after <= now))
    )
    candidate_ids = (await db.execute(
        select(DocumentBatchFile.id)
        .where(DocumentBatchFile.batch_id == batch_id, eligible)
        .order_by(DocumentBatchFile.id.asc())
        .limit(_CLAIM_CANDIDATES)
    )).scalars().all()

    for candidate_id in candidate_ids:
        result = await db.execute(
            update(DocumentBatchFile)
            .where(DocumentBatchFile.id == candidate_id, eligible)
            .values(status=DocumentBatchFileStatus.PROCESSING, worker_id=worker_id, claimed_at=now, started_at=now, last_heartbeat=now)
        )
        await db.commit()
        if result.rowcount == 1:
            return await db.get(DocumentBatchFile, candidate_id)
    return None


def _is_retryable(exc: Exception) -> bool:
    return type(exc).__name__ not in _NON_RETRYABLE_EXCEPTION_NAMES


async def _emit_event(db: AsyncSession, batch_id: int, file_id: int | None, event_type: str, *, message: str | None = None, worker_id: str | None = None) -> None:
    db.add(DocumentBatchEvent(batch_id=batch_id, file_id=file_id, event_type=event_type, message=message, worker_id=worker_id))
    await db.commit()


async def _heartbeat_loop(organization_id: int | None, file_id: int) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        async with sessionmaker() as hb_db:
            await hb_db.execute(
                update(DocumentBatchFile).where(DocumentBatchFile.id == file_id).values(last_heartbeat=datetime.now(timezone.utc))
            )
            await hb_db.commit()


def _storage_override_dict(batch: DocumentBatch) -> dict | None:
    """Builds the override dict document_storage_service.push_document()
    expects from a DocumentBatch row's own storage_* columns (Part 21).
    storage_type=None (the upload dialog's default "SQLite" choice) means
    no override was ever set on this batch — returns None so
    push_document() applies the org-wide config exactly as before this
    feature existed."""
    if batch.storage_type is None:
        return None
    return {
        "storage_type": batch.storage_type.value,
        "target_connection_id": batch.storage_target_connection_id,
        "target_schema": batch.storage_target_schema,
        "target_table": batch.storage_target_table,
        "load_mode": batch.storage_load_mode.value if batch.storage_load_mode else None,
        "file_format": batch.storage_file_format.value if batch.storage_file_format else None,
    }


async def _execute_one_file_task(
    organization_id: int | None, batch_id: int, pipeline_type: DocumentBatchPipelineType, file_id: int, worker_id: str,
) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    heartbeat_task = asyncio.create_task(_heartbeat_loop(organization_id, file_id))
    try:
        async with sessionmaker() as db:
            file_row = await db.get(DocumentBatchFile, file_id)
            if file_row is None:
                return
            file_row.attempt_number += 1
            await db.commit()
            await _emit_event(db, batch_id, file_id, "FILE_CLAIMED", worker_id=worker_id)

            batch_row = await db.get(DocumentBatch, batch_id)
            storage_override = _storage_override_dict(batch_row) if batch_row is not None else None

            try:
                content = Path(file_row.storage_key).read_bytes()
                doc = await document_service.save_from_source(
                    db, content=content, filename=file_row.original_filename, organization_id=organization_id,
                )
                file_row = await db.get(DocumentBatchFile, file_id)
                file_row.document_id = doc.id
                await db.commit()

                doc = await document_service.process_document(
                    db, doc,
                    document_type="INVOICE" if pipeline_type == DocumentBatchPipelineType.INVOICE_INTELLIGENCE else None,
                    storage_override=storage_override,
                )

                # For INVOICE, process_document() already routed
                # REVIEW_REQUIRED through review_queue_service internally
                # via its own invoice materialize/validate/decide chain
                # (see that function's docstring) — calling
                # create_from_document() again here would be redundant
                # (though harmless — it's idempotent) so it's skipped for
                # that pipeline type, mirroring how schedule_service.py's
                # _process_one_file() does the same split.
                #
                # Real bug found live: create_from_document() only inspects
                # doc.extraction_result["fields"] (_pick_primary_issue()) and
                # returns None when the fields themselves are clean — which
                # is exactly the case for a NEEDS_REVIEW verdict driven by
                # OCR quality flags instead. document_service.py's own
                # _process_in_background() (the manual/bulk-upload path)
                # covers that with a create_from_ocr_quality() fallback;
                # this per-batch-file path called process_document() the
                # same way but never had that fallback, so a batch document
                # flagged NEEDS_REVIEW purely on OCR quality silently never
                # got a ReviewQueueItem at all. Mirrored here exactly.
                if pipeline_type != DocumentBatchPipelineType.INVOICE_INTELLIGENCE and doc.status == DocumentStatus.NEEDS_REVIEW:
                    item = await review_queue_service.create_from_document(db, doc)
                    if item is None:
                        ocr_flags = (doc.ocr_result or {}).get("quality_flags") or []
                        if ocr_flags:
                            await review_queue_service.create_from_ocr_quality(db, doc, ocr_flags)

                file_row = await db.get(DocumentBatchFile, file_id)
                if doc.status == DocumentStatus.NEEDS_REVIEW:
                    file_row.status = DocumentBatchFileStatus.REVIEW_REQUIRED
                    counter_field = "review_files"
                elif doc.status == DocumentStatus.FAILED:
                    file_row.status = DocumentBatchFileStatus.FAILED
                    file_row.error_code = "PROCESSING_FAILED"
                    file_row.error_message = doc.error_message
                    counter_field = "failed_files"
                else:
                    file_row.status = DocumentBatchFileStatus.COMPLETED
                    counter_field = "completed_files"
                file_row.completed_at = datetime.now(timezone.utc)
                await db.commit()
                await db.execute(
                    update(DocumentBatch).where(DocumentBatch.id == batch_id)
                    .values(**{counter_field: getattr(DocumentBatch, counter_field) + 1})
                )
                await db.commit()
                await _emit_event(db, batch_id, file_id, f"FILE_{file_row.status.value}", worker_id=worker_id)

            except Exception as exc:  # noqa: BLE001 - one file's failure must never sink the whole batch (spec section 34)
                logger.exception("Batch file %s failed during processing", file_id)
                file_row = await db.get(DocumentBatchFile, file_id)
                if file_row is None:
                    return
                retryable = _is_retryable(exc)
                if retryable and file_row.attempt_number < _MAX_FILE_RETRIES:
                    backoff = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (file_row.attempt_number - 1)))
                    jitter = random.uniform(0, backoff * 0.2)
                    file_row.status = DocumentBatchFileStatus.RETRY_PENDING
                    file_row.retry_after = datetime.now(timezone.utc) + timedelta(seconds=backoff + jitter)
                else:
                    file_row.status = DocumentBatchFileStatus.FAILED
                    await db.execute(
                        update(DocumentBatch).where(DocumentBatch.id == batch_id)
                        .values(failed_files=DocumentBatch.failed_files + 1)
                    )
                file_row.error_code = type(exc).__name__
                file_row.error_message = str(exc)[:2000]
                await db.commit()
                await _emit_event(db, batch_id, file_id, f"FILE_{file_row.status.value}", message=str(exc)[:500], worker_id=worker_id)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def _one_worker(organization_id: int | None, batch_id: int, pipeline_type: DocumentBatchPipelineType, worker_id: str) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    while True:
        async with sessionmaker() as db:
            batch = await db.get(DocumentBatch, batch_id)
            if batch is None or batch.cancel_requested or batch.pause_requested:
                return
            task = await claim_task(db, batch_id, worker_id)
        if task is None:
            return
        await _execute_one_file_task(organization_id, batch_id, pipeline_type, task.id, worker_id)


async def _finalize_batch_if_done(batch_id: int, organization_id: int | None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        batch = await db.get(DocumentBatch, batch_id)
        if batch is None or batch.status in (
            DocumentBatchStatus.COMPLETED, DocumentBatchStatus.COMPLETED_WITH_ERRORS,
            DocumentBatchStatus.CANCELLED, DocumentBatchStatus.FAILED,
        ):
            return
        remaining = (await db.execute(
            select(func.count()).select_from(DocumentBatchFile).where(
                DocumentBatchFile.batch_id == batch_id,
                DocumentBatchFile.status.in_([
                    DocumentBatchFileStatus.REGISTERED, DocumentBatchFileStatus.QUEUED,
                    DocumentBatchFileStatus.PROCESSING, DocumentBatchFileStatus.RETRY_PENDING,
                ]),
            )
        )).scalar_one()

        # By the time every worker in THIS pool's asyncio.gather() has
        # returned, none of them is still mid-file — _one_worker()'s loop
        # only checks cancel/pause BETWEEN claims, so any file it had
        # claimed has already fully run through _execute_one_file_task()
        # before the worker exits. A file genuinely stuck at PROCESSING
        # here means an EARLIER pool crashed mid-file — that's
        # recover_stuck_document_batches()'s job (ZB8), not this function's.
        if batch.cancel_requested:
            batch.status = DocumentBatchStatus.CANCELLED
            batch.completed_at = datetime.now(timezone.utc)
        elif batch.pause_requested:
            # Paused — parked, not finished. completed_at stays unset;
            # resume_batch() picks up right where this left off (REGISTERED/
            # RETRY_PENDING rows are untouched, still claimable).
            batch.status = DocumentBatchStatus.PAUSED
        elif remaining > 0:
            # Retries still waiting on their backoff window (or, in principle,
            # a stray claimable row) — nothing left for THIS worker pool to
            # do right now. Left as PROCESSING; the periodic stale-heartbeat
            # sweep (ZB8) redispatches a fresh worker pool once backoffs
            # expire, so this is never a permanent stall.
            return
        else:
            batch.status = DocumentBatchStatus.COMPLETED_WITH_ERRORS if batch.failed_files > 0 else DocumentBatchStatus.COMPLETED
            batch.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await _emit_event(db, batch_id, None, f"BATCH_{batch.status.value}")


async def _run_worker_pool(batch_id: int, organization_id: int | None, pipeline_type: DocumentBatchPipelineType, effective_workers: int) -> None:
    worker_ids = [f"{uuid.uuid4().hex[:8]}-w{i}" for i in range(max(1, effective_workers))]
    await asyncio.gather(*[_one_worker(organization_id, batch_id, pipeline_type, wid) for wid in worker_ids])
    await _finalize_batch_if_done(batch_id, organization_id)


async def start_processing_for_batch(db: AsyncSession, batch: DocumentBatch) -> None:
    """Dispatches the bounded worker pool for a QUEUED batch — real
    fire-and-forget asyncio.create_task, same pattern as
    document_service.start_processing()/bulk_migration_service.start_run().
    Idempotent no-op for a batch that isn't QUEUED (already PROCESSING,
    or terminal)."""
    if batch.status != DocumentBatchStatus.QUEUED:
        return
    batch.status = DocumentBatchStatus.PROCESSING
    batch.started_at = datetime.now(timezone.utc)
    await db.commit()
    await _emit_event(db, batch.id, None, "BATCH_PROCESSING_STARTED")
    asyncio.create_task(_run_worker_pool(batch.id, batch.organization_id, batch.pipeline_type, batch.effective_workers))


# ── Batch controls (ZB7) — pause/resume/cancel/retry-failed/retry-selected ──


async def request_pause(db: AsyncSession, batch: DocumentBatch) -> DocumentBatch:
    """Cooperative — the running worker pool's _one_worker() loops check
    pause_requested BETWEEN claims (never mid-file), so this takes effect
    once every in-flight file finishes, not instantly. _finalize_batch_if_
    done() flips status to PAUSED once that pool's asyncio.gather() returns."""
    if batch.status != DocumentBatchStatus.PROCESSING:
        raise DocumentBatchError(
            DocumentBatchErrorCode.INVALID_BATCH_STATE,
            f"Cannot pause a batch in status {batch.status.value} — only a PROCESSING batch can be paused.",
        )
    batch.pause_requested = True
    await db.commit()
    await _emit_event(db, batch.id, None, "BATCH_PAUSE_REQUESTED")
    return batch


async def resume_batch(db: AsyncSession, batch: DocumentBatch) -> DocumentBatch:
    """Clears the pause and redispatches a fresh worker pool — REGISTERED/
    RETRY_PENDING file rows were never touched while paused, so this picks
    up exactly where it left off, no re-work."""
    if batch.status != DocumentBatchStatus.PAUSED:
        raise DocumentBatchError(
            DocumentBatchErrorCode.INVALID_BATCH_STATE,
            f"Cannot resume a batch in status {batch.status.value} — only a PAUSED batch can be resumed.",
        )
    batch.pause_requested = False
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await _emit_event(db, batch.id, None, "BATCH_RESUMED")
    asyncio.create_task(_run_worker_pool(batch.id, batch.organization_id, batch.pipeline_type, batch.effective_workers))
    return batch


async def request_cancel(db: AsyncSession, batch: DocumentBatch) -> DocumentBatch:
    """A PROCESSING batch's cancel is cooperative, same as pause — the
    running pool notices between claims and _finalize_batch_if_done()
    marks it CANCELLED once workers stop. A QUEUED or PAUSED batch has no
    active worker pool that will ever notice the flag on its own, so those
    are finalized immediately here instead."""
    if batch.status in (
        DocumentBatchStatus.COMPLETED, DocumentBatchStatus.COMPLETED_WITH_ERRORS,
        DocumentBatchStatus.CANCELLED, DocumentBatchStatus.FAILED,
    ):
        return batch  # already terminal — nothing to cancel
    batch.cancel_requested = True
    if batch.status in (DocumentBatchStatus.QUEUED, DocumentBatchStatus.PAUSED):
        batch.status = DocumentBatchStatus.CANCELLED
        batch.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await _emit_event(db, batch.id, None, "BATCH_CANCELLED" if batch.status == DocumentBatchStatus.CANCELLED else "BATCH_CANCEL_REQUESTED")
    return batch


async def retry_failed(db: AsyncSession, batch: DocumentBatch) -> DocumentBatch:
    """Resets every FAILED file in this batch back to REGISTERED (fresh
    attempt_number, cleared error/claim state) and redispatches. Successful
    documents are never touched — per spec, retry never re-runs work that
    already succeeded."""
    if batch.status not in (DocumentBatchStatus.COMPLETED_WITH_ERRORS, DocumentBatchStatus.FAILED):
        raise DocumentBatchError(
            DocumentBatchErrorCode.INVALID_BATCH_STATE,
            f"Cannot retry failed files on a batch in status {batch.status.value}.",
        )
    result = await db.execute(
        update(DocumentBatchFile)
        .where(DocumentBatchFile.batch_id == batch.id, DocumentBatchFile.status == DocumentBatchFileStatus.FAILED)
        .values(
            status=DocumentBatchFileStatus.REGISTERED, attempt_number=0, error_code=None, error_message=None,
            retry_after=None, worker_id=None, claimed_at=None, started_at=None, completed_at=None,
        )
    )
    reset_count = result.rowcount
    if reset_count == 0:
        return batch
    batch.failed_files = max(0, batch.failed_files - reset_count)
    batch.status = DocumentBatchStatus.QUEUED
    batch.cancel_requested = False
    await db.commit()
    await db.refresh(batch)
    await _emit_event(db, batch.id, None, "BATCH_RETRY_FAILED_REQUESTED", message=f"{reset_count} failed file(s) reset for retry.")
    await start_processing_for_batch(db, batch)
    return batch


async def retry_selected(db: AsyncSession, batch: DocumentBatch, file_ids: list[int]) -> DocumentBatch:
    """Same reset as retry_failed(), scoped to explicitly chosen file ids —
    and, unlike retry_failed(), allowed to include COMPLETED/REVIEW_REQUIRED
    files too (an operator explicitly re-running a specific document is a
    deliberate choice, not an accidental re-run of already-good work)."""
    rows = (await db.execute(
        select(DocumentBatchFile).where(
            DocumentBatchFile.batch_id == batch.id,
            DocumentBatchFile.id.in_(file_ids),
            DocumentBatchFile.status.in_([
                DocumentBatchFileStatus.FAILED, DocumentBatchFileStatus.COMPLETED, DocumentBatchFileStatus.REVIEW_REQUIRED,
            ]),
        )
    )).scalars().all()
    if not rows:
        return batch

    counter_deltas = {"failed_files": 0, "completed_files": 0, "review_files": 0}
    for f in rows:
        if f.status == DocumentBatchFileStatus.FAILED:
            counter_deltas["failed_files"] -= 1
        elif f.status == DocumentBatchFileStatus.COMPLETED:
            counter_deltas["completed_files"] -= 1
        elif f.status == DocumentBatchFileStatus.REVIEW_REQUIRED:
            counter_deltas["review_files"] -= 1
        f.status = DocumentBatchFileStatus.REGISTERED
        f.attempt_number = 0
        f.error_code = None
        f.error_message = None
        f.retry_after = None
        f.worker_id = None
        f.claimed_at = None
        f.started_at = None
        f.completed_at = None

    for field_name, delta in counter_deltas.items():
        if delta:
            setattr(batch, field_name, max(0, getattr(batch, field_name) + delta))

    should_redispatch = batch.status in (
        DocumentBatchStatus.COMPLETED, DocumentBatchStatus.COMPLETED_WITH_ERRORS, DocumentBatchStatus.FAILED,
    )
    if should_redispatch:
        batch.status = DocumentBatchStatus.QUEUED
        batch.cancel_requested = False
    await db.commit()
    await db.refresh(batch)
    await _emit_event(db, batch.id, None, "BATCH_RETRY_SELECTED_REQUESTED", message=f"{len(rows)} file(s) reset for retry.")
    if should_redispatch:
        await start_processing_for_batch(db, batch)
    return batch


# ── Crash recovery (ZB8) ─────────────────────────────────────────────────
# Same two-mechanism split as bulk_migration_service.py's own BM5 (see that
# module's crash-recovery section docstring for the full rationale):
#   1. recover_stuck_document_batches() — STARTUP-only, wired into
#      main.py's lifespan() next to recover_stuck_bulk_migration_tasks().
#      Any DocumentBatch left PROCESSING is real crash debris — the
#      previous process's _run_worker_pool() coroutine died with it, so
#      every DocumentBatchFile still marked PROCESSING for that batch is a
#      worker that never got to record an outcome.
#   2. sweep_stale_document_batch_files() — a PERIODIC APScheduler job for
#      the narrower case: the backend process is still alive, but ONE
#      worker's task hung (an OCR/AI call that never returned or raised)
#      while the rest of that batch's worker pool is healthy. Detected
#      purely by last_heartbeat age against a batch that's still
#      PROCESSING — never by trying to introspect whether a specific
#      asyncio task is still running (not reliable across a restart, and
#      not needed: claim_task()'s atomic CAS means a redundant worker-pool
#      redispatch after a stale-file fix is wasteful at worst, never
#      unsafe — two pools racing for the same file only one ever wins the
#      UPDATE).
#
# Both scoped to the platform/default tenant DB only (AsyncSessionLocal) —
# same disclosed limitation as bulk_migration_service.py's own BM5: an org
# on its own separate BYO-Database tenant engine needs its own recovery
# pass, not covered here.

_STALE_FILE_HEARTBEAT_THRESHOLD_SECONDS = 180  # 6 missed 30s heartbeat ticks (_HEARTBEAT_INTERVAL_SECONDS)


async def _recover_stuck_files_for_batch(db: AsyncSession, batch: DocumentBatch, file_ids: list[int]) -> tuple[int, int]:
    """Shared by both recovery paths below: resets the given PROCESSING
    DocumentBatchFile rows (all crash debris, or specifically the stale-
    heartbeat ones) to RETRY_PENDING (attempt budget remaining) or FAILED
    (exhausted) — the same attempt_number/backoff logic
    _execute_one_file_task's own exception handler uses, since a dead
    worker is functionally the same outcome as one that raised. Returns
    (requeued, failed)."""
    requeued = failed = 0
    for file_id in file_ids:
        file_row = await db.get(DocumentBatchFile, file_id)
        if file_row is None or file_row.status != DocumentBatchFileStatus.PROCESSING:
            continue
        if file_row.attempt_number < _MAX_FILE_RETRIES:
            backoff = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (file_row.attempt_number - 1)))
            file_row.status = DocumentBatchFileStatus.RETRY_PENDING
            file_row.retry_after = datetime.now(timezone.utc) + timedelta(seconds=backoff)
            file_row.error_code = "WORKER_CRASHED"
            file_row.error_message = "Worker heartbeat went stale (crash or hang) — requeued."
            requeued += 1
        else:
            file_row.status = DocumentBatchFileStatus.FAILED
            file_row.error_code = "WORKER_CRASHED"
            file_row.error_message = "Worker heartbeat went stale (crash or hang) and the retry budget is exhausted."
            file_row.completed_at = datetime.now(timezone.utc)
            batch.failed_files = (batch.failed_files or 0) + 1
            failed += 1
        await _emit_event(db, batch.id, file_id, f"FILE_{file_row.status.value}", message=file_row.error_message)
    return requeued, failed


async def _redispatch_or_finalize_recovered_batch(db: AsyncSession, batch: DocumentBatch, requeued: int, failed: int) -> None:
    """Shared tail of both recovery paths: honors a pending cancel/pause
    request first (same precedence as _finalize_batch_if_done()), then
    either finalizes a batch with nothing left claimable or redispatches
    a fresh worker pool for it."""
    if batch.cancel_requested:
        batch.status = DocumentBatchStatus.CANCELLED
        batch.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await _emit_event(db, batch.id, None, "BATCH_CANCELLED")
        return
    if batch.pause_requested:
        batch.status = DocumentBatchStatus.PAUSED
        await db.commit()
        await _emit_event(db, batch.id, None, "BATCH_PAUSED")
        return

    remaining = (await db.execute(
        select(func.count()).select_from(DocumentBatchFile).where(
            DocumentBatchFile.batch_id == batch.id,
            DocumentBatchFile.status.in_([
                DocumentBatchFileStatus.REGISTERED, DocumentBatchFileStatus.QUEUED,
                DocumentBatchFileStatus.PROCESSING, DocumentBatchFileStatus.RETRY_PENDING,
            ]),
        )
    )).scalar_one()
    if remaining == 0:
        batch.status = DocumentBatchStatus.COMPLETED_WITH_ERRORS if batch.failed_files > 0 else DocumentBatchStatus.COMPLETED
        batch.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await _emit_event(db, batch.id, None, f"BATCH_{batch.status.value}")
    else:
        await _emit_event(
            db, batch.id, None, "BATCH_RECOVERED",
            message=f"Backend restart interrupted this batch — {requeued} file(s) requeued, {failed} exhausted. Resuming.",
        )
        asyncio.create_task(_run_worker_pool(batch.id, batch.organization_id, batch.pipeline_type, batch.effective_workers))


async def recover_stuck_document_batches() -> dict:
    recovered_batches = requeued_files = failed_files = 0
    async with AsyncSessionLocal() as db:
        stuck_batches = (await db.execute(
            select(DocumentBatch).where(DocumentBatch.status == DocumentBatchStatus.PROCESSING)
        )).scalars().all()
        for batch in stuck_batches:
            stuck_file_ids = (await db.execute(
                select(DocumentBatchFile.id).where(
                    DocumentBatchFile.batch_id == batch.id, DocumentBatchFile.status == DocumentBatchFileStatus.PROCESSING,
                )
            )).scalars().all()
            requeued, failed = await _recover_stuck_files_for_batch(db, batch, stuck_file_ids)
            requeued_files += requeued
            failed_files += failed
            recovered_batches += 1
            await db.commit()
            await _redispatch_or_finalize_recovered_batch(db, batch, requeued, failed)
    return {"recovered_batches": recovered_batches, "requeued_files": requeued_files, "failed_files": failed_files}


async def sweep_stale_document_batch_files() -> dict:
    """Periodic (APScheduler) heartbeat sweep — see section docstring
    above. Only touches files belonging to a batch that is itself still
    PROCESSING (a hung single file inside an otherwise-healthy batch), not
    the startup-only "the whole batch is orphaned" assumption above."""
    threshold = datetime.now(timezone.utc) - timedelta(seconds=_STALE_FILE_HEARTBEAT_THRESHOLD_SECONDS)
    requeued_files = failed_files = 0
    async with AsyncSessionLocal() as db:
        stale_batch_ids = (await db.execute(
            select(DocumentBatchFile.batch_id).distinct().where(
                DocumentBatchFile.status == DocumentBatchFileStatus.PROCESSING,
                DocumentBatchFile.last_heartbeat.is_not(None),
                DocumentBatchFile.last_heartbeat < threshold,
            )
        )).scalars().all()
        for batch_id in stale_batch_ids:
            batch = await db.get(DocumentBatch, batch_id)
            if batch is None or batch.status != DocumentBatchStatus.PROCESSING:
                continue
            stale_file_ids = (await db.execute(
                select(DocumentBatchFile.id).where(
                    DocumentBatchFile.batch_id == batch_id,
                    DocumentBatchFile.status == DocumentBatchFileStatus.PROCESSING,
                    DocumentBatchFile.last_heartbeat < threshold,
                )
            )).scalars().all()
            requeued, failed = await _recover_stuck_files_for_batch(db, batch, stale_file_ids)
            requeued_files += requeued
            failed_files += failed
            await db.commit()
            if requeued:
                asyncio.create_task(_run_worker_pool(batch.id, batch.organization_id, batch.pipeline_type, batch.effective_workers))
    return {"requeued_files": requeued_files, "failed_files": failed_files}


# ── Retention / cleanup (ZB9) ─────────────────────────────────────────────
#
# Deletes on-disk extracted/ bytes for old, fully-settled batches — never
# the DocumentBatch/DocumentBatchFile DB rows, which stay forever as the
# audit trail (cheap rows, not the bulky blobs). "Fully settled" means
# BOTH a terminal batch status AND zero files still in any state someone
# might still need the original bytes for (never touching active/review/
# retry-pending files, per spec) — checked directly against the file rows
# rather than trusted from batch.status alone, because request_cancel()
# on a QUEUED/PAUSED batch marks it CANCELLED immediately without
# resetting whatever REGISTERED/RETRY_PENDING file rows it already had.
#
# Same platform-DB-only disclosed scope as recover_stuck_document_batches()
# above (see that section's docstring) — an org on its own BYO-Database
# tenant engine isn't covered by this simple sweep either.

_PROTECTED_FILE_STATUSES = (
    DocumentBatchFileStatus.REGISTERED, DocumentBatchFileStatus.QUEUED,
    DocumentBatchFileStatus.PROCESSING, DocumentBatchFileStatus.RETRY_PENDING,
    DocumentBatchFileStatus.REVIEW_REQUIRED,
)
_TERMINAL_BATCH_STATUSES = (
    DocumentBatchStatus.COMPLETED, DocumentBatchStatus.COMPLETED_WITH_ERRORS,
    DocumentBatchStatus.CANCELLED, DocumentBatchStatus.FAILED,
)


def _naive_utc(dt: datetime) -> datetime:
    """SQLite round-trips DateTime(timezone=True) columns as naive —
    normalize both sides of a Python-level comparison to naive UTC so this
    never raises "can't compare offset-naive and offset-aware datetimes"
    (same fix this project's test suite already applies for the identical
    SQLite quirk)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


async def cleanup_expired_document_batches() -> dict:
    cutoff = _naive_utc(datetime.now(timezone.utc) - timedelta(days=settings.extracted_file_retention_days))
    storage = get_batch_storage()
    purged_batches = 0
    async with AsyncSessionLocal() as db:
        candidates = (await db.execute(
            select(DocumentBatch).where(DocumentBatch.status.in_(_TERMINAL_BATCH_STATUSES))
        )).scalars().all()
        for batch in candidates:
            reference_time = batch.completed_at or batch.created_at
            if reference_time is None or _naive_utc(reference_time) >= cutoff:
                continue
            extracted_root = storage.batch_extracted_root(batch.organization_id, batch.id)
            if not extracted_root.exists():
                continue  # already cleaned up in an earlier sweep — cheap skip, no DB write needed
            protected = (await db.execute(
                select(func.count()).select_from(DocumentBatchFile).where(
                    DocumentBatchFile.batch_id == batch.id, DocumentBatchFile.status.in_(_PROTECTED_FILE_STATUSES),
                )
            )).scalar_one()
            if protected > 0:
                continue
            storage.delete_batch_extracted(batch.organization_id, batch.id)
            await _emit_event(db, batch.id, None, "BATCH_EXTRACTED_FILES_PURGED", message=f"Extracted files purged after the {settings.extracted_file_retention_days}-day retention window.")
            purged_batches += 1
    return {"purged_batches": purged_batches}
