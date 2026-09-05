"""
Invoice / InvoiceLineItem / InvoiceCounter — generated for EVERY
subscription lifecycle event (create incl. trial, renew, upgrade,
downgrade, add-module, extend), never only for paid ones. A trial
invoice posts NO_PAYMENT_REQUIRED at $0.00 immediately; a paid one posts
PAYMENT_PENDING until a real, gateway-verified Payment flips it to PAID.

See services/billing/invoice_service.py for creation/numbering and
services/billing/payment_service.py for the PAID transition — this
module is schema only, no business logic.
"""
import enum
from datetime import datetime

from sqlalchemy import (
    String, DateTime, Float, ForeignKey, Enum, Integer, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class InvoiceStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    OVERDUE = "OVERDUE"
    VOID = "VOID"
    CANCELLED = "CANCELLED"
    NO_PAYMENT_REQUIRED = "NO_PAYMENT_REQUIRED"


class Invoice(PlatformBase):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # "INV-2026-000123" — generated server-side only, see
    # services/billing/invoice_service.py::next_invoice_number(). Never
    # generated in frontend JS, per spec section 14.
    invoice_number: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)

    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True, index=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id"), nullable=True, index=True)

    status: Mapped[InvoiceStatus] = mapped_column(Enum(InvoiceStatus), default=InvoiceStatus.DRAFT, index=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    subtotal: Mapped[float] = mapped_column(Float, default=0.0)
    discount: Mapped[float] = mapped_column(Float, default=0.0)
    tax: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)

    billing_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    billing_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issue_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # What triggered this invoice — CREATE|TRIAL|RENEWAL|UPGRADE|DOWNGRADE|
    # ADD_MODULE|EXTENSION — surfaced on the Seller Invoices screen and in
    # the PDF; purely descriptive, no behavior branches on it besides display.
    reason: Mapped[str] = mapped_column(String(30), default="CREATE")

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Invoice {self.invoice_number} {self.status}>"


class InvoiceLineItem(PlatformBase):
    __tablename__ = "invoice_line_items"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False, index=True)
    module: Mapped[str | None] = mapped_column(String(60), nullable=True)  # one of module_service.MODULE_CATALOG keys, or None for a plan-level line
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    discount: Mapped[float] = mapped_column(Float, default=0.0)
    tax: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)

    def __repr__(self) -> str:
        return f"<InvoiceLineItem invoice={self.invoice_id} {self.description}>"


class InvoiceCounter(PlatformBase):
    """
    Concurrency-safe, year-scoped invoice number sequence. SQLite has no
    real SEQUENCE object, so this is one row per year, incremented inside
    a committed transaction; combined with SQLite's single-writer WAL
    semantics and an in-process asyncio.Lock in invoice_service.py (never
    nested — see that module's own warning about the prior asyncio-lock
    deadlock bug found elsewhere in this codebase), this is safe.
    """
    __tablename__ = "invoice_counters"
    __table_args__ = (UniqueConstraint("year", name="uq_invoice_counter_year"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    last_number: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:
        return f"<InvoiceCounter {self.year}:{self.last_number}>"
