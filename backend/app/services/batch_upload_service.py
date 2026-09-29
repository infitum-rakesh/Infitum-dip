"""
ZIP Batch Upload — chunked/resumable upload session lifecycle (ZB3).

One BatchUpload row = one chunked-upload session. The chunk files
LocalBatchStorageProvider writes to disk are the actual durable state; the
DB row's received_chunks JSON list is just a fast-read cache of that so a
status/resume check doesn't need to list a directory on every poll (it's
reconciled against the real on-disk set in get_status() below, never
trusted blindly).

Idempotency (spec section 6/38): complete_upload() computes the assembled
file's real SHA-256 by streaming it (never loaded whole into memory), and
checks for an existing COMPLETED BatchUpload in the same organization with
that same hash. If found, this upload is marked complete too but flagged as
a duplicate with a pointer back to the original — the caller
(document_batch_service.py, ZB4) uses that to decide whether to reuse the
existing DocumentBatch instead of re-extracting/re-registering/re-processing
an identical archive, per "do not blindly process duplicate ZIP files."
"""
from __future__ import annotations

import enum
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.connection import AsyncSessionLocal
from app.models.document_batch import BatchUpload, BatchUploadStatus
from app.services.batch_storage_service import get_batch_storage

settings = get_settings()

_HASH_BUFSIZE = 1024 * 1024  # 1 MB streaming reads — never load the assembled file into memory


class BatchUploadErrorCode(str, enum.Enum):
    ZIP_UPLOAD_DISABLED = "ZIP_UPLOAD_DISABLED"
    UNSUPPORTED_ARCHIVE_TYPE = "UNSUPPORTED_ARCHIVE_TYPE"
    ZIP_SECURITY_LIMIT_EXCEEDED = "ZIP_SECURITY_LIMIT_EXCEEDED"
    INVALID_CHUNK_NUMBER = "INVALID_CHUNK_NUMBER"
    UPLOAD_NOT_ACCEPTING_CHUNKS = "UPLOAD_NOT_ACCEPTING_CHUNKS"
    MISSING_CHUNKS = "MISSING_CHUNKS"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"


