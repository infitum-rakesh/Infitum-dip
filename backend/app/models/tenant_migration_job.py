"""
TenantMigrationJob / TenantMigrationJobLog — execution tracking for a
Bring-Your-Own-Database migration/cutover run (Phase 4 of the plan at
.claude/plans/inherited-inventing-harp.md). Deliberately mirrors
PipelineJob/PipelineJobLog's shape (models/pipeline_job.py) — status enum
+ append-only log table + a background asyncio task the frontend polls —
the same pattern this codebase already uses twice (Pipeline Jobs, Target
Loads), so a still-running migration can be tailed live instead of only
polled for a final result.

Lives on PlatformBase, not TenantBase — a migration job describes work
happening TO an org's TenantBase data, and must stay queryable/updatable
regardless of which physical database that data currently lives in
(mid-migration it's neither fully the old DB nor fully the new one).
Same reasoning as TenantDatabase itself.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class TenantMigrationStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROVISIONING_SCHEMA = "PROVISIONING_SCHEMA"
    COPYING = "COPYING"
    VERIFYING = "VERIFYING"
    CUTTING_OVER = "CUTTING_OVER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TenantMigrationJob(PlatformBase):
    __tablename__ = "tenant_migration_jobs"
    __table_args__ = {"sqlite_autoincrement": True}  # see PipelineJob's __table_args__ comment for why

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # NULL = the Seller/platform-level migration (Phase 6 — not wired up
    # yet, see tenant_migration_service.py module docstring).
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    tenant_database_id: Mapped[int] = mapped_column(ForeignKey("tenant_databases.id"), nullable=False, index=True)

    status: Mapped[TenantMigrationStatus] = mapped_column(Enum(TenantMigrationStatus), default=TenantMigrationStatus.PENDING, index=True)

    # Real progress, not a faked smooth percentage — updated as each
    # TenantBase table is copied/verified (see tenant_migration_service.py).
    tables_total: Mapped[int] = mapped_column(Integer, default=0)
    tables_done: Mapped[int] = mapped_column(Integer, default=0)
    current_table: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rows_copied: Mapped[int] = mapped_column(Integer, default=0)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TenantMigrationJob {self.id} org={self.organization_id} status={self.status}>"


class TenantMigrationJobLog(PlatformBase):
    """Append-only log lines — one row per event, written as the migration
    goes so a running job's log can be polled for genuinely-live output,
    same as PipelineJobLog."""
    __tablename__ = "tenant_migration_job_logs"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("tenant_migration_jobs.id"), nullable=False, index=True)

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(String(10), default="INFO")  # INFO | WARN | ERROR
    message: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<TenantMigrationJobLog job={self.job_id} {self.level} {self.message[:40]!r}>"
