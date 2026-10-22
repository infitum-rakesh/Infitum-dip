"""
DocumentMappedOutput — append-only execution-result history for Document
Mapping Profiles (Document Intelligence: Dynamic JSON Extraction spec —
"snapshot the exact mapping profile version used by each execution and
preserve historical outputs").

Deliberately a NEW, lightweight table rather than another mapping-
definition/versioning system: the mapping DEFINITION and its own version
history live entirely on the EXISTING ColumnMapping/ColumnMappingVersion
tables (see models/column_mapping.py's module docstring) — this table
only records what happened each time a PUBLISHED version was actually
applied to a real Document, mirroring ColumnMappingUsage's own "append-
only, never overwritten, never deleted on its own" convention. Running
the same mapping against the same document twice produces two rows, not
one row overwritten — "preserve historical outputs" taken literally.

mapping_version is the exact ColumnMappingVersion.version_number that was
applied (never re-resolved to "whatever is published now" after the
fact) — the same pinned-version discipline
execution/mapping_execution.py::resolve_mapping_version() already
enforces for pipeline nodes, applied here to document executions too.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class DocumentMappedOutput(TenantBase):
    __tablename__ = "document_mapped_outputs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("column_mappings.id"), nullable=False, index=True)
    mapping_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_format: Mapped[str] = mapped_column(String(30), nullable=False)  # DB_TABLE | INVOICE_SCHEMA — snapshotted, not re-derived

    # Scheduled Mapping (spec section 7.4) — "every scheduled execution
    # must record mapping profile ID/version, schedule ID, run ID". Both
    # nullable: a manual "Apply Mapping Profile" run from Document
    # Details (MP10) leaves these None and trigger="MANUAL" — only a
    # scheduler-originated run (services/schedule_service.py) sets them.
    # No FK to schedules.id/schedule_runs.id — same loose-coupling
    # convention as DocumentScheduleConfig.source_connection_id; a
    # deleted schedule must never block reading this row's history.
    schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    schedule_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    trigger: Mapped[str] = mapped_column(String(10), default="MANUAL")  # MANUAL | SCHEDULED

    columns: Mapped[list] = mapped_column(JSON, default=list)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(10), default="SUCCESS", index=True)  # SUCCESS | FAILED
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_rows: Mapped[list] = mapped_column(JSON, default=list)  # [{"row": {...}, "error": "..."}] — see mapping_execution.apply_mapping_to_dataframe()

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<DocumentMappedOutput document={self.document_id} mapping={self.mapping_id}v{self.mapping_version} {self.status}>"
