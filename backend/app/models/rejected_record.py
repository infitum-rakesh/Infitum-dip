"""
RejectedRecord — real per-row capture of individual write failures (KK2),
so a user can see exactly which rows a run rejected and why, instead of
either the whole run failing on one bad row or bad rows silently
vanishing.

Populated by loading/writers/base.py's write_batch_with_fallback() (the
bulk-insert-then-row-by-row fallback — see that function's own docstring
for the mechanism and its circuit breaker) via app/services/reject_service.py,
wired into both real target-write paths:
  - execution/load_worker.py — Run Load (source_type=TARGET_LOAD,
    source_id=TargetLoadRun.id)
  - execution/pipeline_job_executor.py — Run Pipeline's dataset target
    node (source_type=PIPELINE_JOB, source_id=PipelineJob.id, node_id set)

source_type/source_id is a polymorphic association (not two nullable FKs)
since a reject can belong to either a PipelineJob or a TargetLoadRun —
two different tables — and only one applies per row.

NOT wired into SCD1/SCD2 merges (loading/scd.py) — those run their own
transaction-scoped SELECT+UPDATE+INSERT sequence that a single write_batch()
call (and this per-row fallback) can't express safely; a disclosed scope
boundary, not a silent gap.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class RejectSourceType(str, enum.Enum):
    PIPELINE_JOB = "PIPELINE_JOB"
    TARGET_LOAD = "TARGET_LOAD"


class RejectedRecord(TenantBase):
    __tablename__ = "rejected_records"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    source_type: Mapped[RejectSourceType] = mapped_column(Enum(RejectSourceType), index=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)  # PipelineJob.id or TargetLoadRun.id, per source_type
    node_id: Mapped[str | None] = mapped_column(String(100), nullable=True)  # PIPELINE_JOB only — which target node rejected it
    target_object: Mapped[str | None] = mapped_column(String(200), nullable=True)

    row_data: Mapped[dict] = mapped_column(JSON)  # the actual row that was rejected, JSON-safe (see reject_service._json_safe)
    error_message: Mapped[str] = mapped_column(Text)  # the real error from the target (scrubbed of secrets), never fabricated

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<RejectedRecord {self.id} {self.source_type}:{self.source_id} {self.error_message[:40]!r}>"
