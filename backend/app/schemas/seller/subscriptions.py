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
    # SB28 — status of this subscription's most recent Invoice (its
    # real payment state), None when it has never had one billed yet.
    payment_status: str | None = None


class SubscriptionListOut(BaseModel):
    items: list[SubscriptionListItem]
    total: int
    page: int
    page_size: int


class SubscriptionOut(BaseModel):
    id: int
    organization_id: int
    plan_id: int | None
    pending_plan_id: int | None = None
    status: str
    start_date: datetime
    end_date: datetime | None
    trial_start_date: datetime | None = None
    trial_end_date: datetime | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    next_billing_date: datetime | None = None
    cancelled_at: datetime | None = None
    billing_cycle: str
    amount: float
    currency: str
    auto_renew: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SubscriptionModuleOut(BaseModel):
    module: str
    status: str
    enabled: bool
    expiry_date: datetime | None = None


class SubscriptionModulesOut(BaseModel):
    subscription_id: int
    items: list[SubscriptionModuleOut]


class CreateSubscriptionRequest(BaseModel):
    organization_id: int
    plan_id: int | None = None
    status: str = "TRIAL"  # one of SubscriptionStatus values: TRIAL|PENDING|ACTIVE|EXPIRING|PAST_DUE|PAYMENT_PENDING|EXPIRED|SUSPENDED|CANCELLED|TERMINATED
    start_date: datetime | None = None
    end_date: datetime | None = None
    billing_cycle: str = "MONTHLY"
    amount: float | None = None
    currency: str | None = None
    auto_renew: bool = False
    # Extra modules beyond the plan's own list (or the full module set for
    # a plan-less/custom subscription) — unioned with plan.modules, never
    # replaces it. See services/seller/subscription_service.create_subscription().
    modules: list[str] | None = None


class UpdateSubscriptionRequest(BaseModel):
    end_date: datetime | None = None
    amount: float | None = None
    currency: str | None = None
    auto_renew: bool | None = None
    billing_cycle: str | None = None


class ChangePlanRequest(BaseModel):
    plan_id: int
    # "immediate" (default) or "period_end" (spec section 37 — downgrades
    # only; an upgrade always applies immediately regardless of this flag).
    effective: str = "immediate"


class ExtendSubscriptionRequest(BaseModel):
    days: int = 30


class AddModuleRequest(BaseModel):
    organization_id: int
    module: str
    # None = price from the module price catalog (Settings -> Module
    # Prices) — see subscription_service.add_module(). Pass an explicit
    # value only to override that catalog price for this one grant.
    amount: float | None = None
    currency: str = "USD"
    billing_cycle: str = "MONTHLY"
    auto_renew: bool = False
    end_date: datetime | None = None
