"""
ERP posting + reconciliation models for Invoice Intelligence.

See services/invoice/invoice_erp_service.py (idempotent posting, retry,
circuit breaker — INV9) and invoice_reconciliation_service.py (INV10).
`ApInvoiceErpStatus`/`ApInvoiceReconciliationStatus` are defined here and
re-exported from core.py so ApInvoice's rollup columns and this module's
per-attempt/per-run rows share exactly one enum each.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ApInvoiceErpStatus(str, enum.Enum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    DEAD_LETTER = "DEAD_LETTER"


class ApInvoiceReconciliationStatus(str, enum.Enum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    RECONCILED = "RECONCILED"
    MISMATCH = "MISMATCH"
    FAILED = "FAILED"


class ApInvoiceErpTransaction(TenantBase):
    """One row per ERP posting ATTEMPT (not one row per invoice) — a
    retried invoice gets additional rows, all sharing the same
    idempotency_key, so the full attempt history is visible. The UNIQUE
    constraint on idempotency_key is what actually enforces "never create
    a duplicate ERP invoice" (Technical Design §5) at the database level,
    not just in application logic."""
    __tablename__ = "ap_invoice_erp_transactions"
    __table_args__ = (UniqueConstraint("idempotency_key", "attempt_number", name="uq_ap_erp_txn_idempotency_attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ap_invoices.id"), nullable=False, index=True)
    erp_connection_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # Connection.id, no FK

    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Identifies "this logical post of this invoice" across retries — a
    # NEW attempt row shares the same idempotency_key as the first; what
    # must never repeat is the actual ERP call once one attempt SUCCEEDED
    # (see invoice_erp_service's pre-call existing-state check).
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[ApInvoiceErpStatus] = mapped_column(Enum(ApInvoiceErpStatus), default=ApInvoiceErpStatus.PENDING, index=True)
    erp_transaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    request_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Secret-scrubbed before persisting — same convention as
    # connectors/base.py's BaseConnector._scrub().
    response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_retryable: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoiceErpTransaction invoice={self.invoice_id} attempt={self.attempt_number} ({self.status})>"


class ApInvoiceReconciliation(TenantBase):
    __tablename__ = "ap_invoice_reconciliations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ap_invoices.id"), nullable=False, index=True)
    erp_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("ap_invoice_erp_transactions.id"), nullable=True, index=True)

    status: Mapped[ApInvoiceReconciliationStatus] = mapped_column(Enum(ApInvoiceReconciliationStatus), default=ApInvoiceReconciliationStatus.PENDING, index=True)
    # [{field, platform_value, erp_value, result: "MATCH"|"MISMATCH"}]
    field_comparisons: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoiceReconciliation invoice={self.invoice_id} ({self.status})>"
