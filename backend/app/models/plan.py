"""
Plan — Seller-configurable subscription plan catalog (SELLER PORTAL spec,
section 2: "Support configurable plans... Do NOT hardcode these limits.
Store plans and limits in the backend"). Purchased Modules (Phase 4) and
Subscriptions (Phase 3) draw from this table.

This supersedes the previously hardcoded PLAN_LIMITS/PLAN_LABELS in
services/license_service.py and SUBSCRIPTION_PLANS in
frontend/src/utils/permissions.js as the source of truth going forward —
those constants are left in place as display fallbacks for
pre-Subscription-table organizations (see services/seller/plan_seed.py,
which backfills a real Subscription+Plan pair for every existing
Organization so the fallback path stops being exercised for real data).
"""
import enum
from datetime import datetime

from sqlalchemy import String, DateTime, JSON, Boolean, Float, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class BillingCycle(str, enum.Enum):
    MONTHLY = "MONTHLY"
    ANNUAL = "ANNUAL"
    CUSTOM = "CUSTOM"


class Plan(PlatformBase):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id"), nullable=True, index=True)

    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)  # trial|basic|professional|enterprise|<custom>
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    modules: Mapped[list] = mapped_column(JSON, default=list)  # module keys this plan grants (the licensing ceiling)
    # {users, documents, ai_calls, storage_gb, ...} — -1 or absent = unlimited.
    # Extensible: new resource keys don't need a migration.
    limits: Mapped[dict] = mapped_column(JSON, default=dict)

    price: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    billing_cycle: Mapped[BillingCycle] = mapped_column(Enum(BillingCycle), default=BillingCycle.MONTHLY)
    trial_days: Mapped[int | None] = mapped_column(nullable=True)

    is_system: Mapped[bool] = mapped_column(Boolean, default=False)  # seeded starter plan — code is protected, limits/modules stay editable
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Plan {self.code}>"
