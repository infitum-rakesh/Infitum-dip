"""
Subscription — the real record of what a customer (Organization) is
subscribed to and until when (SELLER PORTAL spec, section 2). Every
Seller-visible KPI about revenue/active-subscriptions/expiring-soon reads
this table, not Organization directly.

Organization.plan/license_status/trial_expires_at/license_expires_at
(models/organization.py) remain fields the REST of the app can read for
DISPLAY (evaluateAccess(), the org-side Licenses dashboard, sidebar) — a
denormalized cache kept in sync by services/seller/subscription_service.py
and the hourly expiry job. They are no longer the enforcement source of
truth: core/deps.require_action() and api/sql_workspace.py's
require_sql_action() now call
services/billing/access_service.SubscriptionAccessService, which
evaluates real Subscription/CustomerModule rows live, per request.

Multi-Subscription Billing & Payments Platform (2026): an Organization
can hold MULTIPLE simultaneous Subscription rows with overlapping module
grants — e.g. two different subscriptions can each independently carry
an entitlement for the same module (see models/customer_module.py's
subscription_id FK), so one expiring never removes a module another
still covers. Do not assume "the latest row by created_at" is the only
one that matters; that assumption only holds for legacy single-
subscription orgs, which the access service falls back to when an org
has zero Subscription rows at all.
"""
import enum
from datetime import datetime

from sqlalchemy import String, DateTime, Float, Boolean, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase
from app.models.plan import BillingCycle


class SubscriptionStatus(str, enum.Enum):
    TRIAL = "TRIAL"
    PENDING = "PENDING"            # created, awaiting first payment — not yet granting access
    ACTIVE = "ACTIVE"
    EXPIRING = "EXPIRING"
    PAST_DUE = "PAST_DUE"          # renewal invoice unpaid, grace period before PAYMENT_PENDING
    PAYMENT_PENDING = "PAYMENT_PENDING"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"
    TERMINATED = "TERMINATED"      # hard-terminated by Seller, distinct from a natural CANCELLED wind-down


class Subscription(PlatformBase):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id"), nullable=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True, index=True)
    # Downgrade-at-period-end (spec section 37): set instead of mutating
    # plan_id immediately; services/billing/expiry_job.py applies it once
    # current_period_end/end_date is reached, never before. NULL = no
    # pending downgrade.
    pending_plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)

    status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), default=SubscriptionStatus.TRIAL, index=True)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Hard expiry date — the spec's "expiry_date". Kept as end_date (not a
    # duplicate column) since every existing renew/extend/change-plan call
    # site already reads/writes this name.
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Multi-Subscription Billing & Payments Platform (2026) — full date
    # model (spec section 3). All nullable/additive so pre-existing rows
    # (and the org-backfill in plan_seed.py) keep working unmodified.
    trial_start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    billing_start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_billing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    billing_cycle: Mapped[BillingCycle] = mapped_column(Enum(BillingCycle), default=BillingCycle.MONTHLY)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False)

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Subscription org={self.organization_id} {self.status}>"
