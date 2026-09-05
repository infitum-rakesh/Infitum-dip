"""
One-time (idempotent) seed/backfill run at startup — see main.py lifespan.

1. Ensures exactly one Seller row exists (the platform operator — see
   models/seller.py).
2. Ensures the starter Plan catalog exists (trial/basic/professional/
   enterprise), migrating the module lists that used to live only in
   frontend/src/utils/permissions.js's SUBSCRIPTION_PLANS and the limits
   from services/license_service.py's PLAN_LIMITS into the real Plan
   table — SELLER PORTAL spec section 2: "Do NOT hardcode these limits.
   Store plans and limits in the backend."
3. Backfills a real Subscription row for every pre-existing Organization
   that doesn't have one yet, so the Seller Dashboard's subscription/
   revenue KPIs reflect real rows instead of re-deriving from
   Organization fields every time.
4. Seeds a default price for every catalog module (models/billing/
   module_price.py) — what ad-hoc Purchased-Modules grants and "Custom"
   plan subscriptions are billed at. A Seller can edit any of these for
   real afterward (Settings -> Module Prices); this only fills in what's
   missing so an edit is never silently overwritten on the next startup.

Safe to run on every startup — each step only inserts what's missing.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing.module_price import ModulePrice
from app.models.organization import LicenseStatus, Organization
from app.models.plan import BillingCycle, Plan
from app.models.seller import Seller
from app.models.subscription import Subscription, SubscriptionStatus

# Default monthly USD price per module — a starting point, not a fixed
# rule; edit for real in Seller Settings -> Module Prices. Keys must match
# services/seller/module_service.py's MODULE_CATALOG (checked at seed
# time, not hardcoded here, so this file never drifts from that list).
_DEFAULT_MODULE_PRICES: dict[str, float] = {
    "DOCUMENT_INTELLIGENCE": 149.0,
    "OCR": 49.0,
    "AI_EXTRACTION": 99.0,
    "TRANSFORMATION": 129.0,
    "SQL_WORKSPACE": 149.0,
    "VALIDATION": 79.0,
    "RECONCILIATION": 99.0,
    "PIPELINE_SCHEDULER": 89.0,
    "INCIDENTS": 69.0,
    "LINEAGE": 99.0,
    "OBSERVABILITY": 119.0,
    "ANOMALY_DETECTION": 129.0,
    "DATA_CATALOG": 79.0,
    "INVOICE_INTELLIGENCE": 179.0,
}

# Mirrors frontend/src/utils/permissions.js SUBSCRIPTION_PLANS module lists
# and services/license_service.py PLAN_LIMITS — kept in sync manually until
# the frontend switches to reading GET /api/seller/dashboard's plan catalog
# instead of its own hardcoded copy (Phase 3).
_STARTER_PLANS = [
    {
        "code": "trial", "name": "Free Trial", "trial_days": 14,
        "modules": ["DOCUMENT_INTELLIGENCE", "OCR", "AI_EXTRACTION", "TRANSFORMATION", "VALIDATION"],
        "limits": {"users": 5, "documents": 500, "ai_calls": 1000, "storage_gb": 5},
        "price": 0.0, "billing_cycle": BillingCycle.CUSTOM,
    },
    {
        "code": "basic", "name": "Basic",
        "modules": ["DOCUMENT_INTELLIGENCE", "OCR", "AI_EXTRACTION", "TRANSFORMATION", "VALIDATION"],
        "limits": {"users": 10, "documents": 5000, "ai_calls": 10000, "storage_gb": 50},
        "price": 199.0, "billing_cycle": BillingCycle.MONTHLY,
    },
    {
        "code": "professional", "name": "Professional",
        "modules": [
            "DOCUMENT_INTELLIGENCE", "OCR", "AI_EXTRACTION", "TRANSFORMATION", "VALIDATION",
            "RECONCILIATION", "DATA_CATALOG", "LINEAGE", "PIPELINE_SCHEDULER",
        ],
        "limits": {"users": 50, "documents": 50000, "ai_calls": 100000, "storage_gb": 500},
        "price": 999.0, "billing_cycle": BillingCycle.MONTHLY,
    },
    {
        "code": "enterprise", "name": "Enterprise",
        "modules": [
            "DOCUMENT_INTELLIGENCE", "OCR", "AI_EXTRACTION", "TRANSFORMATION", "VALIDATION",
            "RECONCILIATION", "SQL_WORKSPACE", "DATA_CATALOG", "LINEAGE", "PIPELINE_SCHEDULER",
            "OBSERVABILITY", "INCIDENTS",
        ],
        "limits": {"users": -1, "documents": -1, "ai_calls": -1, "storage_gb": -1},
        "price": 2999.0, "billing_cycle": BillingCycle.MONTHLY,
    },
]

_ORG_STATUS_TO_SUB_STATUS = {
    LicenseStatus.TRIAL: SubscriptionStatus.TRIAL,
    LicenseStatus.ACTIVE: SubscriptionStatus.ACTIVE,
    LicenseStatus.EXPIRING: SubscriptionStatus.EXPIRING,
    LicenseStatus.EXPIRED: SubscriptionStatus.EXPIRED,
    LicenseStatus.SUSPENDED: SubscriptionStatus.SUSPENDED,
}


async def ensure_seller_seeded(db: AsyncSession) -> Seller:
    result = await db.execute(select(Seller).limit(1))
    seller = result.scalar_one_or_none()
    if seller is None:
        seller = Seller(name="Platform Seller", default_trial_days=14)
        db.add(seller)
        await db.flush()

    result = await db.execute(select(Plan))
    existing_codes = {p.code for p in result.scalars().all()}
    for spec in _STARTER_PLANS:
        if spec["code"] in existing_codes:
            continue
        db.add(Plan(
            seller_id=seller.id, code=spec["code"], name=spec["name"],
            description=f"{spec['name']} plan", modules=spec["modules"], limits=spec["limits"],
            price=spec["price"], currency="USD", billing_cycle=spec["billing_cycle"],
            trial_days=spec.get("trial_days"), is_system=True, is_active=True,
        ))
    await db.flush()

    # Backfill: one Subscription per Organization that doesn't have one yet.
    plans_by_code = {p.code: p for p in (await db.execute(select(Plan))).scalars().all()}
    orgs_result = await db.execute(select(Organization))
    orgs = orgs_result.scalars().all()
    subs_result = await db.execute(select(Subscription.organization_id))
    orgs_with_subs = {row[0] for row in subs_result.all()}

    now = datetime.now(timezone.utc)
    for org in orgs:
        if org.id in orgs_with_subs:
            continue
        plan = plans_by_code.get(org.plan) or plans_by_code.get("trial")
        end_date = org.license_expires_at or org.trial_expires_at
        sub = Subscription(
            seller_id=seller.id,
            organization_id=org.id,
            plan_id=plan.id if plan else None,
            status=_ORG_STATUS_TO_SUB_STATUS.get(org.license_status, SubscriptionStatus.TRIAL),
            start_date=org.created_at if org.created_at.tzinfo else org.created_at.replace(tzinfo=timezone.utc),
            end_date=end_date,
            billing_cycle=plan.billing_cycle if plan else BillingCycle.MONTHLY,
            amount=plan.price if (plan and org.license_status != LicenseStatus.TRIAL) else 0.0,
            currency="USD",
            auto_renew=False,
        )
        db.add(sub)

    # Module price catalog — one row per MODULE_CATALOG key, only inserting
    # what's missing (never overwrites a Seller's real edit). Imported
    # locally to dodge a startup-order import cycle with module_service.
    from app.services.seller.module_service import MODULE_CATALOG
    existing_prices = {mp.module for mp in (await db.execute(select(ModulePrice))).scalars().all()}
    for module in MODULE_CATALOG:
        if module in existing_prices:
            continue
        db.add(ModulePrice(module=module, price=_DEFAULT_MODULE_PRICES.get(module, 0.0), currency="USD"))

    await db.commit()
    return seller
