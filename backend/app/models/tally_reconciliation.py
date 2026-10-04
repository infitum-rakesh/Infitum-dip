"""
TALLY-E — reconciliation between materialized ApInvoice rows (Invoice
Intelligence) and TallyCanonicalVoucher/Party rows actually extracted
from a real TallyPrime instance (TALLY-B/C).

Deliberately reuses `app/reconciliation/engine.py`'s real row-diffing
primitives (`ListRowSource`, `diff_keys_and_fields`, its new
`tolerance_columns` parameter) for the two-sided voucher comparison,
and the SAME `app.models.reconciliation.ExceptionType` vocabulary for
outcomes — this is the explicit lesson from `invoice_reconciliation_
service.py` (INV10), which built a second, parallel comparator
(its own `_values_match`/`_normalize`, its own ad-hoc result shape)
instead of reusing the engine that already existed. TALLY-E does not
repeat that: `services/tally_reconciliation_service.py` calls the real
engine for the row-diff part, and only adds what the engine's
two-source-diff abstraction genuinely has no concept of — a ledger
running-balance check and a cross-voucher-type existence check (does a
paid invoice have a corresponding Payment voucher).

`TallyExceptionType` intentionally duplicates the FIRST SEVEN string
values of `app.models.reconciliation.ExceptionType` (same meaning, same
spelling) so a UI rendering "MISMATCH"/"AMBIGUOUS_MATCH"/etc. doesn't
need Tally-specific copy — then adds two genuinely new outcome kinds
this domain needs that the generic engine has no concept of at all.

TALLY-F adds one more thing: `TallyReconciliationException.review_item_id`
below — see that column's own docstring for the "minimal anchor
abstraction" this whole phase is about.
"""
import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class TallyReconciliationRunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class TallyExceptionType(str, enum.Enum):
    # Same vocabulary as app.models.reconciliation.ExceptionType — reused
    # deliberately (see module docstring), not reinvented.
    MISSING = "MISSING"                        # ApInvoice with no matching Tally purchase voucher
    EXTRA = "EXTRA"                             # Tally purchase voucher with no matching ApInvoice
    MISMATCH = "MISMATCH"                       # matched, but a GST/TDS field differs beyond tolerance
    MATCH_BY_NAME = "MATCH_BY_NAME"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    DUPLICATE_TARGET_MATCH = "DUPLICATE_TARGET_MATCH"
    MATCHED = "MATCHED"
    # Genuinely new outcome kinds — not a two-source key/field diff at all.
    LEDGER_BALANCE_MISMATCH = "LEDGER_BALANCE_MISMATCH"  # sum of a vendor's open ApInvoice totals vs that vendor's real Tally closing balance
    PAYMENT_VOUCHER_MISSING = "PAYMENT_VOUCHER_MISSING"  # a matched, old-enough invoice has no corresponding Payment-type voucher in Tally


class TallyReconciliationRun(TenantBase):
    """One run of TALLY-E's reconciliation against ONE Tally connection.
    Mirrors ReconciliationRun's count-summary shape (models/reconciliation.py)
    — reused as a pattern, not literally, since there's no ReconciliationJob
    concept here (the comparison shape is fixed by the accounting domain,
    not a user-configured key/compare-column UI)."""
    __tablename__ = "tally_reconciliation_runs"
    __table_args__ = ({"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # TALLY-J — nullable/additive: every run before this existed (and every
    # manually-triggered run since) leaves both null. Set only when
    # tally_scheduled_reconciliation_service.py creates this run on behalf
    # of a Schedule with a TallyReconciliationScheduleConfig — lets the
    # Monitoring "Tally Integration" tab and a run-history list tell
    # scheduled runs apart from POST /connections/{id}/reconciliation/runs'
    # manual ones, the same "Schedule | Manual | Rerun" convention
    # ScheduleRun.trigger already uses.
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedules.id"), nullable=True, index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="Manual")

    tolerance: Mapped[float] = mapped_column(Float, default=0.02)
    status: Mapped[TallyReconciliationRunStatus] = mapped_column(Enum(TallyReconciliationRunStatus), default=TallyReconciliationRunStatus.PENDING, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Voucher-level two-sided diff counts (from diff_keys_and_fields)
    source_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ApInvoice rows considered
    target_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Tally purchase vouchers considered
    matched_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missing_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mismatched_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ambiguous_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duplicate_target_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Domain-specific finding counts (not part of the row diff)
    ledger_balance_mismatch_count: Mapped[int] = mapped_column(Integer, default=0)
    payment_voucher_missing_count: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TallyReconciliationRun {self.id} conn={self.connection_id} {self.status}>"


class TallyReconciliationException(TenantBase):
    """One row per interesting outcome — same 'only store what needs
    attention (plus a capped MATCHED sample)' convention as
    ReconciliationException."""
    __tablename__ = "tally_reconciliation_exceptions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("tally_reconciliation_runs.id"), nullable=False, index=True)

    exception_type: Mapped[TallyExceptionType] = mapped_column(Enum(TallyExceptionType), nullable=False, index=True)

    # Identifies the ApInvoice / TallyCanonicalVoucher / TallyCanonicalParty
    # this row is about — nullable since not every exception_type has both
    # sides (e.g. EXTRA has no invoice_id, MISSING has no voucher_id).
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("ap_invoices.id"), nullable=True, index=True)
    voucher_id: Mapped[int | None] = mapped_column(ForeignKey("tally_canonical_vouchers.id"), nullable=True, index=True)
    party_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    column: Mapped[str | None] = mapped_column(String(100), nullable=True)  # MISMATCH only — which field differed
    source_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    # TALLY-F's whole "minimal anchor abstraction": the master directive
    # forbids a polymorphic/nullable ReviewQueueItem.document_id and forbids
    # fake Document rows, so ReviewQueueItem itself is never touched. A
    # TallyReconciliationException is only ever escalated to a real
    # ReviewQueueItem when it already has a real anchor available — i.e.
    # invoice_id is set, which means the SAME real Document the ApInvoice
    # was materialized from already exists and gets reused as-is. This
    # column is the one new pointer that makes that link discoverable from
    # the Tally side; nullable because most exception types (EXTRA,
    # DUPLICATE_TARGET_MATCH, LEDGER_BALANCE_MISMATCH — no invoice_id at
    # all — and MATCHED, which needs no review) are never escalated. See
    # tally_reconciliation_service.py's _escalate_exceptions().
    review_item_id: Mapped[int | None] = mapped_column(ForeignKey("review_queue_items.id"), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TallyReconciliationException {self.exception_type} run={self.run_id}>"