class BatchUploadError(Exception):
    """Mirrors zip_security_service.ZipSecurityError's shape (code +
    message + free-form details) — a fixed, JSON-serializable taxonomy the
    API layer (ZB10) maps directly to an HTTP error response."""
    def __init__(self, code: BatchUploadErrorCode, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


async def init_upload(
    db: AsyncSession, *, organization_id: int | None, filename: str,
    size_bytes: int, chunk_size: int, total_chunks: int, uploaded_by: int | None,
) -> BatchUpload:
    if not settings.zip_upload_enabled:
        raise BatchUploadError(BatchUploadErrorCode.ZIP_UPLOAD_DISABLED, "ZIP upload is disabled for this deployment.")
    if not filename.lower().endswith(".zip"):
        raise BatchUploadError(BatchUploadErrorCode.UNSUPPORTED_ARCHIVE_TYPE, f"{filename!r} does not have a .zip extension.")
    max_bytes = settings.max_zip_size_mb * 1024 * 1024
    if size_bytes > max_bytes:
        raise BatchUploadError(
            BatchUploadErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED,
            f"Declared size {size_bytes:,} bytes exceeds the configured MAX_ZIP_SIZE_MB={settings.max_zip_size_mb} ({max_bytes:,} bytes).",
        )
    if total_chunks < 1 or chunk_size < 1:
        raise BatchUploadError(BatchUploadErrorCode.INVALID_CHUNK_NUMBER, "total_chunks and chunk_size must both be positive.")

    upload = BatchUpload(
        organization_id=organization_id, filename=filename, storage_key="", size_bytes=size_bytes,
        chunk_size=chunk_size, total_chunks=total_chunks, received_chunks=[],
        status=BatchUploadStatus.INITIATED, uploaded_by=uploaded_by,
    )
    db.add(upload)
    await db.flush()  # assigns upload.id without a full commit
    upload.storage_key = f"tenants/{organization_id if organization_id is not None else 'unassigned'}/uploads/{upload.id}/source.zip"
    await db.commit()
    await db.refresh(upload)
    return upload


async def get_upload(db: AsyncSession, upload_id: int, organization_id: int | None) -> BatchUpload | None:
    """Org-scoped lookup — never returns another tenant's upload row. Same
    `organization_id is None` == "no scoping" convention as
    document_service.get_document() — see document_batch_service.get_batch()'s
    docstring for why an unconditional equality check would be wrong here."""
    stmt = select(BatchUpload).where(BatchUpload.id == upload_id)
    if organization_id is not None:
        stmt = stmt.where(BatchUpload.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def receive_chunk(db: AsyncSession, *, upload: BatchUpload, chunk_number: int, data: bytes) -> BatchUpload:
    if upload.status not in (BatchUploadStatus.INITIATED, BatchUploadStatus.UPLOADING):
        raise BatchUploadError(
            BatchUploadErrorCode.UPLOAD_NOT_ACCEPTING_CHUNKS,
            f"Upload {upload.id} is {upload.status.value}; it can no longer accept chunks.",
        )
    if not (0 <= chunk_number < upload.total_chunks):
        raise BatchUploadError(
            BatchUploadErrorCode.INVALID_CHUNK_NUMBER,
            f"Chunk number {chunk_number} is out of range for an upload with total_chunks={upload.total_chunks}.",
        )

    storage = get_batch_storage()
    storage.save_chunk(upload.organization_id, upload.id, chunk_number, data)

    received = set(upload.received_chunks or [])
    received.add(chunk_number)
    upload.received_chunks = sorted(received)
    if upload.status == BatchUploadStatus.INITIATED:
        upload.status = BatchUploadStatus.UPLOADING
    await db.commit()
    await db.refresh(upload)
    return upload


async def get_status(db: AsyncSession, upload: BatchUpload) -> dict:
    """Reconciles the DB's received_chunks cache against the real on-disk
    chunk set — never trusts the cache blindly — so a client that resumes
    after a crash gets a genuinely accurate missing-chunk list."""
    storage = get_batch_storage()
    on_disk = storage.chunk_numbers_present(upload.organization_id, upload.id)
    if on_disk != set(upload.received_chunks or []):
        upload.received_chunks = sorted(on_disk)
        await db.commit()
        await db.refresh(upload)
    missing = sorted(set(range(upload.total_chunks)) - on_disk)
    uploaded_bytes = min(len(on_disk) * upload.chunk_size, upload.size_bytes)
    return {
        "upload_id": upload.id,
        "status": upload.status.value,
        "total_chunks": upload.total_chunks,
        "received_chunks": sorted(on_disk),
        "missing_chunks": missing,
        "uploaded_bytes": uploaded_bytes,
        "total_bytes": upload.size_bytes,
        "sha256": upload.sha256,
        "error_code": upload.error_code,
        "error_message": upload.error_message,
    }


def _sha256_of_file(path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_BUFSIZE)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


async def complete_upload(
    db: AsyncSession, *, upload: BatchUpload, expected_sha256: str | None = None,
) -> tuple[BatchUpload, bool, int | None]:
    """Returns (upload, is_duplicate, duplicate_of_upload_id)."""
    if upload.status == BatchUploadStatus.COMPLETED:
        # Idempotent re-call (e.g. a retried /complete request) — return the
        # already-computed result rather than re-assembling.
        return upload, False, None
    if upload.status not in (BatchUploadStatus.INITIATED, BatchUploadStatus.UPLOADING):
        raise BatchUploadError(
            BatchUploadErrorCode.UPLOAD_NOT_ACCEPTING_CHUNKS,
            f"Upload {upload.id} is {upload.status.value}; it cannot be completed.",
        )

    storage = get_batch_storage()
    on_disk = storage.chunk_numbers_present(upload.organization_id, upload.id)
    missing = sorted(set(range(upload.total_chunks)) - on_disk)
    if missing:
        raise BatchUploadError(
            BatchUploadErrorCode.MISSING_CHUNKS,
            f"Upload {upload.id} is missing {len(missing)} chunk(s): {missing[:20]}{'...' if len(missing) > 20 else ''}.",
        )

    upload.status = BatchUploadStatus.ASSEMBLING
    await db.commit()

    assembled_path = storage.assemble(upload.organization_id, upload.id, upload.total_chunks)
    actual_size = assembled_path.stat().st_size
    computed_sha256 = _sha256_of_file(assembled_path)

    if expected_sha256 and expected_sha256.lower() != computed_sha256:
        upload.status = BatchUploadStatus.FAILED
        upload.error_code = BatchUploadErrorCode.CHECKSUM_MISMATCH.value
        upload.error_message = f"Client-declared SHA-256 {expected_sha256!r} does not match the assembled file's real SHA-256 {computed_sha256!r}."
        await db.commit()
        raise BatchUploadError(BatchUploadErrorCode.CHECKSUM_MISMATCH, upload.error_message)

    duplicate_of_upload_id = None
    existing = await db.execute(
        select(BatchUpload).where(
            BatchUpload.organization_id == upload.organization_id,
            BatchUpload.sha256 == computed_sha256,
            BatchUpload.status == BatchUploadStatus.COMPLETED,
            BatchUpload.id != upload.id,
        ).order_by(BatchUpload.id.asc())
    )
    prior = existing.scalars().first()
    if prior is not None:
        duplicate_of_upload_id = prior.id

    upload.sha256 = computed_sha256
    upload.size_bytes = actual_size
    upload.status = BatchUploadStatus.COMPLETED
    upload.completed_at = datetime.now(timezone.utc)
    storage.delete_chunks(upload.organization_id, upload.id)  # now-redundant parts — the assembled file is the real artifact
    await db.commit()
    await db.refresh(upload)
    return upload, duplicate_of_upload_id is not None, duplicate_of_upload_id


async def cancel_upload(db: AsyncSession, upload: BatchUpload) -> BatchUpload:
    if upload.status in (BatchUploadStatus.COMPLETED, BatchUploadStatus.CANCELLED):
        return upload
    storage = get_batch_storage()
    storage.delete_upload(upload.organization_id, upload.id)
    upload.status = BatchUploadStatus.CANCELLED
    await db.commit()
    await db.refresh(upload)
    return upload


# ── Retention / cleanup (ZB9) ────────────────────────────────────────────


async def cleanup_expired_uploads() -> dict:
    """Two distinct cleanups, both scoped by settings.zip_retention_days —
    same platform-DB-only disclosed scope as document_batch_service's own
    ZB8/ZB9 sweeps (see that module):
      1. COMPLETED uploads older than the retention window — the raw
         assembled source.zip has already been fully extracted
         (document_batch_service.create_batch_from_upload, ZB4) and isn't
         needed for anything else; only the on-disk blob is deleted, the
         BatchUpload row (filename/sha256/who/when) stays forever as the
         audit trail, and idempotent-reupload detection (complete_upload's
         own sha256 lookup) keeps working off that row alone.
      2. Uploads that never completed (INITIATED/UPLOADING/ASSEMBLING) and
         are older than the same window are abandoned — no legitimate
         chunked upload takes anywhere near that long to finish. Marked
         FAILED so they stop looking like a phantom in-progress upload,
         and their partial working directory (chunks + any partial
         assembled file) is deleted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.zip_retention_days)
    storage = get_batch_storage()
    purged_completed_zips = marked_abandoned = 0
    async with AsyncSessionLocal() as db:
        completed = (await db.execute(
            select(BatchUpload).where(BatchUpload.status == BatchUploadStatus.COMPLETED, BatchUpload.completed_at < cutoff)
        )).scalars().all()
        for upload in completed:
            path = storage.assembled_path(upload.organization_id, upload.id)
            if path.exists():
                path.unlink(missing_ok=True)
                purged_completed_zips += 1

        abandoned = (await db.execute(
            select(BatchUpload).where(
                BatchUpload.status.in_([BatchUploadStatus.INITIATED, BatchUploadStatus.UPLOADING, BatchUploadStatus.ASSEMBLING]),
                BatchUpload.created_at < cutoff,
            )
        )).scalars().all()
        for upload in abandoned:
            storage.delete_upload(upload.organization_id, upload.id)
            upload.status = BatchUploadStatus.FAILED
            upload.error_code = "UPLOAD_ABANDONED"
            upload.error_message = (
                f"Upload was never completed within the {settings.zip_retention_days}-day retention window and has been cleaned up."
            )
            marked_abandoned += 1
        await db.commit()
    return {"purged_completed_zips": purged_completed_zips, "marked_abandoned": marked_abandoned}
