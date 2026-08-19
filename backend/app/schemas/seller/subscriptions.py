"""Pydantic models for /api/seller/plans/* and /api/seller/subscriptions/* (Phase 3)."""
from datetime import datetime

from pydantic import BaseModel


# ── Plans ────────────────────────────────────────────────────────

class PlanOut(BaseModel):
    id: int
    code: str
    name: str
    description: str | None
    modules: list[str]
    limits: dict
    price: float
    currency: str
    billing_cycle: str
    trial_days: int | None
    is_system: bool
    is_active: bool

    model_config = {"from_attributes": True}


class PlanListOut(BaseModel):
    items: list[PlanOut]


class CreatePlanRequest(BaseModel):
    code: str
    name: str
    description: str | None = None
    modules: list[str] = []
    limits: dict = {}
    price: float = 0.0
    currency: str = "USD"
    billing_cycle: str = "MONTHLY"
    trial_days: int | None = None


class UpdatePlanRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    modules: list[str] | None = None
    limits: dict | None = None
    price: float | None = None
    currency: str | None = None
    billing_cycle: str | None = None
    trial_days: int | None = None
    is_active: bool | None = None


# ── Subscriptions ────────────────────────────────────────────────

class SubscriptionListItem(BaseModel):
    id: int
    organization_id: int
    organization_name: str
    plan_id: int | None
    plan_name: str | None
    plan_code: str | None
    status: str
    start_date: datetime
    end_date: datetime | None
    billing_cycle: str
    amount: float
    currency: str
    auto_renew: bool
    created_at: datetime


class SubscriptionListOut(BaseModel):
    items: list[SubscriptionListItem]
    total: int
    page: int
    page_size: int


class SubscriptionOut(BaseModel):
    id: int
    organization_id: int
    plan_id: int | None
    status: str
    start_date: datetime
    end_date: datetime | None
    billing_cycle: str
    amount: float
    currency: str
    auto_renew: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CreateSubscriptionRequest(BaseModel):
    organization_id: int
    plan_id: int | None = None
    status: str = "TRIAL"  # one of SubscriptionStatus values: TRIAL|ACTIVE|EXPIRING|EXPIRED|SUSPENDED|CANCELLED
    start_date: datetime | None = None
    end_date: datetime | None = None
    billing_cycle: str = "MONTHLY"
    amount: float | None = None
    currency: str | None = None
    auto_renew: bool = False


class UpdateSubscriptionRequest(BaseModel):
    end_date: datetime | None = None
    amount: float | None = None
    currency: str | None = None
    auto_renew: bool | None = None
    billing_cycle: str | None = None


class ChangePlanRequest(BaseModel):
    plan_id: int
