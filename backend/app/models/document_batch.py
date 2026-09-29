"""
ZIP Batch Upload — models (Document Intelligence + Invoice Intelligence).

One ZIP upload = ONE reusable pipeline + MANY runtime document tasks, never
one pipeline/scheduler-job per extracted file. This module is the durable
state backing that: a chunked/resumable upload session (BatchUpload), the
batch run itself (DocumentBatch), one row per extracted file that doubles as
both a dedup-registry entry AND a claimable task (DocumentBatchFile), and an
append-only per-batch event log for live progress polling
(DocumentBatchEvent).

Everything else the feature needs (original document, OCR raw text/JSON, AI
raw response, structured/normalized data, review corrections, ERP payload)
already exists on Document (models/document.py) and ApInvoice
(models/invoice_intelligence/core.py) — reused as-is, zero new columns
there. Each claimed DocumentBatchFile task just calls
document_service.save_from_source() then process_document() (with
document_type="INVOICE" when DocumentBatch.pipeline_type is
INVOICE_INTELLIGENCE) — no second processing pipeline is built here.

DocumentBatchFile doubling as both "why was this file classified as a
duplicate/unsupported entry" AND "claim/heartbeat/attempt task state" on one
row mirrors two existing precedents that already do exactly this safely:
DocumentSourceRegistry (models/document_schedule.py — a SKIPPED/UNSUPPORTED
row simply leaves its processing columns unset) and BulkMigrationTask
(models/bulk_migration_run.py — a SKIPPED/CANCELLED row does the same for
worker_id/claimed_at). DocumentBatchFileStatus + ALLOWED_FILE_TRANSITIONS is
a direct structural mirror of BulkMigrationTaskStatus +
ALLOWED_TASK_TRANSITIONS there, and of DocumentSourceStatus +
ALLOWED_TRANSITIONS in document_schedule.py: an enforced state-machine dict
is the single choke point every status write must go through (see
document_batch_service.py::transition()).

Task claiming (document_batch_service.py::claim_task()) uses the same
portable atomic compare-and-swap UPDATE ... WHERE status IN (...) + rowcount
check as Bulk Migration's claim_task() — not SELECT ... FOR UPDATE SKIP
LOCKED — for the same reason: SQLite (this project's default tenant DB) has
no row-level locking, and the UPDATE+rowcount idiom works identically across
SQLite/Postgres/MySQL/SQL Server.

DocumentBatchEvent mirrors BulkMigrationTaskEvent/PipelineJobLog's
autoincrement-id-as-since_id-cursor pattern for the live progress drawer —
deliberately NOT the generic AuditLog table (which has no since_id consumer
anywhere and is documented as "not called from anywhere yet"). A handful of
coarse AuditLog.record_event() calls (batch uploaded/completed/failed) are
additionally made for the org-wide Audit Logs page — a legitimately
different consumer — see AUDIT_MODULES' new "Document Batch" entry.
"""
import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase
from app.models.document_storage import (
    ProcessedDataStorageFileFormat,
    ProcessedDataStorageLoadMode,
    ProcessedDataStorageType,
)


class BatchUploadStatus(str, enum.Enum):
    INITIATED = "INITIATED"
    UPLOADING = "UPLOADING"
    ASSEMBLING = "ASSEMBLING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DocumentBatchPipelineType(str, enum.Enum):
    DOCUMENT_INTELLIGENCE = "DOCUMENT_INTELLIGENCE"
    INVOICE_INTELLIGENCE = "INVOICE_INTELLIGENCE"
    # AUTO — deferred to a later phase (mixed-ZIP per-document classification)


class DocumentBatchStatus(str, enum.Enum):
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    EXTRACTING = "EXTRACTING"
    REGISTERING = "REGISTERING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DocumentBatchFileStatus(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    EXTRACTED = "EXTRACTED"
    REGISTERED = "REGISTERED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    SCANNING = "SCANNING"
    LANGUAGE_DETECTION = "LANGUAGE_DETECTION"
    OCR_PROCESSING = "OCR_PROCESSING"
    AI_EXTRACTION = "AI_EXTRACTION"
    VALIDATING = "VALIDATING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY_PENDING = "RETRY_PENDING"
    STALE = "STALE"  # heartbeat timeout — requeued by recover_stuck_document_batches()
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    UNSUPPORTED = "UNSUPPORTED"
    NESTED_ARCHIVE_NOT_SUPPORTED = "NESTED_ARCHIVE_NOT_SUPPORTED"
    CANCELLED = "CANCELLED"


