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

from app.database.connection import Base


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


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # FK only, no relationship() back-populates — additive, doesn't touch
    # models/organization.py or models/user.py.
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # References an existing pipeline from the Pipeline Builder catalogue —
    # by id/name only, no FK (Pipeline Builder has no backend model yet;
    # see schedule_service module docstring). Schedules never re-implement
    # what a pipeline does, only when it runs.
    pipeline_id: Mapped[str] = mapped_column(String(100), nullable=False)
    pipeline_name: Mapped[str] = mapped_column(String(200), nullable=False)

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


class ScheduleRun(Base):
    """One execution of a Schedule — the audit/history trail."""
    __tablename__ = "schedule_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"), nullable=False, index=True)

    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.RUNNING, index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="Schedule")  # Schedule | Manual | Rerun

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Integer, nullable=True)

    records_processed: Mapped[int] = mapped_column(Integer, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # [{timestamp, level, message}]
    logs: Mapped[list] = mapped_column(JSON, default=list)

    def __repr__(self) -> str:
        return f"<ScheduleRun schedule={self.schedule_id} {self.status}>"
