"""
Seller Portal -> License Management (Phase 5) — the license *lifecycle*
layer on top of Phase 4's `CustomerModule` rows (see that model's
docstring: this was designed from Phase 4 onward to double as the spec's
"License" entity rather than build a redundant second table).

Reuses module_service's MODULE_CATALOG/MODULE_DEPENDENCIES/_block_reason/
_latest_subscription rather than duplicating the "can this module be
enabled for this customer" business rule (customer suspended / subscription
expired / missing dependency) — Activate here IS that same rule.

Also owns check_license() — the real implementation behind the platform-
wide GET /api/license/check (spec section "LICENSE VALIDATION"). Not
wired into every existing require_action() call site yet (that's Phase 10
"Integrate RBAC + licensing + organization isolation" scope) but it's a
genuine, independently-callable, correctly-computing endpoint today.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer_module import CustomerModule
from app.models.organization import LicenseStatus, Organization
from app.models.plan import BillingCycle
from app.models.seller_audit_log import record_seller_event
from app.models.subscription import SubscriptionStatus
from app.models.user import User
from app.services import license_service as org_license_service  # top-level, non-seller — real usage counters
from app.services.seller.module_service import (
    MODULE_CATALOG,
    MODULE_DEPENDENCIES,
    _block_reason,
    _latest_subscription,
)

STATUSES = ["ACTIVE", "SUSPENDED", "EXPIRED", "REVOKED"]


class LicenseServiceError(Exception):
    pass


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _effective_status(row: CustomerModule) -> str:
    """REVOKED/SUSPENDED are explicit and win outright; otherwise a past
    expiry_date reads as EXPIRED even if nobody has explicitly flipped the
    stored status yet — same "compute on read" approach as the rest of
    this codebase's expiry handling (see dashboard_service/license_service)."""
    if row.status == "REVOKED":
        return "REVOKED"
    if row.status == "SUSPENDED":
        return "SUSPENDED"
    expiry = _as_utc(row.expiry_date)
    if expiry is not None and expiry < datetime.now(timezone.utc):
        return "EXPIRED"
    return row.status or "ACTIVE"


async def ensure_customer_modules_seeded(db: AsyncSession) -> None:
    """Idempotent backfill — every module already in an Organization's
    licensed_modules gets a real CustomerModule row (if it doesn't have one
    yet) so License Management shows the platform's actual current
    licensing, not just what's been explicitly touched via the UI so far."""
    orgs = (await db.execute(select(Organization))).scalars().all()
    existing_result = await db.execute(select(CustomerModule.organization_id, CustomerModule.module))
    existing = {(row[0], row[1]) for row in existing_result.all()}

    created_any = False
    for org in orgs:
        for module in (org.licensed_modules or []):
            if module not in MODULE_CATALOG:
                continue
            if (org.id, module) in existing:
                continue
            db.add(CustomerModule(
                organization_id=org.id, module=module, enabled=True, status="ACTIVE",
                license_type="SUBSCRIPTION",
                start_date=org.created_at if org.created_at.tzinfo else org.created_at.replace(tzinfo=timezone.utc),
            ))
            created_any = True
    if created_any:
        await db.commit()


async def get_stats(db: AsyncSession) -> dict:
    rows = (await db.execute(select(CustomerModule))).scalars().all()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=30)
    counts = {"total": len(rows), "active": 0, "expiring": 0, "expired": 0, "suspended": 0, "revoked": 0}
    for row in rows:
        eff = _effective_status(row)
        if eff == "ACTIVE":
            counts["active"] += 1
            expiry = _as_utc(row.expiry_date)
            if expiry is not None and now <= expiry <= horizon:
                counts["expiring"] += 1
        elif eff == "EXPIRED":
            counts["expired"] += 1
        elif eff == "SUSPENDED":
            counts["suspended"] += 1
        elif eff == "REVOKED":
            counts["revoked"] += 1
    return counts


