"""
Scheduled Document Intelligence Workflow — models.

Additive-only extension of the existing Schedules module
(models/schedule.py). Mirrors the AD12 precedent exactly: Schedule stays
the generic "when does this run" record; a Schedule that wants to run a
real document-discovery-and-processing job (rather than the simulated
pipeline path, or AD12's real AnomalyJob dispatch) gets one of these
DocumentScheduleConfig rows attached via a 1:1 nullable FK — see
services/schedule_service.py's execute_schedule() dispatch branch. Every
other schedule type is completely unaffected.

DocumentSourceRegistry is the idempotency ledger required by the
"scheduler must never reprocess an already-processed file" rule — see its
own docstring below. It is intentionally NOT the same identity model as
Document.content_hash (that one deliberately allows repeat uploads of
identical bytes — see document_service.save_upload()'s docstring); this
one is the durable dedup key keyed off the SOURCE location, exactly as
spec section 7 describes.
"""
import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class DocumentSourceStatus(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    OCR_PROCESSING = "OCR_PROCESSING"
    AI_PROCESSING = "AI_PROCESSING"
    VALIDATING = "VALIDATING"
    PROCESSED = "PROCESSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    UNSUPPORTED = "UNSUPPORTED"


# Transitions a document's status is allowed to make — enforced by
# document_schedule_service.transition() so a bad call site can never
# silently corrupt the state machine (spec section 94).
ALLOWED_TRANSITIONS: dict[DocumentSourceStatus, set[DocumentSourceStatus]] = {
    DocumentSourceStatus.DISCOVERED: {DocumentSourceStatus.QUEUED, DocumentSourceStatus.SKIPPED, DocumentSourceStatus.UNSUPPORTED},
    DocumentSourceStatus.QUEUED: {DocumentSourceStatus.PROCESSING, DocumentSourceStatus.FAILED},
    DocumentSourceStatus.PROCESSING: {
        DocumentSourceStatus.OCR_PROCESSING, DocumentSourceStatus.AI_PROCESSING, DocumentSourceStatus.VALIDATING,
        DocumentSourceStatus.PROCESSED, DocumentSourceStatus.REVIEW_REQUIRED, DocumentSourceStatus.FAILED,
    },
    DocumentSourceStatus.OCR_PROCESSING: {DocumentSourceStatus.AI_PROCESSING, DocumentSourceStatus.FAILED},
    DocumentSourceStatus.AI_PROCESSING: {DocumentSourceStatus.VALIDATING, DocumentSourceStatus.FAILED},
    DocumentSourceStatus.VALIDATING: {DocumentSourceStatus.PROCESSED, DocumentSourceStatus.REVIEW_REQUIRED, DocumentSourceStatus.FAILED},
    # Terminal states can still move to QUEUED — a retry (transient
    # failure) or a changed-content re-discovery both re-enter the front
    # of the pipeline for what is, from the registry's point of view, the
    # same (connection, path, filename) identity.
    DocumentSourceStatus.PROCESSED: {DocumentSourceStatus.QUEUED},
    DocumentSourceStatus.REVIEW_REQUIRED: {DocumentSourceStatus.QUEUED},
    DocumentSourceStatus.FAILED: {DocumentSourceStatus.QUEUED},
    DocumentSourceStatus.SKIPPED: {DocumentSourceStatus.QUEUED},
    DocumentSourceStatus.UNSUPPORTED: set(),
}


class DocumentScheduleConfig(TenantBase):
    """1:1 with Schedule (schedule.id) — the document-source-specific
    fields that don't belong on the generic Schedule row. Presence of a
    row here (schedule.document_config relationship-free lookup by
    schedule_id) is what makes execute_schedule() take the real
    document-discovery dispatch branch instead of the simulated one."""
    __tablename__ = "document_schedule_configs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"), nullable=False, unique=True, index=True)

    # No FK to connections.id — Connection is a real, already-tenant-scoped
    # model but this file intentionally stays loosely coupled the same way
    # Schedule.pipeline_id/pipeline_name reference the Pipeline Builder
    # catalogue by id only, so a connection being deleted later is a clean
    # "connection not found" runtime error, not a migration-blocking FK.
    source_connection_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # PAY18/PAY20 — non-file intake sources. "FILE" (default) is every
    # schedule created before this existed: source_connection_id points at
    # a STORAGE connection and source_path/file_pattern/recursive_scan
    # drive discover_files() exactly as before, completely unaffected.
    # "DATABASE" instead points source_connection_id at a real
    # DATABASE-category connection (Postgres/MySQL/SQL Server/...) and
    # runs source_query via that connector's own preview_sql() (see
    # document_schedule_service.discover_database_records()). "API"
    # (PAY20) points source_connection_id at a real API-category
    # connection (REST) and runs the source_api_* fields below through
    # that connector's own fetch_api() — the same real, paginated HTTP
    # fetch the SQL Workspace/Source Dataset "preview-api" surface already
    # uses (see document_schedule_service.discover_api_records()). Either
    # non-file kind's returned rows become one Document each, mapped
    # directly onto the pinned document_type's canonical taxonomy field
    # names with NO OCR/AI Extraction stage (the row is already
    # structured; see document_service.process_document()'s
    # preextracted_fields param). source_path stays required for every
    # kind and is reused as a free-text label (e.g. "AP invoices table" /
    # "Vendor invoices API") shown wherever a file schedule would show its
    # path — not a filesystem path for either non-file kind.
    source_kind: Mapped[str] = mapped_column(String(20), default="FILE")
    source_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Column in the source row (a DATABASE query's result set, or one
    # decoded API record) that uniquely identifies it (e.g. an invoice
    # number or primary key) — used to build the DocumentSourceRegistry
    # per-record dedup identity (file_name column, reused unchanged) the
    # same way a file's own name does. Shared by DATABASE and API kinds.
    # Optional: a source with no natural key still gets a stable identity
    # from a hash of the row's own content (see document_schedule_service.
    # record_identity()), it just can't tell "this row changed" apart
    # from "this is a genuinely different row" as precisely.
    source_record_key_column: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # PAY20 — API-kind fields (all None/default for FILE/DATABASE kinds).
    # Mirrors connectors/base.py's fetch_api() kwargs directly rather than
    # inventing a parallel shape — see that method's docstring for the
    # pagination dict's contract (style/offset/page/cursor/token) and
    # json_path's dot-path envelope-extraction syntax.
    source_api_endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_api_method: Mapped[str] = mapped_column(String(10), default="GET")
    source_api_query_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_api_pagination: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_api_json_path: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # PAY21 — ERP-kind's one config field: source_connection_id already
    # points at the ORACLE_ERP_CLOUD connection to pull from (see
    # OracleErpConnector.list_invoices()), so there's no endpoint/method
    # to configure the way API-kind needs — Oracle's own documented
    # Invoices collection resource is implicit. This is the only knob:
    # an optional Oracle REST query-language filter string (e.g.
    # "ValidationStatus='NEEDS_REVALIDATION'"), passed through verbatim as
    # list_invoices()'s `q` param. Blank means "no filter — pull every
    # invoice the connection's credentials can see".
    source_erp_query: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_pattern: Mapped[str] = mapped_column(String(200), default="*")

    # Pins this schedule's discovered files to a known document_taxonomies
    # type (e.g. "INVOICE") so scheduled intake runs the real taxonomy
    # instead of ad-hoc/auto detection — see services/invoice/ (INV11).
    # Nullable/free-text on purpose: schedules that don't set this keep
    # today's auto-detect behavior unchanged.
    document_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    recursive_scan: Mapped[bool] = mapped_column(Boolean, default=False)

    # Scheduled Mapping (Document Intelligence: Dynamic JSON Extraction
    # spec, section 7) — the EXPLICIT mapping profile this schedule uses,
    # highest-precedence per section 7.2's recommended order (explicit
    # schedule mapping -> compatible document-type default -> no mapping,
    # preserve original JSON). No FK to column_mappings.id, same loose-
    # coupling convention as source_connection_id/invoice_erp_connection_id
    # above — a deleted/retired mapping becomes a clear, reportable
    # per-document mapping-stage failure at execution time (see
    # services/document_mapping_resolution_service.py), never a migration-
    # blocking FK or a silently-substituted unrelated profile. Nullable:
    # a schedule with this unset falls through to the org's document-type
    # default, if any, per the same precedence.
    mapping_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Scheduler integration (INV11) — lets an invoice-typed schedule post a
    # STRAIGHT_THROUGH decision straight to the ERP with zero human
    # involvement, the same way a person clicking "Post to ERP" in the
    # Invoice Inbox would (reuses invoice_erp_service.post_invoice()
    # unmodified — see document_service.process_document()'s schedule
    # branch). No FK to connections.id, same loose-coupling convention as
    # source_connection_id above. Only ever consulted when document_type
    # == "INVOICE"; a REVIEW_REQUIRED/REJECT/RETRY decision is completely
    # unaffected regardless of this flag — auto-post only ever fires on
    # the highest-confidence outcome, never as a way to skip review.
    invoice_auto_post: Mapped[bool] = mapped_column(Boolean, default=False)
    invoice_erp_connection_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    batch_size: Mapped[int] = mapped_column(Integer, default=500)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=3)

    confidence_threshold: Mapped[float] = mapped_column(Float, default=90.0)
    accuracy_threshold: Mapped[float] = mapped_column(Float, default=90.0)

    archive_after_success: Mapped[bool] = mapped_column(Boolean, default=False)
    archive_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    move_failed_to_review_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # SKIP_DUPLICATE (default, idempotent/safe — spec section 78) or
    # PROCESS_AS_SEPARATE_DOCUMENT.
    duplicate_content_behavior: Mapped[str] = mapped_column(String(30), default="SKIP_DUPLICATE")

    max_file_size_mb: Mapped[int] = mapped_column(Integer, default=100)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<DocumentScheduleConfig schedule={self.schedule_id} path={self.source_path!r}>"


