"""
Reconciliation — real source-vs-target comparison (Phase 7: Staging,
Validation & Reconciliation).

ReconciliationJob is a saved definition: a source object + a target
object (same ObjectRef shape mapping.py/validation.py already use, and
they may be on entirely different connections/vendors — the whole point
is comparing what a load run (Phase 6) actually moved), which columns
form the row key, which columns to compare value-by-value, and which
comparison types to run (COUNT, CHECKSUM, KEY_MATCH, FIELD_LEVEL).

ReconciliationRun records what a real run found. Two of the four
comparison types (KEY_MATCH, FIELD_LEVEL) need every row's key (and,
for FIELD_LEVEL, compare-column values) held in memory at once to
compute a correct diff — see reconciliation/engine.py's MAX_RECONCILE_ROWS
for the real, disclosed scale limit this implies (a hard refusal past
that size, never a silent truncation that would report a wrong count).
COUNT and CHECKSUM have no such limit — both stream in bounded chunks
regardless of object size.

Exceptions (individual missing/extra/mismatched rows) are capped at
MAX_PERSISTED_EXCEPTIONS per run for storage — the run's own
missing_count/extra_count/mismatched_count fields are always the true,
complete totals; exceptions_truncated says whether the stored sample is
the whole story or just the first slice of it.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ReconciliationRunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ExceptionType(str, enum.Enum):
    MISSING = "MISSING"    # present in source, absent from target
    EXTRA = "EXTRA"        # present in target, absent from source
    MISMATCH = "MISMATCH"  # present in both, differing compare-column value


class ReconciliationJob(TenantBase):
    __tablename__ = "reconciliation_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # connection_id=0 is a real, established sentinel in this codebase
    # (same convention custom_engine.py's AI-free extraction path uses)
    # meaning "not a real Connection row — see the matching
    # *_uploaded_file_id instead." Kept NOT NULL rather than loosening the
    # column (which would need a SQLite table rebuild, not just an ALTER
    # TABLE ADD COLUMN) — 0 is never a real Connection.id.
    #
    # Deliberately a bare int, NOT ForeignKey("connections.id") — same
    # cross-boundary-style exception the 75-FK sweep already applies
    # elsewhere in this codebase (e.g. Organization.seller_id), just for
    # a different reason: a real DB-level FK can't represent "0 means no
    # row" the way this column's own app-level convention does. A hard
    # FK constraint here isn't just redundant, it's actively wrong: a
    # real Postgres/MySQL FK rejects the value 0 outright (never a
    # Connection.id) the moment a file-backed reconciliation job's row
    # is written to a real BYO backend database (create, or a Bring-
    # Your-Own-Database migration copy) — found live during Phase 6
    # verification, invisible on SQLite since it never enforces FK
    # constraints by default.
    source_connection_id: Mapped[int] = mapped_column(nullable=False)
    source_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_object: Mapped[str] = mapped_column(String(200), nullable=False)
    source_uploaded_file_id: Mapped[int | None] = mapped_column(ForeignKey("uploaded_dataset_files.id"), nullable=True)

    target_connection_id: Mapped[int] = mapped_column(nullable=False)  # same 0-sentinel exception as source_connection_id above
    target_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_object: Mapped[str] = mapped_column(String(200), nullable=False)
    target_uploaded_file_id: Mapped[int | None] = mapped_column(ForeignKey("uploaded_dataset_files.id"), nullable=True)

    key_columns: Mapped[list] = mapped_column(JSON, default=list)
    compare_columns: Mapped[list] = mapped_column(JSON, default=list)
    comparison_types: Mapped[list] = mapped_column(JSON, default=list)  # subset of COUNT/CHECKSUM/KEY_MATCH/FIELD_LEVEL
    # {source_column: target_column} — only entries where the two sides use
    # a different real column name for the same logical field. A source
    # column with no entry here falls back to the same name on the target
    # (the pre-existing assumption); this only overrides that fallback for
    # the columns that actually need it.
    column_mapping: Mapped[dict] = mapped_column(JSON, default=dict)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ReconciliationJob {self.name} ({self.source_object} vs {self.target_object})>"


class ReconciliationRun(TenantBase):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=False, index=True)

    status: Mapped[ReconciliationRunStatus] = mapped_column(Enum(ReconciliationRunStatus), default=ReconciliationRunStatus.PENDING)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missing_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mismatched_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exceptions_truncated: Mapped[bool] = mapped_column(Boolean, default=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ReconciliationRun {self.id} job={self.job_id} status={self.status}>"


class ReconciliationException(TenantBase):
    __tablename__ = "reconciliation_exceptions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"), nullable=False, index=True)

    exception_type: Mapped[ExceptionType] = mapped_column(Enum(ExceptionType), nullable=False, index=True)
    key_value: Mapped[dict] = mapped_column(JSON, nullable=False)  # {"col": value, ...} — the row's key column(s)
    column: Mapped[str | None] = mapped_column(String(200), nullable=True)  # MISMATCH only
    source_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<ReconciliationException {self.exception_type} run={self.run_id}>"
