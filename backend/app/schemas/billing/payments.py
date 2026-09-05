"""Schemas for /api/billing/payments/* and the payment-link action (SB26)."""
from datetime import datetime

from pydantic import BaseModel


class PaymentOut(BaseModel):
    id: int
    invoice_id: int
    invoice_number: str | None = None
    organization_id: int
    organization_name: str | None = None
    gateway_id: int
    gateway_provider: str | None = None
    gateway_transaction_id: str
    amount: float
    currency: str
    status: str
    payment_method: str | None
    paid_at: datetime | None
    created_at: datetime


class PaymentListOut(BaseModel):
    items: list[PaymentOut]
    total: int
    page: int
    page_size: int


class PaymentLinkOut(BaseModel):
    payment_url: str
    gateway_reference: str
    expires_at: datetime | None
