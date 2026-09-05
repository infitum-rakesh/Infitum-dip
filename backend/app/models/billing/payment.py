"""
Payment — a real, gateway-verified charge against an Invoice. Never
created directly off a frontend "payment succeeded" claim — always via
services/billing/payment_service.py::record_payment(), which re-verifies
against the gateway's own get_payment_status()/webhook payload first.

UniqueConstraint(gateway_id, gateway_transaction_id) is the idempotency
backbone: the same webhook arriving twice (spec section 24/48) can only
ever produce one Payment row — a second insert attempt hits the
constraint and is treated as a duplicate, not a new payment.
"""
import enum
from datetime import datetime

from sqlalchemy import String, DateTime, Float, ForeignKey, Enum, JSON, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    CHARGEBACK = "CHARGEBACK"


class Payment(PlatformBase):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("gateway_id", "gateway_transaction_id", name="uq_payment_gateway_transaction"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False, index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("payment_gateways.id"), nullable=False, index=True)

    # The gateway's own charge/session/order id — never a client-supplied
    # value; always read from the gateway's verified response/webhook.
    gateway_transaction_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.PENDING, index=True)
    payment_method: Mapped[str | None] = mapped_column(String(60), nullable=True)  # e.g. "card", "bank_transfer", "manual"
    # Secret-scrubbed reference payload from the gateway (never card
    # numbers/CVV/secrets — see services/billing/payment_service.py's
    # scrubbing pass, same discipline as the connector layer's existing
    # "scrub secrets from persisted error messages" precedent).
    gateway_response_reference: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<Payment invoice={self.invoice_id} {self.status} {self.amount}{self.currency}>"
