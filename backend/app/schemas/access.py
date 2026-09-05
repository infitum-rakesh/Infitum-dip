"""
Schemas for GET /api/access/check and /api/access/modules-summary
(Multi-Subscription Billing & Payments Platform, 2026, spec sections 40/56).
"""
from datetime import datetime

from pydantic import BaseModel


class AccessCheckOut(BaseModel):
    granted: bool
    reason: str | None = None
    module: str
    expired_at: datetime | None = None
    covered_by_other_subscription: bool = False
    # Human-readable, professional denial message (spec section 27) — the
    # frontend's PermissionGate/SubscriptionExpiredNotice render this
    # directly rather than deriving their own copy from `reason`.
    message: str | None = None


class ExpiringModuleOut(BaseModel):
    module: str
    expiry_date: datetime | None = None
    subscription_id: int | None = None


class ModulesSummaryOut(BaseModel):
    active_modules: list[str]
    expired_modules: dict[str, datetime | None]
    expiring_modules: list[ExpiringModuleOut]
    has_tenant_access: bool


class SalesContactOut(BaseModel):
    """SB30 — the real Seller.sales_* fields (spec section 46), never a
    hardcoded seller identity. Any field can be null if the Seller hasn't
    configured it yet in Seller Settings; the frontend renders only what's
    actually present rather than fabricating a contact."""
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    url: str | None = None