async def list_licenses(
    db: AsyncSession, *, customer_id: int | None = None, status_filter: str | None = None,
    module_filter: str | None = None, page: int = 1, page_size: int = 20,
) -> tuple[list[dict], int]:
    stmt = select(CustomerModule, Organization).join(Organization, Organization.id == CustomerModule.organization_id)
    if customer_id is not None:
        stmt = stmt.where(CustomerModule.organization_id == customer_id)
    if module_filter:
        stmt = stmt.where(CustomerModule.module == module_filter)
    stmt = stmt.order_by(CustomerModule.created_at.desc())

    all_result = await db.execute(stmt)
    all_rows = all_result.all()

    items = []
    for row, org in all_rows:
        eff = _effective_status(row)
        if status_filter and eff != status_filter:
            continue
        items.append(_shape(row, org, eff))

    total = len(items)
    start = (page - 1) * page_size
    return items[start:start + page_size], total


def _shape(row: CustomerModule, org: Organization, effective_status: str) -> dict:
    usage_limit = row.monthly_credits if row.monthly_credits is not None else row.max_documents
    return {
        "id": row.id, "organization_id": org.id, "organization_name": org.name,
        "module": row.module, "module_label": MODULE_CATALOG.get(row.module, {}).get("label", row.module),
        "license_type": row.license_type, "start_date": row.start_date, "expiry_date": row.expiry_date,
        "max_users": row.max_users, "usage_limit": usage_limit, "status": effective_status,
        "monthly_credits": row.monthly_credits, "max_documents": row.max_documents,
        "max_records": row.max_records, "storage_limit_gb": row.storage_limit_gb,
    }


async def get_license(db: AsyncSession, license_id: int) -> CustomerModule | None:
    return await db.get(CustomerModule, license_id)


async def _org_for(db: AsyncSession, row: CustomerModule) -> Organization:
    org = await db.get(Organization, row.organization_id)
    if org is None:
        raise LicenseServiceError("Customer not found.")
    return org


async def _sync_org_licensed_modules(db: AsyncSession, org: Organization, module: str, enabled: bool) -> None:
    licensed = list(org.licensed_modules or [])
    if enabled and module not in licensed:
        licensed.append(module)
    elif not enabled and module in licensed:
        licensed.remove(module)
    org.licensed_modules = licensed
    await db.commit()


async def create_license(db: AsyncSession, *, body, actor: User, ip_address: str | None = None) -> CustomerModule:
    org = await db.get(Organization, body.organization_id)
    if org is None:
        raise LicenseServiceError("Customer not found.")
    if body.module not in MODULE_CATALOG:
        raise LicenseServiceError(f"'{body.module}' is not a module the Seller licenses on this platform.")

    existing = await db.execute(select(CustomerModule).where(CustomerModule.organization_id == body.organization_id, CustomerModule.module == body.module))
    if existing.scalar_one_or_none() is not None:
        raise LicenseServiceError(f"A license for {body.module} already exists for this customer — edit it instead.")

    sub = await _latest_subscription(db, body.organization_id)
    reason = _block_reason(org, sub, body.module)
    if reason:
        raise LicenseServiceError(reason)
    deps = MODULE_DEPENDENCIES.get(body.module, [])
    enabled_now = set(org.licensed_modules or [])
    missing = [d for d in deps if d not in enabled_now]
    if missing:
        missing_labels = ", ".join(MODULE_CATALOG.get(d, {}).get("label", d) for d in missing)
        raise LicenseServiceError(f"Cannot license {MODULE_CATALOG[body.module]['label']} — missing required module(s): {missing_labels}.")

    row = CustomerModule(
        organization_id=body.organization_id, module=body.module, enabled=True, status="ACTIVE",
        license_type=body.license_type, max_users=body.max_users, monthly_credits=body.ai_credit_limit,
        max_documents=body.document_limit, max_records=body.record_limit, storage_limit_gb=body.storage_limit_gb,
        start_date=body.start_date or datetime.now(timezone.utc), expiry_date=body.expiry_date,
        created_by=actor.id,
    )
    db.add(row)
    await _sync_org_licensed_modules(db, org, body.module, True)
    await db.refresh(row)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="License Created",
        module=body.module, resource=MODULE_CATALOG[body.module]["label"], resource_id=str(row.id),
        ip_address=ip_address, after_value={"license_type": row.license_type, "expiry_date": row.expiry_date.isoformat() if row.expiry_date else None},
    )
    return row


