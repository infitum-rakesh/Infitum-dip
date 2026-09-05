"""
Learning/evaluation + narrow audit-event models for Invoice Intelligence.

DISCLOSED LIMITATION: this codebase has no ML training infrastructure
(no GPU workers, no model registry, no training pipeline) — see the
Invoice Intelligence plan's "genuinely new work" section. These tables
support an honest "correction store + evaluation metrics" workflow
(Technical Design §7's first five steps: capture -> associate -> approve
-> evaluation set -> [stop here]), NOT automated retraining. See
services/invoice/invoice_learning_service.py's module docstring for the
same disclosure at the service layer (INV13).
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ApInvoiceLearningRecord(TenantBase):
    """One row per reviewer field correction pulled from the existing
    ReviewAuditLog (models/review_queue.py) — this table does NOT
    duplicate ReviewAuditLog's audit function, it re-indexes the subset
    of corrections that touched an ApInvoice field by vendor/field, which
    ReviewAuditLog's generic (review_item_id, action) shape can't answer
    efficiently for evaluation metrics."""
    __tablename__ = "ap_invoice_learning_records"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ap_invoices.id"), nullable=False, index=True)
    review_audit_log_id: Mapped[int | None] = mapped_column(ForeignKey("review_audit_logs.id"), nullable=True, index=True)

    field_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("ap_invoice_vendors.id"), nullable=True, index=True)
    document_layout_hint: Mapped[str | None] = mapped_column(String(120), nullable=True)  # freeform, e.g. vendor+template guess

    reviewer_id: Mapped[int | None] = mapped_column(nullable=True)
    approved_for_learning: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoiceLearningRecord invoice={self.invoice_id} field={self.field_name}>"


class ApInvoiceAuditEventType(str, enum.Enum):
    """Narrow, invoice-lifecycle-specific events NOT already covered by
    Document.timeline (OCR/extraction stage timestamps) or ReviewAuditLog
    (field corrections/review actions) — see the plan's explicit
    "do not duplicate" note. Everything else reads from those two."""
    DECISION_MADE = "DECISION_MADE"
    DUPLICATE_DETECTED = "DUPLICATE_DETECTED"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"  # INV23 — a CRITICAL anomaly finding downgraded STRAIGHT_THROUGH to REVIEW_REQUIRED
    ERP_SUBMITTED = "ERP_SUBMITTED"
    ERP_SUCCEEDED = "ERP_SUCCEEDED"
    ERP_FAILED = "ERP_FAILED"
    ERP_RETRIED = "ERP_RETRIED"
    ERP_DEAD_LETTERED = "ERP_DEAD_LETTERED"
    RECONCILIATION_RUN = "RECONCILIATION_RUN"


class ApInvoiceAuditEvent(TenantBase):
    __tablename__ = "ap_invoice_audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ap_invoices.id"), nullable=False, index=True)

    event_type: Mapped[ApInvoiceAuditEventType] = mapped_column(Enum(ApInvoiceAuditEventType), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    actor_id: Mapped[int | None] = mapped_column(nullable=True)  # null = system

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoiceAuditEvent invoice={self.invoice_id} {self.event_type}>"
