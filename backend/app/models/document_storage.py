"""
ProcessedDataStorageConfig — configurable destination for PROCESSED
document output (OCR text, AI extraction fields, structured dataset, raw
AI response), separate from and additive to the existing Bring-Your-Own-
Database feature (models/tenant_database.py).

BYO-Database migrates an org's ENTIRE tenant-scoped schema (every
TenantBase table — documents, pipelines, users, reconciliation jobs,
...) to one external Postgres/MySQL instance, all-or-nothing per org
(see tenant_engine_registry.py/tenant_migration_service.py). That is a
different concept from what this model configures: by default (mode=
DEFAULT) a document's processed output lives nowhere but its own
Document row's JSON columns, in whatever DB the org's tenant schema
already lives in (SQLite, or a BYO-migrated Postgres/MySQL — this model
doesn't care which). Setting mode=EXTERNAL additionally pushes a
flattened copy of that same processed output to a separately-configured
Connection after each document finishes processing — reusing the SAME
Connection/connector/BulkWriter abstraction the rest of this codebase
already uses for real ETL (Source Dataset, Run Load, Pipeline targets),
which is what makes Postgres/SQL Server/Oracle/MySQL/Snowflake all work
here for free (see loading/writers/registry.py — every one of those five
already has a real, registered BulkWriter) plus a local-file destination
via a small addition to LocalFileConnector. See services/
document_storage_service.py for the actual push logic.

One row per organization (organization_id unique, like
OrganizationSettings) — get_or_create()'d lazily by the service, exactly
like OrganizationSettings/models/settings.py.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ProcessedDataStorageMode(str, enum.Enum):
    DEFAULT = "DEFAULT"    # processed data lives only on the Document row itself
    EXTERNAL = "EXTERNAL"  # ALSO pushed to target_connection_id after each document finishes


class ProcessedDataStorageType(str, enum.Enum):
    """Explicit "Storage Type" selector (spec item #5). SQLITE is not a
    real target_connection_id at all — it's what mode=DEFAULT already
    means (this platform's own tenant database, SQLite unless BYO-
    Database has migrated the org elsewhere — see this module's own
    docstring). DATABASE/FILE both require mode=EXTERNAL + a matching
    target_connection_id, and pick which of load_mode/file_format
    actually applies."""
    SQLITE = "SQLITE"
    DATABASE = "DATABASE"
    FILE = "FILE"


class ProcessedDataStorageLoadMode(str, enum.Enum):
    """How each document's push interacts with the target TABLE (DATABASE
    storage_type only — a FILE target has no equivalent concept beyond
    "append", which append_csv_row()/append_jsonl()/write_parquet_record()
    already do unconditionally).
    CREATE_IF_NOT_EXISTS — today's original behavior: ensure_table() once,
        then upsert-by-document_id on every push (safe to run forever).
    APPEND — always INSERT, never upsert; a document reprocessed twice
        lands as two rows (the caller's own choice to make history, not
        overwrite it).
    TRUNCATE_AND_LOAD — the table is truncated exactly ONCE, the next time
        a document pushes after this mode is (re)selected, then behaves
        like CREATE_IF_NOT_EXISTS from then on (see pending_reset).
    DROP_AND_RECREATE — same one-time-then-settle behavior, but drops and
        lets ensure_table() recreate the table instead of truncating it
        (picks up a schema change in the record shape)."""
    CREATE_IF_NOT_EXISTS = "CREATE_IF_NOT_EXISTS"
    APPEND = "APPEND"
    TRUNCATE_AND_LOAD = "TRUNCATE_AND_LOAD"
    DROP_AND_RECREATE = "DROP_AND_RECREATE"


class ProcessedDataStorageFileFormat(str, enum.Enum):
    """FILE storage_type's format — JSONL was the only option before this
    was added; see document_storage_service.py's _push_to_file(). XML
    (Part 21 of the ZIP-processing-storage spec, which lists "JSON / CSV /
    PARQUET / XML / Database" as the required format set) writes one
    file per document under target_table, the same directory-of-files
    layout write_parquet_record() already uses — genuinely appending a
    single record into one growing XML file isn't practical without
    keeping the file's root element open across pushes (this connector is
    constructed fresh per push), so this is a disclosed structural choice,
    not a fake "append"."""
    JSONL = "JSONL"
    CSV = "CSV"
    PARQUET = "PARQUET"
    XML = "XML"


class ProcessedDataStoragePushStatus(str, enum.Enum):
    NEVER_RUN = "NEVER_RUN"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class DocumentDatasetType(str, enum.Enum):
    """The six logical datasets spec item #5 asks to be stored SEPARATELY
    (never overwriting raw with transformed) — DocumentProcessedDataset
    below gives each type its own row per document (UNIQUE(document_id,
    dataset_type)), so writing STRUCTURED_DATA never touches OCR's row
    and vice versa. Deliberately one table with a discriminator column
    rather than six near-identical physical tables (documents/
    document_ocr/document_ai_extraction/document_structured_data/
    document_validation/document_processing_runs, as the spec's own
    "recommended logical structure" names them) — the spec explicitly
    permits adapting rather than blindly creating those exact tables, and
    a single table keeps queries/migrations/tests simple while preserving
    the real separation the requirement actually cares about."""
    METADATA = "METADATA"
    OCR = "OCR"
    AI_EXTRACTION = "AI_EXTRACTION"
    STRUCTURED_DATA = "STRUCTURED_DATA"
    VALIDATION = "VALIDATION"
    PROCESSING_RUN = "PROCESSING_RUN"


class ProcessedDataStorageConfig(TenantBase):
    __tablename__ = "processed_data_storage_configs"
    __table_args__ = (UniqueConstraint("organization_id", name="uq_processed_data_storage_org"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    mode: Mapped[ProcessedDataStorageMode] = mapped_column(Enum(ProcessedDataStorageMode), default=ProcessedDataStorageMode.DEFAULT)
    # Only meaningful when mode=EXTERNAL — DATABASE vs FILE governs which
    # of load_mode/file_format below actually applies, and which
    # connection types update_config() accepts as target_connection_id.
    # Kept independent of `mode` rather than folded into it (e.g. a 3-way
    # SQLITE/DATABASE/FILE enum on `mode` itself) purely for backward
    # compatibility with the original DEFAULT/EXTERNAL contract every
    # existing caller (document_service.py, tests) already depends on.
    storage_type: Mapped[ProcessedDataStorageType] = mapped_column(Enum(ProcessedDataStorageType), default=ProcessedDataStorageType.DATABASE)
    load_mode: Mapped[ProcessedDataStorageLoadMode] = mapped_column(Enum(ProcessedDataStorageLoadMode), default=ProcessedDataStorageLoadMode.CREATE_IF_NOT_EXISTS)
    file_format: Mapped[ProcessedDataStorageFileFormat] = mapped_column(Enum(ProcessedDataStorageFileFormat), default=ProcessedDataStorageFileFormat.JSONL)
    # Set True whenever load_mode is (re)saved as TRUNCATE_AND_LOAD/
    # DROP_AND_RECREATE (or the target table/connection changes under a
    # destructive mode) — the NEXT document push performs the one-time
    # truncate/drop, then clears this flag, so a destructive mode never
    # re-fires on every subsequent document (which would delete every
    # previously-pushed row each time). See _apply_load_mode_reset().
    pending_reset: Mapped[bool] = mapped_column(Boolean, default=False)

    # Deliberately a bare int, not ForeignKey("connections.id") with
    # ondelete=CASCADE — a target Connection getting deleted should
    # surface as a real, visible push failure (last_status=FAILED,
    # last_error naming the missing connection) the next time a document
    # finishes processing, not silently revert this config to DEFAULT.
    target_connection_id: Mapped[int | None] = mapped_column(nullable=True)
    target_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_table: Mapped[str] = mapped_column(String(200), default="dip_processed_documents")

    # Every flag defaults to what a first-time EXTERNAL setup almost
    # certainly wants: the OCR text/extraction fields/dataset are exactly
    # what "processed data" means to a caller of this feature.
    # ai_raw_response defaults OFF — it's the verbatim raw AI model
    # payload (can be large/verbose and vendor-shaped), an opt-in extra
    # rather than something every push should carry.
    include_ocr_text: Mapped[bool] = mapped_column(Boolean, default=True)
    include_extraction_fields: Mapped[bool] = mapped_column(Boolean, default=True)
    include_dataset: Mapped[bool] = mapped_column(Boolean, default=True)
    include_ai_raw_response: Mapped[bool] = mapped_column(Boolean, default=False)

    last_pushed_document_id: Mapped[int | None] = mapped_column(nullable=True)
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[ProcessedDataStoragePushStatus] = mapped_column(Enum(ProcessedDataStoragePushStatus), default=ProcessedDataStoragePushStatus.NEVER_RUN)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ProcessedDataStorageConfig org={self.organization_id} mode={self.mode}>"


class DocumentProcessedDataset(TenantBase):
    """One row per (document, dataset_type) — the always-on, zero-config
    "storage_type=SQLITE" path: this platform's own tenant database
    (SQLite by default) already normalizes a document's processed output
    into these separately-addressable rows the moment processing finishes,
    with NO external Connection required. See document_storage_service.py's
    store_local_datasets(), called unconditionally from push_document()
    regardless of `mode`/`storage_type` — EXTERNAL push (to a real
    Postgres/MySQL/SQL Server/Oracle/Snowflake/file destination) is an
    additive, opt-in second step on top of this, not a replacement for it.

    Each dataset_type's row is written ONLY by the step that owns that
    type (OCR only touched by the OCR-finished step, STRUCTURED_DATA only
    by the structuring step, ...) — writing one type's row never reads,
    merges into, or overwrites another type's row, which is what actually
    satisfies "do NOT overwrite raw OCR with transformed data": they are
    different ROWS, not different keys inside one shared JSON blob."""
    __tablename__ = "document_processed_datasets"
    __table_args__ = (UniqueConstraint("document_id", "dataset_type", name="uq_document_processed_dataset"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    dataset_type: Mapped[DocumentDatasetType] = mapped_column(Enum(DocumentDatasetType), nullable=False, index=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<DocumentProcessedDataset document={self.document_id} type={self.dataset_type}>"
