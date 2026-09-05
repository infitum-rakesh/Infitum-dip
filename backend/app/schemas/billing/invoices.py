"""Schemas for /api/billing/invoices/* (spec section 44/45/56)."""
from datetime import datetime

from pydantic import BaseModel


class InvoiceLineItemOut(BaseModel):
    id: int
    module: str | None
    description: str
    quantity: int
    unit_price: float
    discount: float
    tax: float
    total: float

    model_config = {"from_attributes": True}


class InvoiceOut(BaseModel):
    id: int
    invoice_number: str
    organization_id: int
    organization_name: str | None = None
    subscription_id: int | None
    status: str
    currency: str
    subtotal: float
    discount: float
    tax: float
    total: float
    billing_period_start: datetime | None
    billing_period_end: datetime | None
    issue_date: datetime
    due_date: datetime | None
    paid_at: datetime | None
    reason: str
    created_at: datetime
    line_items: list[InvoiceLineItemOut] = []


class InvoiceListItem(BaseModel):
    id: int
    invoice_number: str
    organization_id: int
    organization_name: str
    subscription_id: int | None
    status: str
    currency: str
    total: float
    issue_date: datetime
    due_date: datetime | None
    reason: str


class InvoiceListOut(BaseModel):
    items: list[InvoiceListItem]
    total: int
    page: int
    page_size: int
