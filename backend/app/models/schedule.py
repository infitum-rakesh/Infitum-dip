"""
Schedule models — automates execution of existing pipelines (Document
Intelligence, OCR, AI Extraction, Transformation, Validation,
Reconciliation). See services/schedule_service.py for why execution is
simulated here rather than a second execution engine.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ScheduleFrequency(str, enum.Enum):
    ONCE = "ONCE"
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    CUSTOM_CRON = "CUSTOM_CRON"


class ScheduleEnvironment(str, enum.Enum):
    PRODUCTION = "PRODUCTION"
    STAGING = "STAGING"
    DEVELOPMENT = "DEVELOPMENT"
    TEST = "TEST"


class ScheduleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"  # a ONCE schedule that has already fired
    DISABLED = "DISABLED"


class RunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    FAILED = "FAILED"
    # Document-schedule runs only (spec section 28/81/95) — a batch where
    # some files succeeded and others didn't is neither a clean SUCCESS nor
    # a total FAILED; CANCELLED is a user-requested stop via
    # ScheduleRun.cancel_requested (cooperative — checked between files,
    # never mid-file, so a document already being processed always
    # finishes cleanly one way or another rather than being torn mid-write).
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    CANCELLED = "CANCELLED"


class Schedule(TenantBase):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # FK only, no relationship() back-populates — additive, doesn't touch
    # models/organization.py or models/user.py.
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    owner_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # References an existing pipeline from the Pipeline Builder catalogue —
    # by id/name only, no FK (Pipeline Builder has no backend model yet;
    # see schedule_service module docstring). Schedules never re-implement
    # what a pipeline does, only when it runs.
    pipeline_id: Mapped[str] = mapped_column(String(100), nullable=False)
    pipeline_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # AD12 — additive, nullable: when set, this schedule runs a real
    # AnomalyJob (via anomaly_service.run_job()) instead of the simulated
    # pipeline path — see schedule_service.execute_schedule()'s dispatch
    # branch. A real FK is safe here (unlike pipeline_id/pipeline_name
    # above): AnomalyJob is a real model in this same tenant DB, so there's
    # no "model doesn't exist yet" reason to avoid it the way pipeline_id
    # does. When anomaly_job_id is set, pipeline_id/pipeline_name are still
    # populated (service layer sets pipeline_id="0" — the same file-backed/
    # non-pipeline sentinel convention already used by Reconciliation/
    # Anomaly connection_id — and pipeline_name to the job's own name) so
    # every existing pipeline_id-keyed list/search/display path keeps
    # working unmodified for anomaly-linked schedules too.
    anomaly_job_id: Mapped[int | None] = mapped_column(ForeignKey("anomaly_jobs.id"), nullable=True, index=True)

    environment: Mapped[ScheduleEnvironment] = mapped_column(Enum(ScheduleEnvironment), default=ScheduleEnvironment.DEVELOPMENT)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")

    frequency: Mapped[ScheduleFrequency] = mapped_column(Enum(ScheduleFrequency), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # naive local wall-clock, interpreted in `timezone`
    cron_expression: Mapped[str | None] = mapped_column(String(120), nullable=True)  # required when frequency == CUSTOM_CRON

    status: Mapped[ScheduleStatus] = mapped_column(Enum(ScheduleStatus), default=ScheduleStatus.ACTIVE, index=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Advanced options
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    concurrency: Mapped[int] = mapped_column(Integer, default=1)
    notify_on_failure: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_success: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_emails: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Schedule {self.name} ({self.status})>"


class ScheduleRun(TenantBase):
    """One execution of a Schedule — the audit/history trail."""
    __tablename__ = "schedule_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"), nullable=False, index=True)

    # name= is required here (Postgres/MySQL only — SQLite ignores it and
    # just emits a CHECK constraint): this codebase has TWO independent
    # RunStatus enum classes (this one and pipeline_run.py's, different
    # member sets) — SQLAlchemy's Enum() names the native DB enum type
    # after the Python class by default, so both would otherwise collide
    # on a single "runstatus" type and whichever table's create_all() ran
    # second would fail loading the other's values. Found live during
    # Bring-Your-Own-Database Phase 6 verification against real Postgres
    # (SQLite has no native enum type, so this was invisible until then).
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, name="schedule_run_status"), default=RunStatus.RUNNING, index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="Schedule")  # Schedule | Manual | Rerun

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Integer, nullable=True)

    records_processed: Mapped[int] = mapped_column(Integer, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # [{timestamp, level, message}]
    logs: Mapped[list] = mapped_column(JSON, default=list)

    # Document-schedule runs only — cooperative cancellation flag (spec
    # section 81). Checked between files inside _execute_document_schedule()
    # (never mid-file — a file already being OCR'd/AI-extracted always
    # finishes one way or another), so remaining QUEUED files stop cleanly
    # instead of a hard kill that could leave a Document row stuck
    # mid-pipeline. POST /schedules/{id}/runs/{run_id}/cancel sets this.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    # Scheduled Document Intelligence Workflow (additive, nullable — every
    # other schedule type leaves this null). Updated incrementally in short
    # commits while the run is in progress (never one long-held write
    # transaction — see document_schedule_service.py):
    # {discovered, queued, processed, success, review, failed, skipped,
    #  unsupported, current_file, current_stage}. This is what
    # GET /schedules/{id}/runs/{run_id} polls for live progress (spec
    # section 15/16) without a second run-detail table.
    document_stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<ScheduleRun schedule={self.schedule_id} {self.status}>"