async def _lifecycle_action(db: AsyncSession, row: CustomerModule, *, new_status: str, enabled: bool, action: str, actor: User, ip_address: str | None, details: str | None = None, enforce_block: bool = False) -> CustomerModule:
    org = await _org_for(db, row)
    if enforce_block:
        sub = await _latest_subscription(db, org.id)
        reason = _block_reason(org, sub, row.module)
        if reason:
            raise LicenseServiceError(reason)
        deps = MODULE_DEPENDENCIES.get(row.module, [])
        enabled_now = set(org.licensed_modules or [])
        missing = [d for d in deps if d not in enabled_now]
        if missing:
            missing_labels = ", ".join(MODULE_CATALOG.get(d, {}).get("label", d) for d in missing)
            raise LicenseServiceError(f"Cannot activate — missing required module(s): {missing_labels}.")

    before_status = _effective_status(row)
    row.status = new_status
    row.enabled = enabled
    await db.commit()
    await _sync_org_licensed_modules(db, org, row.module, enabled)
    await db.refresh(row)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action=action,
        module=row.module, resource=MODULE_CATALOG.get(row.module, {}).get("label", row.module), resource_id=str(row.id),
        ip_address=ip_address, details=details,
        before_value={"status": before_status}, after_value={"status": _effective_status(row)},
    )
    return row


async def activate_license(db: AsyncSession, row: CustomerModule, *, actor: User, ip_address: str | None = None) -> CustomerModule:
    return await _lifecycle_action(db, row, new_status="ACTIVE", enabled=True, action="License Activated", actor=actor, ip_address=ip_address, enforce_block=True)


async def suspend_license(db: AsyncSession, row: CustomerModule, *, actor: User, ip_address: str | None = None) -> CustomerModule:
    return await _lifecycle_action(db, row, new_status="SUSPENDED", enabled=False, action="License Suspended", actor=actor, ip_address=ip_address)


async def revoke_license(db: AsyncSession, row: CustomerModule, *, actor: User, ip_address: str | None = None) -> CustomerModule:
    return await _lifecycle_action(db, row, new_status="REVOKED", enabled=False, action="License Revoked", actor=actor, ip_address=ip_address)


async def extend_license(db: AsyncSession, row: CustomerModule, *, days: int, actor: User, ip_address: str | None = None) -> CustomerModule:
    org = await _org_for(db, row)
    now = datetime.now(timezone.utc)
    base = _as_utc(row.expiry_date) if row.expiry_date and _as_utc(row.expiry_date) > now else now
    before_expiry = row.expiry_date
    row.expiry_date = base + timedelta(days=days)
    if row.status == "EXPIRED":
        row.status = "ACTIVE"
    await db.commit()
    await _sync_org_licensed_modules(db, org, row.module, row.status == "ACTIVE")
    await db.refresh(row)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="License Extended",
        module=row.module, resource=MODULE_CATALOG.get(row.module, {}).get("label", row.module), resource_id=str(row.id),
        ip_address=ip_address, details=f"Extended by {days} days, now through {row.expiry_date.date()}",
        before_value={"expiry_date": before_expiry.isoformat() if before_expiry else None},
        after_value={"expiry_date": row.expiry_date.isoformat()},
    )
    return row


_RENEWAL_DAYS = {"MONTHLY": 30, "ANNUAL": 365}


async def renew_license(db: AsyncSession, row: CustomerModule, *, actor: User, ip_address: str | None = None) -> CustomerModule:
    org = await _org_for(db, row)
    now = datetime.now(timezone.utc)
    base = _as_utc(row.expiry_date) if row.expiry_date and _as_utc(row.expiry_date) > now else now
    days = _RENEWAL_DAYS.get(row.license_type, 30)
    before_expiry = row.expiry_date
    row.expiry_date = base + timedelta(days=days)
    row.status = "ACTIVE"
    row.enabled = True
    await db.commit()
    await _sync_org_licensed_modules(db, org, row.module, True)
    await db.refresh(row)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="License Renewed",
        module=row.module, resource=MODULE_CATALOG.get(row.module, {}).get("label", row.module), resource_id=str(row.id),
        ip_address=ip_address, details=f"Renewed through {row.expiry_date.date()}",
        before_value={"expiry_date": before_expiry.isoformat() if before_expiry else None, "status": "renewed_from"},
        after_value={"expiry_date": row.expiry_date.isoformat(), "status": row.status},
    )
    return row


