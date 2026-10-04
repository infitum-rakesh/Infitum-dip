"""
TALLY-J — Scheduled/batch Tally reconciliation.

Additive-only extension of the existing Schedules module (models/schedule.py),
following the exact AD12/Scheduled-Document-Intelligence precedent: Schedule
stays the generic "when does this run" record; a Schedule that wants to run
a real Tally extraction+reconciliation job gets one of these
TallyReconciliationScheduleConfig rows attached via a 1:1 nullable FK — see
services/schedule_service.py's execute_schedule() dispatch branch. Every
other schedule type (Pipeline Builder's simulated path, AD12's Anomaly
Job, the Scheduled Document Intelligence Workflow) is completely
unaffected.

Unlike DocumentScheduleConfig's deliberately loose (no-FK) reference to a
connection — kept that way only because that model predates Connection
being consistently the right FK target for every source kind — this config
uses a REAL FK to connections.id: TallyExtractionRun/TallyReconciliationRun
already do the same, so there's no "model doesn't exist yet" reason to
avoid it (see Schedule.anomaly_job_id's own docstring for this exact
reasoning applied to AnomalyJob).
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class TallyReconciliationScheduleConfig(TenantBase):
    """1:1 with Schedule (schedule.id) — the Tally-reconciliation-specific
    fields that don't belong on the generic Schedule row. Presence of a row
    here is what makes execute_schedule() take the real Tally
    extraction+reconciliation dispatch branch instead of the simulated one.
    """
    __tablename__ = "tally_reconciliation_schedule_configs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"), nullable=False, unique=True, index=True)

    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    tolerance: Mapped[float] = mapped_column(Float, default=0.02)

    # Rolling window for the INCREMENTAL PURCHASE_VOUCHERS/PAYMENTS
    # extractions run immediately before each scheduled reconciliation —
    # see tally_scheduled_reconciliation_service.py. Each extraction's own
    # checkpoint (TallyExtractionRun.checkpoint_date) already prevents
    # re-fetching anything already pulled by a PRIOR run of this same
    # schedule, so this only bounds the FIRST run's (or a resumed-after-a-
    # long-gap run's) date range — not a cap on every run's window.
    lookback_days: Mapped[int] = mapped_column(Integer, default=7)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<TallyReconciliationScheduleConfig schedule={self.schedule_id} conn={self.connection_id}>"