# Enforced choke point — see document_batch_service.py::transition(). Direct
# structural mirror of ALLOWED_TASK_TRANSITIONS (models/bulk_migration_run.py)
# and ALLOWED_TRANSITIONS (models/document_schedule.py).
ALLOWED_FILE_TRANSITIONS: dict[DocumentBatchFileStatus, set[DocumentBatchFileStatus]] = {
    DocumentBatchFileStatus.DISCOVERED: {
        DocumentBatchFileStatus.EXTRACTED, DocumentBatchFileStatus.SKIPPED_DUPLICATE,
        DocumentBatchFileStatus.UNSUPPORTED, DocumentBatchFileStatus.NESTED_ARCHIVE_NOT_SUPPORTED,
    },
    DocumentBatchFileStatus.EXTRACTED: {DocumentBatchFileStatus.REGISTERED, DocumentBatchFileStatus.FAILED},
    DocumentBatchFileStatus.REGISTERED: {DocumentBatchFileStatus.QUEUED, DocumentBatchFileStatus.CANCELLED},
    DocumentBatchFileStatus.QUEUED: {DocumentBatchFileStatus.PROCESSING, DocumentBatchFileStatus.CANCELLED},
    DocumentBatchFileStatus.PROCESSING: {
        DocumentBatchFileStatus.SCANNING, DocumentBatchFileStatus.LANGUAGE_DETECTION,
        DocumentBatchFileStatus.OCR_PROCESSING, DocumentBatchFileStatus.AI_EXTRACTION,
        DocumentBatchFileStatus.VALIDATING, DocumentBatchFileStatus.COMPLETED,
        DocumentBatchFileStatus.REVIEW_REQUIRED, DocumentBatchFileStatus.FAILED,
        DocumentBatchFileStatus.RETRY_PENDING, DocumentBatchFileStatus.STALE,
    },
    DocumentBatchFileStatus.SCANNING: {DocumentBatchFileStatus.LANGUAGE_DETECTION, DocumentBatchFileStatus.OCR_PROCESSING, DocumentBatchFileStatus.FAILED, DocumentBatchFileStatus.STALE},
    DocumentBatchFileStatus.LANGUAGE_DETECTION: {DocumentBatchFileStatus.OCR_PROCESSING, DocumentBatchFileStatus.FAILED, DocumentBatchFileStatus.STALE},
    DocumentBatchFileStatus.OCR_PROCESSING: {DocumentBatchFileStatus.AI_EXTRACTION, DocumentBatchFileStatus.FAILED, DocumentBatchFileStatus.STALE},
    DocumentBatchFileStatus.AI_EXTRACTION: {DocumentBatchFileStatus.VALIDATING, DocumentBatchFileStatus.FAILED, DocumentBatchFileStatus.STALE},
    DocumentBatchFileStatus.VALIDATING: {
        DocumentBatchFileStatus.COMPLETED, DocumentBatchFileStatus.REVIEW_REQUIRED,
        DocumentBatchFileStatus.FAILED, DocumentBatchFileStatus.STALE,
    },
    DocumentBatchFileStatus.RETRY_PENDING: {DocumentBatchFileStatus.QUEUED, DocumentBatchFileStatus.FAILED, DocumentBatchFileStatus.CANCELLED},
    DocumentBatchFileStatus.STALE: {DocumentBatchFileStatus.QUEUED, DocumentBatchFileStatus.RETRY_PENDING, DocumentBatchFileStatus.FAILED},
    # Terminal-but-retryable — a retry (transient failure) or an explicit
    # "retry selected" re-enters the front of the claim queue.
    DocumentBatchFileStatus.COMPLETED: {DocumentBatchFileStatus.QUEUED},
    DocumentBatchFileStatus.REVIEW_REQUIRED: {DocumentBatchFileStatus.QUEUED},
    DocumentBatchFileStatus.FAILED: {DocumentBatchFileStatus.QUEUED},
    # True dead ends — never re-enter the claim queue on their own.
    DocumentBatchFileStatus.SKIPPED_DUPLICATE: set(),
    DocumentBatchFileStatus.UNSUPPORTED: set(),
    DocumentBatchFileStatus.NESTED_ARCHIVE_NOT_SUPPORTED: set(),
    DocumentBatchFileStatus.CANCELLED: set(),
}


class DuplicatePolicy(str, enum.Enum):
    SKIP_DUPLICATE = "SKIP_DUPLICATE"
    PROCESS_DUPLICATE = "PROCESS_DUPLICATE"
    CREATE_NEW_VERSION = "CREATE_NEW_VERSION"


class BatchUpload(TenantBase):
    """One row per chunked/resumable ZIP upload session. The chunk files on
    disk (see services/batch_upload_service.py's LocalBatchStorageProvider)
    are the actual durable state; received_chunks is a fast-read index of
    what's already landed so a status/resume check never has to list a
    directory."""
    __tablename__ = "batch_uploads"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # set once assembly completes
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    received_chunks: Mapped[list] = mapped_column(JSON, default=list)  # list[int] of chunk_number already written

    status: Mapped[BatchUploadStatus] = mapped_column(
        Enum(BatchUploadStatus, name="batch_upload_status"), default=BatchUploadStatus.INITIATED, index=True,
    )
    uploaded_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<BatchUpload {self.id} {self.filename!r} status={self.status}>"


