"""
TransformationJob — Data Format Transformation Engine (Phase 4): converts
data from one format to another (JSON, TABLE, CSV, Excel, XML, Parquet)
in ANY direction, through the same canonical read/write adapters every
other real ETL feature in this codebase already uses, rather than a
separate hand-written converter per format pair:

  - SOURCE: either an already-uploaded file (source_uploaded_file_id —
    same UploadedDatasetFile Reconciliation/Validation/Anomaly already
    use) read via reconciliation/engine.py's FileRowSource, or a real
    database TABLE (source_connection_id/schema/object) read via that
    same module's ConnectorRowSource — both already implement the one
    shared RowSource interface (get_columns()/iter_rows()), so reading
    needs no format-specific code here at all.
  - TARGET: either a downloadable file (target_format — CSV/JSON/XLSX/
    XML/PARQUET, written via services/sql_export_writers.py's
    ChunkWriter classes — the same real, chunked, disk-backed writers
    SQL Workspace's background export already uses) or a real database
    TABLE (target_connection_id/schema/object, written via
    loading/writers/'s BulkWriter — ensure_table + write_batch_with_
    fallback, the same pattern services/document_storage_service.py's
    Processed Data Storage push already established in Phase 3).

See execution/transformation_executor.py for the actual read-loop/write-
loop; this file (and services/transformation_service.py) only define the
job record and its CRUD, same split as SqlWorkspaceExportJob/
sql_export_executor.py.

status: PENDING -> RUNNING -> SUCCEEDED | FAILED | CANCELLED. row_count is
real, live progress while RUNNING (updated every chunk), never a faked
"in progress" placeholder.
"""
import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class TransformFormat(str, enum.Enum):
    CSV = "CSV"
    JSON = "JSON"
    # Value is "XLSX", not "EXCEL" — must match services/sql_export_writers.py's
    # _WRITERS/_EXTENSIONS keys exactly, since target_format.value is passed
    # straight into make_writer() unchanged (real bug found live: a job
    # created with "EXCEL" failed with "Unknown export format: EXCEL").
    EXCEL = "XLSX"
    XML = "XML"
    PARQUET = "PARQUET"
    TABLE = "TABLE"


class TransformEndpointKind(str, enum.Enum):
    UPLOADED_FILE = "UPLOADED_FILE"  # source only
    DOWNLOAD_FILE = "DOWNLOAD_FILE"  # target only — a new file this job itself produces
    CONNECTION = "CONNECTION"        # source or target — a real DB connection + schema + object (TABLE format)


class TransformationJobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TransformationJob(TenantBase):
    __tablename__ = "transformation_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    source_kind: Mapped[TransformEndpointKind] = mapped_column(Enum(TransformEndpointKind), nullable=False)
    source_format: Mapped[TransformFormat] = mapped_column(Enum(TransformFormat), nullable=False)
    # Only one of these two is set, matching source_kind.
    source_uploaded_file_id: Mapped[int | None] = mapped_column(nullable=True)
    source_connection_id: Mapped[int | None] = mapped_column(nullable=True)
    source_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_object: Mapped[str | None] = mapped_column(String(200), nullable=True)

    target_kind: Mapped[TransformEndpointKind] = mapped_column(Enum(TransformEndpointKind), nullable=False)
    target_format: Mapped[TransformFormat] = mapped_column(Enum(TransformFormat), nullable=False)
    # Only set when target_kind == CONNECTION (a real TABLE write).
    target_connection_id: Mapped[int | None] = mapped_column(nullable=True)
    target_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_object: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Target-format writer options (spec item #8's "CSV/Excel/XML
    # Configuration Options") — passed straight through to
    # sql_export_writers.make_writer(); keys are format-specific and
    # unrecognized/inapplicable keys for the chosen target_format are
    # ignored rather than rejected (see make_writer()'s own docstring):
    #   CSV: delimiter, quotechar, encoding
    #   XLSX: sheet_name
    #   XML: root_element, row_element
    # None/empty means every current default (comma-delimited, "Result"
    # sheet, <rows>/<row> tags) — unchanged from before this field existed.
    format_options: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[TransformationJobStatus] = mapped_column(Enum(TransformationJobStatus), default=TransformationJobStatus.PENDING, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    # Only set when target_kind == DOWNLOAD_FILE and the job succeeded —
    # relative to TRANSFORM_EXPORT_BASE_PATH, same path-sandboxed download
    # convention as sql_export_service.resolve_download_path().
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # scrubbed of secrets, same convention as every other persisted error here

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self) -> str:
        return f"<TransformationJob {self.id} {self.source_format}->{self.target_format} status={self.status}>"
