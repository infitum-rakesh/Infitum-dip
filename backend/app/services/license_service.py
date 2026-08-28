"""
License service — Administration -> Licenses.

Read-only for organization admins: Seller controls purchased modules,
subscription plan and expiration (Organization.licensed_modules/plan/
license_status/*_expires_at — set at registration today; no Seller-side
edit UI exists yet, same "not built yet" scope boundary as everywhere
else a sibling module hasn't been implemented). Org admins only control
*which of their users* get access to purchased modules, via
Administration -> Users' Module Access tab (already built).

Per-module usage is real where this codebase actually persists something
countable (Documents/OCR/AI Extraction -> the documents table), and
reported as untracked (None) everywhere else rather than a fabricated
number — see MODULE_USAGE_RESOURCE.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.organization import LicenseStatus, Organization
from app.models.seller import Seller
from app.models.seller_audit_log import record_seller_event
from app.models.user import User

# Mirrors frontend/src/utils/permissions.js SUBSCRIPTION_PLANS exactly —
# keep both in sync if either changes.
PLAN_LIMITS: dict[str, dict[str, int]] = {
    "trial":        {"users": 5,  "documents": 500,   "ai_calls": 1_000,   "storage_gb": 5},
    "basic":        {"users": 10, "documents": 5_000,  "ai_calls": 10_000,  "storage_gb": 50},
    "professional": {"users": 50, "documents": 50_000, "ai_calls": 100_000, "storage_gb": 500},
    "enterprise":   {"users": -1, "documents": -1,     "ai_calls": -1,      "storage_gb": -1},
}
PLAN_LABELS = {"trial": "Free Trial", "basic": "Basic", "professional": "Professional", "enterprise": "Enterprise"}

ALL_MODULES = [
    "DOCUMENT_INTELLIGENCE", "OCR", "AI_EXTRACTION", "TRANSFORMATION", "VALIDATION",
    "RECONCILIATION", "SQL_WORKSPACE", "DATA_CATALOG", "LINEAGE", "PIPELINE_SCHEDULER",
    "OBSERVABILITY", "INCIDENTS",
]

# module -> which PLAN_LIMITS resource its usage counts against, or None
# when this codebase has nothing real to count for it yet.
MODULE_USAGE_RESOURCE: dict[str, str | None] = {
    "DOCUMENT_INTELLIGENCE": "documents",
    "OCR": "documents",
    "AI_EXTRACTION": "ai_calls",
    "TRANSFORMATION": None,
    "VALIDATION": None,
    "RECONCILIATION": None,
    "SQL_WORKSPACE": None,
    "DATA_CATALOG": None,
    "LINEAGE": None,
    "PIPELINE_SCHEDULER": None,
    "OBSERVABILITY": None,
    "INCIDENTS": None,
}


def _limit_for(plan: str, resource: str) -> int | None:
    raw = PLAN_LIMITS.get(plan, PLAN_LIMITS["trial"]).get(resource)
    return None if raw is None or raw < 0 else raw


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_utc_opt(dt: datetime | None) -> datetime | None:
    return _as_utc(dt) if dt else None


def _expiry_for(org: Organization) -> datetime | None:
    if org.license_status == LicenseStatus.TRIAL:
        return _as_utc_opt(org.trial_expires_at)
    return _as_utc_opt(org.license_expires_at) or _as_utc_opt(org.trial_expires_at)


def _days_remaining(org: Organization) -> int | None:
    expiry = _expiry_for(org)
    if expiry is None:
        return None
    delta = expiry - datetime.now(timezone.utc)
    return max(0, delta.days)


async def _count(db: AsyncSession, stmt) -> int:
    result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    return result.scalar_one()


async def _users_used(db: AsyncSession, organization_id: int | None) -> int:
    # User is PlatformBase — always the platform session, never tenant-routed.
    return await _count(db, select(User).where(User.organization_id == organization_id))


async def _documents_used(tenant_db: AsyncSession, organization_id: int | None) -> int:
    # Document is TenantBase — for a migrated org this MUST be the
    # tenant-routed session, not the platform one. The shared platform
    # database still physically has this org's OLD Document rows after a
    # migration (Bring-Your-Own-Database's copy step never deletes source
    # rows), so querying the wrong session here wouldn't error — it would
    # just silently return stale usage numbers frozen at the migration date.
    return await _count(tenant_db, select(Document).where(Document.organization_id == organization_id))


async def _ocr_used(tenant_db: AsyncSession, organization_id: int | None) -> int:
    return await _count(tenant_db, select(Document).where(Document.organization_id == organization_id, Document.ocr_percent.isnot(None)))


async def _ai_extraction_used(tenant_db: AsyncSession, organization_id: int | None) -> int:
    return await _count(tenant_db, select(Document).where(Document.organization_id == organization_id, Document.extraction_percent.isnot(None)))


async def _storage_used_gb(tenant_db: AsyncSession, organization_id: int | None) -> float:
    result = await tenant_db.execute(select(func.coalesce(func.sum(Document.size_bytes), 0)).where(Document.organization_id == organization_id))
    return round((result.scalar_one() or 0) / 1_000_000_000, 3)


async def build_usage(db: AsyncSession, tenant_db: AsyncSession, org: Organization) -> dict[str, dict]:
    users_used = await _users_used(db, org.id)
    documents_used = await _documents_used(tenant_db, org.id)
    ai_used = await _ai_extraction_used(tenant_db, org.id)
    storage_used = await _storage_used_gb(tenant_db, org.id)

    return {
        "users":      {"used": users_used, "limit": org.max_users if org.max_users and org.max_users > 0 else None},
        "documents":  {"used": documents_used, "limit": _limit_for(org.plan, "documents")},
        "ai_calls":   {"used": ai_used, "limit": _limit_for(org.plan, "ai_calls")},
        "storage_gb": {"used": storage_used, "limit": _limit_for(org.plan, "storage_gb")},
    }


async def build_dashboard(db: AsyncSession, tenant_db: AsyncSession, org: Organization) -> dict:
    usage = await build_usage(db, tenant_db, org)
    return {
        "organization_id": org.id,
        "license_status": org.license_status.value if hasattr(org.license_status, "value") else org.license_status,
        "plan": org.plan,
        "plan_label": PLAN_LABELS.get(org.plan, org.plan.title()),
        "start_date": _as_utc(org.created_at),
        "expiry_date": _expiry_for(org),
        "days_remaining": _days_remaining(org),
        "users_allowed": org.max_users,
        "users_used": usage["users"]["used"],
        "modules_purchased": len(org.licensed_modules or []),
        "modules_total": len(ALL_MODULES),
        "usage": usage,
    }


async def build_module_licenses(tenant_db: AsyncSession, org: Organization) -> list[dict]:
    licensed = set(org.licensed_modules or [])
    is_enabled_state = org.license_status not in (LicenseStatus.EXPIRED, LicenseStatus.SUSPENDED)
    expiry = _expiry_for(org)

    items = []
    for module in ALL_MODULES:
        purchased = module in licensed
        resource = MODULE_USAGE_RESOURCE.get(module)
        usage = limit = None
        if purchased and resource:
            if resource == "documents":
                usage = await (_ocr_used(tenant_db, org.id) if module == "OCR" else _documents_used(tenant_db, org.id))
            elif resource == "ai_calls":
                usage = await _ai_extraction_used(tenant_db, org.id)
            limit = _limit_for(org.plan, resource)
        items.append({
            "module": module,
            "purchased": purchased,
            "enabled": purchased and is_enabled_state,
            "usage": usage,
            "limit": limit,
            "expiry": expiry if purchased else None,
        })
    return items


async def build_status(db: AsyncSession, org: Organization) -> dict:
    return {
        "status": org.license_status.value if hasattr(org.license_status, "value") else org.license_status,
        "is_trial": org.license_status == LicenseStatus.TRIAL,
        "trial_start": _as_utc(org.created_at) if org.license_status == LicenseStatus.TRIAL else None,
        "trial_end": _as_utc_opt(org.trial_expires_at),
        "days_remaining": _days_remaining(org),
        "usage_limits": {k: _limit_for(org.plan, k) for k in ("users", "documents", "ai_calls", "storage_gb")},
    }


async def contact_sales(
    db: AsyncSession, org: Organization, actor: User, *, module_interest: str | None,
    contact_name: str, contact_email: str, contact_phone: str | None, message: str,
    ip_address: str | None = None,
) -> dict:
    """Org admin -> Seller module-purchase inquiry (Administration -> Licenses'
    "Contact Sales" form). There's no per-customer sales-rep assignment in this
    build, so every inquiry goes to the singleton Seller's billing/support
    inbox (same "one Seller" assumption as models/seller.py) — reuses the
    existing EmailService rather than a new mailer, and is recorded on
    SellerAuditLog (seller_user_id=None, since no Seller-tier user initiated
    it) so it surfaces in Seller Audit Logs / Seller Dashboard like every
    other customer-facing event."""
    from app.services import email_service

    result = await db.execute(select(Seller).limit(1))
    seller = result.scalar_one_or_none()
    to_email = (seller.billing_email or seller.support_email) if seller else None

    if not to_email:
        delivery = {"delivered": False, "detail": "No Seller contact email is configured yet — reach your Seller directly."}
    else:
        subject, body_text = email_service.build_sales_inquiry_email(
            org_name=org.name, module_interest=module_interest, contact_name=contact_name,
            contact_email=contact_email, contact_phone=contact_phone, message=message,
        )
        email_result = await email_service.send_email(db, to=to_email, subject=subject, body=body_text)
        delivery = email_result.to_dict()

    await record_seller_event(
        db, seller_user_id=None, customer_id=org.id, action="Module Purchase Inquiry",
        resource=contact_email, resource_id=str(org.id), ip_address=ip_address,
        details=f"{module_interest or 'General inquiry'} — {message}"[:2000],
        after_value={"delivered": delivery["delivered"], "contact_name": contact_name, "contact_phone": contact_phone},
    )
    return delivery
