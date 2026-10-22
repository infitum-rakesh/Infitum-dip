"""
india_verification_schedule.py — 1:1 Schedule extension (Phase Group 4,
IV21), exact TallyReconciliationScheduleConfig precedent (see
app/models/tally_schedule.py's own docstring): Schedule stays the
generic "when does this run" record; a Schedule that wants to run a
recurring India Verification sweep gets one of these rows attached via
a 1:1 nullable-origin FK (schedule_id unique) — see services/
schedule_service.py's execute_schedule() dispatch branch and
verification_job_service.create_refresh_job_for_expired().
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class IndiaVerificationScheduleConfig(TenantBase):
    __tablename__ = "india_verification_schedule_configs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"), nullable=False, unique=True, index=True)

    business_process: Mapped[str] = mapped_column(String(100), nullable=False)
    # Optional subset of check_type values to sweep; empty/None = every
    # check_type the applicable VerificationRule requires for this
    # business_process.
    check_types: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<IndiaVerificationScheduleConfig schedule={self.schedule_id} process={self.business_process}>"
