"""
PaymentLink — a "Pay Now" link generated through the configured payment
gateway (spec section 17: "DO NOT construct payment URLs manually — the
gateway integration must generate the payment session/payment link").
`payment_url` is always whatever the provider's
create_payment_link()/create_payment_session() call returned.
"""
import enum
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class PaymentLinkStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    PAID = "PAID"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PaymentLink(PlatformBase):
    __tablename__ = "payment_links"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), nullable=False, index=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("payment_gateways.id"), nullable=False, index=True)

    # The provider's own session/link identifier (e.g. Stripe Checkout
    # Session id) — needed to look the session back up when verifying.
    gateway_reference: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    payment_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    # Opaque token used by the Custom Gateway's public /pay/{token} route
    # (see payment_gateways/custom_provider.py) — other providers' links
    # point straight at their own hosted checkout and don't need this.
    token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)

    status: Mapped[PaymentLinkStatus] = mapped_column(Enum(PaymentLinkStatus), default=PaymentLinkStatus.ACTIVE, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<PaymentLink invoice={self.invoice_id} {self.status}>"
