"""
PipelineJob / PipelineJobLog — execution tracking for a FULL saved
pipeline (source -> transform -> quality -> target, across the whole
node/edge DAG), as opposed to PipelineRun (models/pipeline_run.py) which
tracks only a single Source Dataset node's chunked extraction to a
staging file.

This is what "Run" in Pipeline Builder creates one row of, and what the
per-pipeline and global Pipeline Runs log views read from. See
execution/pipeline_job_executor.py for the real (bounded, disclosed)
execution engine that fills these in — node_stats and the log lines are
written live, node by node, so a still-RUNNING job can be tailed for a
genuine live view, not just polled for a final result.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PipelineJob(TenantBase):
    __tablename__ = "pipeline_jobs"
    # Forces SQLite's real AUTOINCREMENT (tracked in its own sqlite_sequence
    # table) instead of the default "reuse the highest deleted rowid"
    # behavior — without this, deleting a pipeline's old jobs (see
    # pipeline_service.delete_pipeline()) could let a brand-new job get
    # assigned the exact same id as a previously-deleted one, which then
    # made that old job's PipelineJobLog rows (still keyed by that numeric
    # id — see below) appear to belong to the new job. A real, observed
    # bug: a job's log view showed unrelated errors (e.g. an old SFTP
    # node failure) from a completely different, long-deleted pipeline
    # that happened to reuse its id. No-op on Postgres, which never
    # reuses sequence values anyway.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), nullable=False, index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer, nullable=False)
    pipeline_name: Mapped[str] = mapped_column(String(200), nullable=False)  # snapshot — survives a later rename

    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.PENDING, index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="Manual")

    # {node_id: {label, type, status, rows_in, rows_out, duration_ms, message}}
    # — updated after every node finishes, not just at job end.
    node_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    records_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    records_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<PipelineJob {self.id} pipeline={self.pipeline_id} status={self.status}>"


class PipelineJobLog(TenantBase):
    """Append-only log lines — one row per event, written as the executor
    goes so a running job's log can be polled for genuinely-live output
    (see api/pipeline_jobs.py GET .../logs?since_id=)."""
    __tablename__ = "pipeline_job_logs"
    __table_args__ = {"sqlite_autoincrement": True}  # see PipelineJob's __table_args__ comment above

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("pipeline_jobs.id"), nullable=False, index=True)

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(String(10), default="INFO")  # INFO | WARN | ERROR
    node_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<PipelineJobLog job={self.job_id} {self.level} {self.message[:40]!r}>"
