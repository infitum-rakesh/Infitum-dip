"""
india_verification_jobs.py — Batch/scheduled verification (Phase Group
4, IV21). Mirrors the claim_task CAS + worker-pool pattern from
bulk_migration_run.py (BulkMigrationRun/BulkMigrationTask), scaled down
to this module's much smaller volume (identifier checks against a mock/
real provider, not ETL row transfer) — no dependency levels, no
per-connection semaphore, no checkpoint/resume-mid-item state.

Two distinct trigger shapes, both producing a VerificationJobRun:
 - MANUAL: a user supplies a real list of subjects + identifiers in the
   request body (see api/compliance.py's POST /jobs) — these items run
   REAL checks through verification_orchestrator.py, exactly like an
   on-demand single check.
 - SCHEDULE: a recurring IndiaVerificationScheduleConfig fires this.
   Because no identifier value is ever persisted in the clear (see
   VerificationRequest's own docstring), a schedule CANNOT silently
   re-submit a plaintext PAN/GSTIN/etc. it never had — see
   verification_job_service.create_refresh_job_for_expired()'s own
   docstring for how this constraint shapes what a scheduled run
   actually does (an advisory sweep, not a blind auto-reverify).
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class VerificationJobTriggerType(str, enum.Enum):
    MANUAL = "MANUAL"
    SCHEDULE = "SCHEDULE"


class VerificationJobRunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class VerificationJobItemStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    # SCHEDULE-triggered items that found an expired result but have no
    # stored identifier to re-verify with — a to-do for a human, not a
    # job failure. See create_refresh_job_for_expired()'s docstring.
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class VerificationJobRun(TenantBase):
    __tablename__ = "verification_job_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)

    trigger_type: Mapped[VerificationJobTriggerType] = mapped_column(Enum(VerificationJobTriggerType), nullable=False, index=True)
    status: Mapped[VerificationJobRunStatus] = mapped_column(Enum(VerificationJobRunStatus), default=VerificationJobRunStatus.PENDING, index=True)
    business_process: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    check_types: Mapped[list] = mapped_column(JSON, default=list)  # list[str] — the check_type values this run covers
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id"), nullable=True)
    schedule_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # no FK — Schedule lives in a different model module, same loose-reference precedent as DocumentScheduleConfig

    total_items: Mapped[int] = mapped_column(Integer, default=0)
    success_items: Mapped[int] = mapped_column(Integer, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, default=0)
    needs_attention_items: Mapped[int] = mapped_column(Integer, default=0)

    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<VerificationJobRun {self.id} {self.trigger_type} {self.status}>"


class VerificationJobItem(TenantBase):
    """One subject within a job run. `identifiers` (MANUAL jobs only) is
    the per-check-type identifier dict supplied at trigger time — held
    only in memory/this transaction's processing, never re-read after
    the item completes (same "don't linger on a plaintext value longer
    than needed" discipline as VerificationRequest, even though this
    column technically persists it for the run's lifetime so a crashed-
    and-recovered worker can resume — see recover_stuck_verification_
    jobs()). SCHEDULE items never populate this column."""
    __tablename__ = "verification_job_items"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)

    run_id: Mapped[int] = mapped_column(ForeignKey("verification_job_runs.id"), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_name: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_id: Mapped[int | None] = mapped_column(nullable=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("verification_cases.id"), nullable=True, index=True)

    identifiers: Mapped[dict] = mapped_column(JSON, default=dict)  # {check_type: identifier} — MANUAL only
    check_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # SCHEDULE items are one (case, check_type) expiry each; MANUAL items cover run.check_types in full

    status: Mapped[VerificationJobItemStatus] = mapped_column(Enum(VerificationJobItemStatus), default=VerificationJobItemStatus.PENDING, index=True)
    worker_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    result_summary: Mapped[dict] = mapped_column(JSON, default=dict)  # {check_type: overall_status} after processing
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<VerificationJobItem run={self.run_id} {self.subject_name} {self.status}>"