class DocumentBatch(TenantBase):
    """One row per ZIP ingestion — the batch run. Every extracted file gets
    exactly one DocumentBatchFile row; there is never a per-file pipeline or
    scheduler job."""
    __tablename__ = "document_batches"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    upload_id: Mapped[int | None] = mapped_column(ForeignKey("batch_uploads.id"), nullable=True, index=True)
    pipeline_type: Mapped[DocumentBatchPipelineType] = mapped_column(
        Enum(DocumentBatchPipelineType, name="document_batch_pipeline_type"), nullable=False,
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    zip_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    duplicate_policy: Mapped[DuplicatePolicy] = mapped_column(
        Enum(DuplicatePolicy, name="document_batch_duplicate_policy"), default=DuplicatePolicy.SKIP_DUPLICATE,
    )

    total_entries: Mapped[int] = mapped_column(Integer, default=0)
    valid_files: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_files: Mapped[int] = mapped_column(Integer, default=0)
    unsupported_files: Mapped[int] = mapped_column(Integer, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, default=0)
    completed_files: Mapped[int] = mapped_column(Integer, default=0)
    review_files: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[DocumentBatchStatus] = mapped_column(
        Enum(DocumentBatchStatus, name="document_batch_status"), default=DocumentBatchStatus.UPLOADING, index=True,
    )
    effective_workers: Mapped[int] = mapped_column(Integer, default=4)
    auto_process: Mapped[bool] = mapped_column(Boolean, default=True)

    # Per-batch Processed Data Storage override (spec "PART 21 — DATA
    # STORAGE FOR ZIP PROCESSING"). All nullable, and storage_type is the
    # single on/off switch: NULL means "no override at all" — this batch's
    # documents push through document_storage_service.push_document() with
    # its ordinary org-wide config exactly as before this feature existed
    # (which is itself SQLITE-only, i.e. local DocumentProcessedDataset
    # rows, unless that org has separately opted into an EXTERNAL push).
    # A non-NULL storage_type means the user explicitly chose a destination
    # on THIS batch's upload dialog: SQLITE here means "force local-only for
    # this batch regardless of the org's own default" (an explicit opt-OUT
    # of an org-wide external push), while DATABASE/FILE require
    # storage_target_connection_id and mean "push externally using THESE
    # settings for this batch's documents, ignoring the org default's own
    # target/format". See document_storage_service.py's _effective_config().
    storage_type: Mapped[ProcessedDataStorageType | None] = mapped_column(Enum(ProcessedDataStorageType, name="document_batch_storage_type"), nullable=True)
    storage_target_connection_id: Mapped[int | None] = mapped_column(nullable=True)
    storage_target_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    storage_target_table: Mapped[str | None] = mapped_column(String(200), nullable=True)
    storage_load_mode: Mapped[ProcessedDataStorageLoadMode | None] = mapped_column(Enum(ProcessedDataStorageLoadMode, name="document_batch_storage_load_mode"), nullable=True)
    storage_file_format: Mapped[ProcessedDataStorageFileFormat | None] = mapped_column(Enum(ProcessedDataStorageFileFormat, name="document_batch_storage_file_format"), nullable=True)

    # Cooperative — checked between task claims only, mirrors
    # BulkMigrationRun.cancel_requested/pause_requested.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<DocumentBatch {self.id} {self.original_filename!r} status={self.status}>"


class DocumentBatchFile(TenantBase):
    """One row per extracted ZIP entry — doubles as the dedup-registry
    identity AND the claimable processing task. See module docstring for why
    this collapse is safe (two existing precedents already do it)."""
    __tablename__ = "document_batch_files"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("document_batches.id"), nullable=False, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)

    zip_relative_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_extension: Mapped[str | None] = mapped_column(String(20), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)

    status: Mapped[DocumentBatchFileStatus] = mapped_column(
        Enum(DocumentBatchFileStatus, name="document_batch_file_status"), default=DocumentBatchFileStatus.DISCOVERED, index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    duplicate_of_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<DocumentBatchFile {self.zip_relative_path!r} batch={self.batch_id} status={self.status}>"


class DocumentBatchEvent(TenantBase):
    """Append-only structured log — autoincrement id doubles as the since_id
    cursor for live-progress-drawer polling, mirroring
    BulkMigrationTaskEvent/PipelineJobLog's since_id pattern."""
    __tablename__ = "document_batch_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("document_batches.id"), nullable=False, index=True)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("document_batch_files.id"), nullable=True, index=True)

    event_type: Mapped[str] = mapped_column(String(60), nullable=False)  # e.g. FILE_CLAIMED, FILE_COMPLETED, BATCH_PAUSED
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<DocumentBatchEvent batch={self.batch_id} {self.event_type}>"
