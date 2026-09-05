"""
PaymentWebhookEvent — the idempotency ledger for inbound payment
webhooks (spec sections 24/48). UniqueConstraint(gateway_id, event_id)
means the same provider event landing twice (retried delivery, replay)
can only ever be recorded once; a second insert attempt is caught and
handled as IGNORED_DUPLICATE by services/billing/payment_service.py
rather than reprocessing the payment.
"""
import enum
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Enum, JSON, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class WebhookEventStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    IGNORED_DUPLICATE = "IGNORED_DUPLICATE"


class PaymentWebhookEvent(PlatformBase):
    __tablename__ = "payment_webhook_events"
    __table_args__ = (UniqueConstraint("gateway_id", "event_id", name="uq_webhook_gateway_event"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("payment_gateways.id"), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)  # the provider's own event id
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)  # PAYMENT_SUCCESS|PAYMENT_FAILED|PAYMENT_PENDING|REFUND|CHARGEBACK|...
    # Scrubbed raw payload — never card numbers/secrets. Kept for audit/
    # debugging, never re-parsed as the source of truth after initial
    # processing (the Payment row is).
    payload_reference: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[WebhookEventStatus] = mapped_column(Enum(WebhookEventStatus), default=WebhookEventStatus.RECEIVED, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<PaymentWebhookEvent gateway={self.gateway_id} {self.event_type} {self.status}>"
