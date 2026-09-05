"""Response models for GET /api/seller/billing/dashboard (SB25)."""
from datetime import datetime

from pydantic import BaseModel


class RecentInvoiceOut(BaseModel):
    id: int
    invoice_number: str
    organization_name: str
    status: str
    total: float
    currency: str
    issue_date: datetime


class RecentPaymentOut(BaseModel):
    id: int
    organization_name: str
    amount: float
    currency: str
    status: str
    payment_method: str | None
    created_at: datetime


class BillingDashboardOut(BaseModel):
    total_revenue: float
    revenue_last_30d: float
    mrr: float
    outstanding_invoices_count: int
    outstanding_invoices_amount: float
    active_subscriptions: int
    expiring_subscriptions: int
    expired_subscriptions: int
    payments_succeeded_30d: int
    payments_failed_30d: int
    payment_success_rate: float | None
    active_gateways: list[str]
    configured_gateways: int
    recent_invoices: list[RecentInvoiceOut]
    recent_payments: list[RecentPaymentOut]
