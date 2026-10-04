"""
tally_writeback.py — TALLY-I: the actual Tally write, gated behind
TALLY-H's approval (preview.approval_status == APPROVED — see
TallyVoucherApprovalStatus's own docstring) and TALLY-D's structural
pre-write validation (preview.status == READY). Mirrors Invoice
Intelligence's ERP-posting pair exactly on purpose — ApInvoiceErpTransaction
(models/invoice_intelligence/erp.py) is one row per posting ATTEMPT with a
UNIQUE(idempotency_key, attempt_number) constraint that is what actually
enforces "never post the same voucher twice" at the database level, and
ApInvoiceReconciliation is a real post-write GET-and-compare. This module
reuses that exact shape for Tally rather than inventing a second pattern:
TallyVoucherWriteBackTransaction / TallyVoucherWriteBackVerification.

Disclosed, same as connectors/application/tallyprime.py itself: real
code, but never live-verified against an actual licensed TallyPrime
instance — verified instead against a local mock Tally XML/HTTP gateway
(see tests/test_tallyprime_connector.py and TALLY-I7's live-verification
report).
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class TallyWriteBackStatus(str, enum.Enum):
    """Mirrors ApInvoiceErpStatus's own vocabulary intentionally — this
    codebase already has one well-understood posting-status enum shape;
    reusing the same names here means anyone who understands invoice ERP
    posting immediately understands Tally write-back too."""
    NOT_WRITTEN = "NOT_WRITTEN"
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    DEAD_LETTER = "DEAD_LETTER"


class TallyVoucherWriteBackTransaction(TenantBase):
    """One row per write ATTEMPT (not one row per preview) — a retried
    write gets additional rows sharing the same idempotency_key, so the
    full attempt history stays visible. idempotency_key is the SAME
    deterministic value TallyVoucherPreview.idempotency_key already
    carries (ap-invoice-{id}-tally-{connection_id}, set at preview-build
    time) — the UNIQUE constraint below is what actually enforces "never
    send this voucher to Tally twice" at the database level, exactly like
    ApInvoiceErpTransaction's own constraint does for ERP posting."""
    __tablename__ = "tally_voucher_writeback_transactions"
    __table_args__ = (UniqueConstraint("idempotency_key", "attempt_number", name="uq_tally_writeback_txn_idempotency_attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    preview_id: Mapped[int] = mapped_column(ForeignKey("tally_voucher_previews.id"), nullable=False, index=True)
    connection_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # Connection.id, no FK — same cross-module convention as ApInvoiceErpTransaction.erp_connection_id

    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[TallyWriteBackStatus] = mapped_column(Enum(TallyWriteBackStatus), default=TallyWriteBackStatus.PENDING, index=True)

    request_xml: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # secret-scrubbed before persisting, same convention as connectors/base.py's BaseConnector._scrub()

    tally_voucher_guid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tally_master_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    tally_voucher_number: Mapped[str | None] = mapped_column(String(100), nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_retryable: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<TallyVoucherWriteBackTransaction preview={self.preview_id} attempt={self.attempt_number} ({self.status})>"


class TallyWriteBackVerificationStatus(str, enum.Enum):
    VERIFIED = "VERIFIED"     # found in Tally, key fields match
    MISMATCH = "MISMATCH"     # found in Tally, but a field disagrees
    NOT_FOUND = "NOT_FOUND"   # Tally reported success but a read-back query found no matching voucher
    FAILED = "FAILED"         # the verification GET itself failed (network/parse) — distinct from a genuine NOT_FOUND


class TallyVoucherWriteBackVerification(TenantBase):
    """Real post-write GET-and-compare (Phase 15's own "post-write read-
    back verification" requirement) — never inferred from the write
    response alone. Tally's Import response confirms it ACCEPTED the
    voucher (CREATED=1, no LINEERROR); it does not prove the voucher now
    reads back with the exact fields this platform sent. Mirrors
    invoice_reconciliation_service.py's field_comparisons shape exactly:
    [{field, platform_value, tally_value, result: "MATCH"|"MISMATCH"}]."""
    __tablename__ = "tally_voucher_writeback_verifications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    preview_id: Mapped[int] = mapped_column(ForeignKey("tally_voucher_previews.id"), nullable=False, index=True)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("tally_voucher_writeback_transactions.id"), nullable=True, index=True)

    status: Mapped[TallyWriteBackVerificationStatus] = mapped_column(Enum(TallyWriteBackVerificationStatus), default=TallyWriteBackVerificationStatus.FAILED, index=True)
    field_comparisons: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TallyVoucherWriteBackVerification preview={self.preview_id} ({self.status})>"
