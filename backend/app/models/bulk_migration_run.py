"""
Bulk Object Migration — execution-side models (Run + Tasks + Task Events).

See models/bulk_migration.py's docstring for the definition-side tables
(BulkMigrationPipeline/BulkMigrationObject) and the overall architecture.

Immutability mechanism: at run start, bulk_migration_planner_service.py
copies every relevant config field off each enabled BulkMigrationObject row
onto a new BulkMigrationTask row (object_key, schemas, load_type,
primary_key, dependency fields, batch size, etc.). A run already in flight
is therefore completely unaffected by later edits to the live
bulk_migration_objects table — no heavyweight JSON version-snapshot blob
needed, matching PipelineRun.pipeline_version's own plain-int-snapshot
convention rather than Transform Workspace's full PipelineVersion table
(see bulk_migration.py's docstring for why that distinction is deliberate).

BulkMigrationTaskStatus + ALLOWED_TASK_TRANSITIONS is a direct structural
mirror of DocumentSourceStatus + ALLOWED_TRANSITIONS in
models/document_schedule.py: an enforced state-machine dict is the single
choke point every status write must go through (see
bulk_migration_service.py::transition()), so a bad call site can never
silently corrupt task state. Terminal states PROCESSED/FAILED-equivalent
(SUCCESS/FAILED/SKIPPED) can still move back to QUEUED — SUCCESS only via
an explicit "retry successful objects too" opt-in on Resume Run, FAILED via
the ordinary Retry Failed action — the same "terminal but retryable" shape
DocumentSourceStatus uses. CANCELLED is the one true dead end (mirrors
UNSUPPORTED there): a cancelled task doesn't reopen mid-run — resuming
means starting a fresh BulkMigrationRun, which creates fresh task rows.

Task claiming (bulk_migration_service.py::claim_task()) uses a portable
atomic compare-and-swap UPDATE ... WHERE status IN (...) + rowcount check
rather than SELECT ... FOR UPDATE SKIP LOCKED, because this project's
default tenant DB is SQLite (see database/connection.py's TenantBase /
the Bring-Your-Own-Database feature for Postgres/MySQL/SQL Server as
opt-in alternatives) and SQLite has no row-level locking — the same
UPDATE+rowcount idiom works identically across all four dialects. This is
disclosed explicitly as the reason, not a silent downgrade.
"""
import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class BulkMigrationRunStatus(str, enum.Enum):
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class BulkMigrationTaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_PENDING = "RETRY_PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"   # waiting on an unmet dependency (depends_on/dependency_order)
    STALE = "STALE"        # heartbeat timeout — requeued by recover_stuck_bulk_migration_tasks()


# Enforced choke point — see bulk_migration_service.py::transition(). Mirrors
# DocumentSourceStatus.ALLOWED_TRANSITIONS in models/document_schedule.py.
ALLOWED_TASK_TRANSITIONS: dict[BulkMigrationTaskStatus, set[BulkMigrationTaskStatus]] = {
    BulkMigrationTaskStatus.PENDING: {
        BulkMigrationTaskStatus.QUEUED, BulkMigrationTaskStatus.BLOCKED,
        BulkMigrationTaskStatus.SKIPPED, BulkMigrationTaskStatus.CANCELLED,
    },
    BulkMigrationTaskStatus.BLOCKED: {
        BulkMigrationTaskStatus.QUEUED, BulkMigrationTaskStatus.SKIPPED, BulkMigrationTaskStatus.CANCELLED,
    },
    BulkMigrationTaskStatus.QUEUED: {BulkMigrationTaskStatus.RUNNING, BulkMigrationTaskStatus.CANCELLED},
    BulkMigrationTaskStatus.RUNNING: {
        BulkMigrationTaskStatus.SUCCESS, BulkMigrationTaskStatus.FAILED, BulkMigrationTaskStatus.RETRY_PENDING,
        BulkMigrationTaskStatus.CANCELLED, BulkMigrationTaskStatus.STALE,
    },
    BulkMigrationTaskStatus.RETRY_PENDING: {
        BulkMigrationTaskStatus.QUEUED, BulkMigrationTaskStatus.FAILED, BulkMigrationTaskStatus.CANCELLED,
    },
    BulkMigrationTaskStatus.STALE: {
        BulkMigrationTaskStatus.QUEUED, BulkMigrationTaskStatus.RETRY_PENDING, BulkMigrationTaskStatus.FAILED,
    },
    # Terminal-but-retryable — see module docstring.
    BulkMigrationTaskStatus.SUCCESS: {BulkMigrationTaskStatus.QUEUED},
    BulkMigrationTaskStatus.FAILED: {BulkMigrationTaskStatus.QUEUED},
    BulkMigrationTaskStatus.SKIPPED: {BulkMigrationTaskStatus.QUEUED},
    BulkMigrationTaskStatus.CANCELLED: set(),  # true dead end — resume means a fresh run, fresh task rows
}


