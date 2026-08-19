"""
SqlWorkspaceExportJob — background export of a SQL Workspace query's FULL
result set, deliberately separate from the bounded, browser-side Export
CSV/JSON/Excel already on the Result Grid (see frontend's exportUtils.js,
which caps at whatever the query's row limit was — max 1000). This is
the "streaming/background export for result sets larger than the
browser-safe bound" increment that file explicitly disclosed as
not-yet-built.

Real execution, bounded memory: the executor (execution/sql_export_
executor.py) re-runs the query server-side via connector.open_sql_stream()
— the SAME bounded-memory streaming primitive Phase 3's pipeline
extraction and the reconciliation engine already use (fetchmany() chunks,
never the full result set materialized in Python memory at once) — and
writes each chunk straight to a real file on disk as it arrives. No row
cap here; a query returning 5 million rows really does write 5 million
rows to the file, chunk by chunk.

status: PENDING (row inserted, not yet picked up by the executor) →
RUNNING → SUCCEEDED | FAILED | CANCELLED. row_count is updated
periodically WHILE RUNNING (real progress, not a fake spinner) and is
final once the job reaches a terminal status.
"""
import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class SqlExportStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SqlExportFormat(str, enum.Enum):
    CSV = "CSV"
    JSON = "JSON"
    XLSX = "XLSX"
    PARQUET = "PARQUET"


class SqlWorkspaceExportJob(Base):
    __tablename__ = "sql_workspace_export_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)

    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[SqlExportFormat] = mapped_column(Enum(SqlExportFormat), nullable=False)

    status: Mapped[SqlExportStatus] = mapped_column(Enum(SqlExportStatus), default=SqlExportStatus.PENDING, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)  # live progress while RUNNING; final count once terminal
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # relative to SQL_EXPORT_BASE_PATH — see api layer's path-sandboxed download
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # scrubbed — same convention as every other persisted error in this codebase

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self) -> str:
        return f"<SqlWorkspaceExportJob {self.id} conn={self.connection_id} format={self.format} status={self.status}>"
