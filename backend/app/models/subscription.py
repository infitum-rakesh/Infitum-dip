"""
Subscription — the real record of what a customer (Organization) is
subscribed to and until when (SELLER PORTAL spec, section 2). Every
Seller-visible KPI about revenue/active-subscriptions/expiring-soon reads
this table, not Organization directly.

Organization.plan/license_status/trial_expires_at/license_expires_at
(models/organization.py) remain the fields the REST of the app already
reads for gating (require_action, the frontend's evaluateAccess()) — this
table is the Seller-side system of record that DRIVES those fields.
services/seller/subscription_service.py (Phase 3) is what keeps them in
sync when a Seller creates/renews/changes a subscription; for now,
services/seller/plan_seed.py does a one-time backfill so every
pre-existing Organization has a real Subscription row instead of the
relationship being implicit.
"""
import enum
from datetime import datetime

from sqlalchemy import String, DateTime, Float, Boolean, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase
from app.models.plan import BillingCycle


class SubscriptionStatus(str, enum.Enum):
    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    EXPIRING = "EXPIRING"
    EXPIRED = "EXPIRED"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"


class Subscription(PlatformBase):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id"), nullable=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True, index=True)

    status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), default=SubscriptionStatus.TRIAL, index=True)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