async def change_license_type(db: AsyncSession, row: CustomerModule, *, new_type: str, actor: User, ip_address: str | None = None) -> CustomerModule:
    org = await _org_for(db, row)
    before_type = row.license_type
    row.license_type = new_type
    await db.commit()
    await db.refresh(row)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="License Changed",
        module=row.module, resource=MODULE_CATALOG.get(row.module, {}).get("label", row.module), resource_id=str(row.id),
        ip_address=ip_address, details=f"License type changed from {before_type} to {new_type}",
        before_value={"license_type": before_type}, after_value={"license_type": new_type},
    )
    return row


# ── GET /api/license/check ──────────────────────────────────────

_QUOTA_MODULE_RESOURCE = {"AI_EXTRACTION": "ai_calls", "DOCUMENT_INTELLIGENCE": "documents", "OCR": "documents"}


async def _real_usage_for(db: AsyncSession, organization_id: int, module: str) -> int | None:
    if module == "AI_EXTRACTION":
        return await org_license_service._ai_extraction_used(db, organization_id)
    if module == "OCR":
        return await org_license_service._ocr_used(db, organization_id)
    if module == "DOCUMENT_INTELLIGENCE":
        return await org_license_service._documents_used(db, organization_id)
    return None


async def check_license(db: AsyncSession, *, customer_id: int, user_id: int | None, module: str) -> dict:
    org = await db.get(Organization, customer_id)
    if org is None:
        return {"allowed": False, "module": module, "reason": "LICENSE_NOT_FOUND", "expiresAt": None, "remainingQuota": None}

    if user_id is not None:
        user = await db.get(User, user_id)
        if user is None or user.organization_id != customer_id:
            return {"allowed": False, "module": module, "reason": "LICENSE_NOT_FOUND", "expiresAt": None, "remainingQuota": None}

    if org.license_status == LicenseStatus.SUSPENDED or not org.is_active:
        return {"allowed": False, "module": module, "reason": "CUSTOMER_SUSPENDED", "expiresAt": None, "remainingQuota": None}

    if module not in (org.licensed_modules or []):
        return {"allowed": False, "module": module, "reason": "MODULE_NOT_PURCHASED", "expiresAt": None, "remainingQuota": None}

    result = await db.execute(select(CustomerModule).where(CustomerModule.organization_id == customer_id, CustomerModule.module == module))
    row = result.scalar_one_or_none()

    expires_at = row.expiry_date if row else (org.license_expires_at or org.trial_expires_at)

    if row is not None:
        eff = _effective_status(row)
        if eff in ("EXPIRED", "REVOKED", "SUSPENDED"):
            return {"allowed": False, "module": module, "reason": "LICENSE_EXPIRED", "expiresAt": expires_at, "remainingQuota": None}

        if row.max_users is not None:
            users_count = await org_license_service._users_used(db, customer_id)
            if users_count > row.max_users:
                return {"allowed": False, "module": module, "reason": "USER_LIMIT_EXCEEDED", "expiresAt": expires_at, "remainingQuota": None}

        limit = row.monthly_credits if module == "AI_EXTRACTION" else row.max_documents
        if limit is not None:
            used = await _real_usage_for(db, customer_id, module)
            if used is not None and used >= limit:
                return {"allowed": False, "module": module, "reason": "QUOTA_EXCEEDED", "expiresAt": expires_at, "remainingQuota": max(0, limit - used)}

    remaining = None
    if row is not None:
        limit = row.monthly_credits if module == "AI_EXTRACTION" else row.max_documents
        if limit is not None:
            used = await _real_usage_for(db, customer_id, module)
            if used is not None:
                remaining = max(0, limit - used)

    return {"allowed": True, "module": module, "reason": "LICENSE_ACTIVE", "expiresAt": expires_at, "remainingQuota": remaining}