class DocumentSourceRegistry(TenantBase):
    """Durable per-file idempotency ledger — spec section 7/8. Identity is
    (organization_id, source_connection_id, source_path, file_name,
    content_hash), NOT schedule_id: two different schedules pointed at the
    same connection+path must still share dedup state, matching the spec's
    suggested unique key exactly. A file whose bytes change gets a new
    content_hash and therefore a new registry row (treated as a new
    version, per section 7) while the old row's history is preserved.

    UNIQUE constraint is the actual idempotency guarantee (spec section 8:
    "do not rely only on application-level checks") — a crashed run that
    restarts and re-discovers the same file hits this constraint instead
    of creating a duplicate row; discovery code upserts on conflict.
    """
    __tablename__ = "document_source_registry"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "source_connection_id", "source_path", "file_name", "content_hash",
            name="uq_document_source_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedules.id"), nullable=True, index=True)
    source_connection_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)  # connection_type at discovery time, informational

    source_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    modified_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    first_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[DocumentSourceStatus] = mapped_column(
        Enum(DocumentSourceStatus, name="document_source_status"), default=DocumentSourceStatus.DISCOVERED, index=True,
    )
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The ScheduleRun during which this row most recently moved — lets a
    # run detail screen answer "which files did THIS run touch" (spec
    # section 15/44) without duplicating file metadata onto ScheduleRun.
    processing_run_id: Mapped[int | None] = mapped_column(ForeignKey("schedule_runs.id"), nullable=True, index=True)

    # Set once a real Document row exists for this file (after
    # save_from_source() — see document_service.py). Nullable because a
    # SKIPPED/UNSUPPORTED/duplicate-content file never gets one.
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<DocumentSourceRegistry {self.file_name} ({self.status})>"