class BulkMigrationErrorCode(str, enum.Enum):
    """Fixed, JSON-serializable error taxonomy — mirrors
    ocr/language_errors.py's LanguageErrorCode/LanguageProcessingError
    precedent (the one other fixed-taxonomy error enum in this codebase)."""
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    SOURCE_OBJECT_NOT_FOUND = "SOURCE_OBJECT_NOT_FOUND"
    TARGET_OBJECT_NOT_FOUND = "TARGET_OBJECT_NOT_FOUND"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    MAPPING_ERROR = "MAPPING_ERROR"
    EXTRACTION_ERROR = "EXTRACTION_ERROR"
    LOAD_ERROR = "LOAD_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    TIMEOUT = "TIMEOUT"
    THROTTLED = "THROTTLED"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"
    UNSUPPORTED_LOAD_TYPE = "UNSUPPORTED_LOAD_TYPE"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class BulkMigrationError(Exception):
    """Mirrors ocr/language_errors.py's LanguageProcessingError shape."""
    def __init__(self, code: BulkMigrationErrorCode, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class BulkMigrationRun(TenantBase):
    __tablename__ = "bulk_migration_runs"
    __table_args__ = {"sqlite_autoincrement": True}  # see models/bulk_migration.py's BulkMigrationPipeline __table_args__ comment

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    pipeline_id: Mapped[int] = mapped_column(ForeignKey("bulk_migration_pipelines.id"), nullable=False, index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer, nullable=False)  # snapshot of BulkMigrationPipeline.version at start
    run_key: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)  # e.g. RUN-20260908-0001

    status: Mapped[BulkMigrationRunStatus] = mapped_column(
        Enum(BulkMigrationRunStatus, name="bulk_migration_run_status"), default=BulkMigrationRunStatus.PENDING, index=True,
    )
    trigger_type: Mapped[str] = mapped_column(String(20), default="MANUAL")  # MANUAL | SCHEDULE | API

    # Cooperative — checked between task claims only, mirrors
    # ScheduleRun.cancel_requested (models/schedule.py).
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    effective_workers: Mapped[int] = mapped_column(Integer, default=8)
    effective_batch_size: Mapped[int] = mapped_column(Integer, default=5000)
    continue_on_error: Mapped[bool] = mapped_column(Boolean, default=True)

    total_objects: Mapped[int] = mapped_column(Integer, default=0)
    pending_objects: Mapped[int] = mapped_column(Integer, default=0)
    queued_objects: Mapped[int] = mapped_column(Integer, default=0)
    running_objects: Mapped[int] = mapped_column(Integer, default=0)
    success_objects: Mapped[int] = mapped_column(Integer, default=0)
    failed_objects: Mapped[int] = mapped_column(Integer, default=0)
    skipped_objects: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_objects: Mapped[int] = mapped_column(Integer, default=0)
    blocked_objects: Mapped[int] = mapped_column(Integer, default=0)

    total_rows_source: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_rejected: Mapped[int] = mapped_column(BigInteger, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    dependency_levels: Mapped[int] = mapped_column(Integer, default=1)

    # Extended/derived metrics that don't need their own columns
    # (throughput history, worker-utilization snapshots) — same
    # convention as ScheduleRun.document_stats (models/schedule.py).
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # top-level failure reason (e.g. run couldn't start)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<BulkMigrationRun {self.run_key} status={self.status}>"


class BulkMigrationTask(TenantBase):
    """One row per object per run — the runtime WORK item. Frozen copy of
    the object's config at run-start time; see module docstring."""
    __tablename__ = "bulk_migration_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "object_key", name="uq_bulk_migration_task_object"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("bulk_migration_runs.id"), nullable=False, index=True)
    object_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_migration_objects.id"), nullable=True, index=True)
    object_key: Mapped[str] = mapped_column(String(600), nullable=False, index=True)

    # Frozen config — copied from BulkMigrationObject at run-start.
    source_connection_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_object: Mapped[str] = mapped_column(String(300), nullable=False)
    target_connection_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_object: Mapped[str] = mapped_column(String(300), nullable=False)
    object_type: Mapped[str] = mapped_column(String(20), default="TABLE")
    load_type: Mapped[str] = mapped_column(String(20), default="FULL_REPLACE")
    primary_key: Mapped[list | None] = mapped_column(JSON, nullable=True)
    filter_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    watermark_column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    batch_size: Mapped[int] = mapped_column(Integer, default=5000)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    truncate_before_load: Mapped[bool] = mapped_column(Boolean, default=True)
    create_target_if_not_exists: Mapped[bool] = mapped_column(Boolean, default=True)
    drop_and_recreate: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_policy: Mapped[str] = mapped_column(String(20), default="LOG")

    dependency_group: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dependency_order: Mapped[int] = mapped_column(Integer, default=0)
    depends_on: Mapped[list | None] = mapped_column(JSON, nullable=True)
    dependency_level: Mapped[int] = mapped_column(Integer, default=0)  # computed "wave" index by the planner

    status: Mapped[BulkMigrationTaskStatus] = mapped_column(
        Enum(BulkMigrationTaskStatus, name="bulk_migration_task_status"), default=BulkMigrationTaskStatus.PENDING, index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    source_row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rows_processed: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_rejected: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_transferred: Mapped[int] = mapped_column(BigInteger, default=0)
    throughput_rows_per_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # {"last_key": ..., "batch_number": ..., "rows_processed": ...} —
    # updated after each committed chunk so a crashed/requeued task
    # resumes rather than restarting from row 0 (where the object's
    # extraction strategy has a natural resume point).
    checkpoint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<BulkMigrationTask {self.object_key!r} run={self.run_id} status={self.status}>"


class BulkMigrationTaskEvent(TenantBase):
    """Append-only structured log — autoincrement id doubles as the
    since_id cursor for log-drawer polling, mirroring PipelineJobLog's
    since_id pattern (models/pipeline_job.py)."""
    __tablename__ = "bulk_migration_task_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("bulk_migration_runs.id"), nullable=False, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_migration_tasks.id"), nullable=True, index=True)

    event_type: Mapped[str] = mapped_column(String(40), nullable=False)  # e.g. TASK_CLAIMED, TASK_RETRY, TASK_SUCCESS, RUN_PAUSED
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<BulkMigrationTaskEvent run={self.run_id} {self.event_type}>"
