"""
Seller Portal -> Customers (Phase 2). Customer == Organization (see
services/seller/plan_seed.py's module docstring / Seller Portal memory) —
no separate Customer table. This service is the Seller-facing read/write
surface over Organization + Subscription, cross-org by design (a Seller
manages every customer, unlike every other *_service.py in this codebase
which is scoped to the caller's own organization_id).
"""
import secrets
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.organization import LicenseStatus, Organization
from app.models.plan import Plan
from app.models.seller_audit_log import record_seller_event
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User, UserRole
from app.models.user_activity import UserActivityLog, UserActivityType
from app.services import license_service, user_admin_service
from app.services.audit_log_service import list_entries as list_org_audit_entries


class CustomerServiceError(Exception):
    pass


def _generate_temp_password() -> str:
    return secrets.token_urlsafe(9)


async def get_stats(db: AsyncSession) -> dict:
    orgs = (await db.execute(select(Organization))).scalars().all()
    return {
        "total": len(orgs),
        "active": sum(1 for o in orgs if o.license_status == LicenseStatus.ACTIVE),
        "trial": sum(1 for o in orgs if o.license_status == LicenseStatus.TRIAL),
        "suspended": sum(1 for o in orgs if o.license_status == LicenseStatus.SUSPENDED),
        "expired": sum(1 for o in orgs if o.license_status == LicenseStatus.EXPIRED),
    }


async def _latest_subscription(db: AsyncSession, organization_id: int) -> Subscription | None:
    result = await db.execute(
        select(Subscription).where(Subscription.organization_id == organization_id)
        .order_by(Subscription.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _users_count(db: AsyncSession, organization_id: int) -> int:
    result = await db.execute(select(func.count()).select_from(select(User).where(User.organization_id == organization_id).subquery()))
    return result.scalar_one()


async def list_customers(
    db: AsyncSession, *, search: str = "", status_filter: str | None = None, plan_filter: str | None = None,
    page: int = 1, page_size: int = 20,
) -> tuple[list[dict], int]:
    stmt = select(Organization)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Organization.name.ilike(like), Organization.legal_name.ilike(like), Organization.primary_contact_email.ilike(like)))
    if status_filter:
        stmt = stmt.where(Organization.license_status == status_filter)
    if plan_filter:
        stmt = stmt.where(Organization.plan == plan_filter)
    stmt = stmt.order_by(Organization.created_at.desc())

    count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = count_result.scalar_one()

    result = await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    orgs = result.scalars().all()

    items = []
    for org in orgs:
        sub = await _latest_subscription(db, org.id)
        users_count = await _users_count(db, org.id)
        items.append({
            "id": org.id,
            "name": org.name,
            "legal_name": org.legal_name,
            "primary_contact_name": org.primary_contact_name,
            "primary_contact_email": org.primary_contact_email,
            "plan": org.plan,
            "license_status": org.license_status.value,
            "users_count": users_count,
            "max_users": org.max_users,
            "modules_count": len(org.licensed_modules or []),
            "subscription_status": sub.status.value if sub else None,
            "subscription_end": sub.end_date if sub else (org.license_expires_at or org.trial_expires_at),
            "created_at": org.created_at,
            "is_active": org.is_active,
        })
    return items, total


async def get_customer_detail(db: AsyncSession, customer_id: int) -> dict | None:
    org = await db.get(Organization, customer_id)
    if org is None:
        return None

    sub = await _latest_subscription(db, org.id)
    all_subs_result = await db.execute(
        select(Subscription).where(Subscription.organization_id == org.id).order_by(Subscription.created_at.desc())
    )
    subs = all_subs_result.scalars().all()

    usage = await license_service.build_usage(db, org)
    modules = await license_service.build_module_licenses(db, org)
    users_result = await db.execute(select(User).where(User.organization_id == org.id).order_by(User.created_at.asc()))
    users = users_result.scalars().all()

    activity_result = await db.execute(
        select(UserActivityLog, User.full_name)
        .join(User, User.id == UserActivityLog.user_id)
        .where(User.organization_id == org.id)
        .order_by(UserActivityLog.created_at.desc()).limit(15)
    )
    recent_activity = [
        {"user_name": name, "event_type": log.event_type.value, "message": log.message, "timestamp": log.created_at}
        for log, name in activity_result.all()
    ]

    return {
        "id": org.id,
        "name": org.name,
        "legal_name": org.legal_name,
        "slug": org.slug,
        "industry": org.industry,
        "country": org.country,
        "timezone": org.timezone,
        "primary_contact_name": org.primary_contact_name,
        "primary_contact_email": org.primary_contact_email,
        "primary_contact_phone": org.primary_contact_phone,
        "plan": org.plan,
        "license_status": org.license_status.value,
        "is_active": org.is_active,
        "max_users": org.max_users,
        "users_count": len(users),
        "licensed_modules": org.licensed_modules or [],
        "created_at": org.created_at,
        "trial_expires_at": org.trial_expires_at,
        "license_expires_at": org.license_expires_at,
        "subscription": {
            "id": sub.id, "status": sub.status.value, "plan_id": sub.plan_id,
            "start_date": sub.start_date, "end_date": sub.end_date,
            "billing_cycle": sub.billing_cycle.value, "amount": sub.amount,
            "currency": sub.currency, "auto_renew": sub.auto_renew,
        } if sub else None,
        "subscription_history": [
            {"id": s.id, "status": s.status.value, "start_date": s.start_date, "end_date": s.end_date, "amount": s.amount, "billing_cycle": s.billing_cycle.value}
            for s in subs
        ],
        "usage": usage,
        "modules": modules,
        "users": [
            {"id": u.id, "full_name": u.full_name, "email": u.email, "role": u.role.value, "status": user_admin_service.status_of(u), "last_login": u.last_login}
            for u in users
        ],
        "recent_activity": recent_activity,
    }


def _slugify(name: str) -> str:
    return name.lower().replace(" ", "-")[:50]


async def create_customer(db: AsyncSession, *, body, actor: User, ip_address: str | None = None) -> tuple[Organization, str | None]:
    slug = _slugify(body.organization_name)
    existing = await db.execute(select(Organization).where(Organization.slug == slug))
    if existing.scalar_one_or_none():
        raise CustomerServiceError("An organization with this name already exists.")

    plan = None
    if body.plan_code:
        plan_result = await db.execute(select(Plan).where(Plan.code == body.plan_code))
        plan = plan_result.scalar_one_or_none()
        if plan is None:
            raise CustomerServiceError(f"Unknown plan: {body.plan_code}")

    license_status = LicenseStatus(body.status) if body.status else LicenseStatus.TRIAL
    now = datetime.now(timezone.utc)
    trial_expires = body.expiry_date if license_status == LicenseStatus.TRIAL else None
    license_expires = body.expiry_date if license_status != LicenseStatus.TRIAL else None

    org = Organization(
        name=body.organization_name,
        legal_name=body.legal_name,
        slug=slug,
        country=body.country,
        industry=body.industry,
        timezone=body.timezone,
        primary_contact_name=body.primary_contact_name,
        primary_contact_email=body.primary_contact_email,
        primary_contact_phone=body.primary_contact_phone,
        license_status=license_status,
        plan=body.plan_code or "trial",
        licensed_modules=plan.modules if plan else [],
        max_users=(plan.limits or {}).get("users", 5) if plan else 5,
        trial_expires_at=trial_expires,
        license_expires_at=license_expires,
        is_active=license_status != LicenseStatus.SUSPENDED,
    )
    db.add(org)
    await db.flush()

    from app.models.plan import BillingCycle
    from app.models.seller import Seller
    seller_row = (await db.execute(select(Seller).limit(1))).scalar_one_or_none()

    sub = Subscription(
        seller_id=seller_row.id if seller_row else None,
        organization_id=org.id,
        plan_id=plan.id if plan else None,
        status=SubscriptionStatus(license_status.value.upper()),
        start_date=body.start_date or now,
        end_date=body.expiry_date,
        billing_cycle=plan.billing_cycle if plan else BillingCycle.MONTHLY,
        amount=plan.price if (plan and license_status != LicenseStatus.TRIAL) else 0.0,
        currency=plan.currency if plan else "USD",
        auto_renew=False,
        created_by=actor.id,
    )
    db.add(sub)

    temp_password = None
    if body.primary_contact_email:
        exists_user = await db.execute(select(User).where(User.email == body.primary_contact_email.lower()))
        if not exists_user.scalar_one_or_none():
            temp_password = _generate_temp_password()
            contact_user = User(
                email=body.primary_contact_email.lower(),
                full_name=body.primary_contact_name or body.primary_contact_email,
                hashed_password=hash_password(temp_password),
                role=UserRole.ORG_ADMIN,
                is_active=True,
                is_verified=False,
                organization_id=org.id,
            )
            db.add(contact_user)
            await db.flush()
            db.add(UserActivityLog(
                user_id=contact_user.id, event_type=UserActivityType.INVITED,
                message="Account created by Seller when the organization was onboarded",
            ))

    await db.commit()
    await db.refresh(org)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="Customer Created",
        resource=org.name, resource_id=str(org.id), ip_address=ip_address,
        after_value={"name": org.name, "plan": org.plan, "status": org.license_status.value},
    )
    return org, temp_password


async def update_customer(db: AsyncSession, org: Organization, body, actor: User, ip_address: str | None = None) -> Organization:
    before = {"name": org.name, "industry": org.industry, "country": org.country}
    for field in ("name", "legal_name", "industry", "country", "timezone", "primary_contact_name", "primary_contact_email", "primary_contact_phone", "max_users"):
        value = getattr(body, field, None)
        if value is not None:
            setattr(org, field, value)
    await db.commit()
    await db.refresh(org)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="Customer Updated",
        resource=org.name, resource_id=str(org.id), ip_address=ip_address,
        before_value=before, after_value={"name": org.name, "industry": org.industry, "country": org.country},
    )
    return org


async def set_status(db: AsyncSession, org: Organization, *, active: bool, actor: User, ip_address: str | None = None) -> Organization:
    before_status = org.license_status.value
    org.is_active = active
    org.license_status = LicenseStatus.ACTIVE if active else LicenseStatus.SUSPENDED
    await db.commit()
    await db.refresh(org)

    sub = await _latest_subscription(db, org.id)
    if sub:
        sub.status = SubscriptionStatus.ACTIVE if active else SubscriptionStatus.SUSPENDED
        await db.commit()

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id,
        action="Customer Activated" if active else "Customer Suspended",
        resource=org.name, resource_id=str(org.id), ip_address=ip_address,
        before_value={"license_status": before_status}, after_value={"license_status": org.license_status.value},
    )
    return org


async def impersonate(db: AsyncSession, org: Organization, *, actor: User, ip_address: str | None = None, user_agent: str | None = None) -> dict:
    """
    Issues a real, short-lived (15 min, no refresh token) access token for
    the customer's admin account, with the impersonating Seller user's id
    embedded in the token's own claims (impersonated_by) so every request
    made during the session is independently traceable back to who
    initiated it — not just the one audit-log row at issuance time.
    Deliberately no refresh token: an expired impersonation session must
    go back through this (audited) endpoint to renew, rather than silently
    refreshing itself indefinitely. SELLER PORTAL spec section 1:
    "Impersonation must be heavily audited. Never allow silent impersonation."
    """
    from datetime import timedelta
    from app.core.security import _create_token

    target_result = await db.execute(
        select(User).where(User.organization_id == org.id, User.role == UserRole.ORG_ADMIN, User.is_active.is_(True))
        .order_by(User.created_at.asc()).limit(1)
    )
    target = target_result.scalar_one_or_none()
    if target is None:
        raise CustomerServiceError("This customer has no active admin user to impersonate.")

    extra = {"role": target.role.value, "org_id": target.organization_id, "impersonated_by": actor.id, "impersonation": True}
    access = _create_token({"sub": str(target.id), "type": "access", **extra}, timedelta(minutes=15))

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="Impersonation",
        resource=target.email, resource_id=str(target.id), ip_address=ip_address,
        details=f"{actor.email} impersonated {target.email} ({target.role.value}) at {org.name}",
        after_value={"target_user_id": target.id, "target_email": target.email},
    )
    return {
        "access_token": access, "token_type": "bearer",
        "target_user_id": target.id, "target_email": target.email,
        "user": {
            "id": target.id, "email": target.email, "full_name": target.full_name, "role": target.role.value,
            "is_active": target.is_active, "is_verified": target.is_verified, "organization_id": target.organization_id,
            "created_at": target.created_at, "accessible_modules": target.accessible_modules,
        },
        "organization": {
            "id": org.id, "name": org.name, "slug": org.slug, "license_status": org.license_status.value,
            "plan": org.plan, "licensed_modules": org.licensed_modules or [], "max_users": org.max_users,
        },
    }


async def get_customer_audit(db: AsyncSession, customer_id: int, page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """Merges the org's own AuditLog feed (Administration -> Audit Logs source)
    with SellerAuditLog entries scoped to this customer — the Audit tab
    shows both "what the org's own users did" and "what the Seller did to
    this account", clearly labeled."""
    from app.models.seller_audit_log import SellerAuditLog

    org_entries, _ = await list_org_audit_entries(db, organization_id=customer_id, page=1, page_size=1000)
    for e in org_entries:
        e["source"] = "organization"

    seller_result = await db.execute(
        select(SellerAuditLog).where(SellerAuditLog.customer_id == customer_id).order_by(SellerAuditLog.timestamp.desc())
    )
    seller_entries = [
        {
            # SQLite drops tzinfo on round-trip even for DateTime(timezone=True)
            # columns — normalize to aware UTC so this sorts against
            # audit_log_service's already-normalized org_entries without
            # TypeError: can't compare offset-naive and offset-aware datetimes.
            "id": f"seller-{row.id}",
            "timestamp": row.timestamp if row.timestamp.tzinfo else row.timestamp.replace(tzinfo=timezone.utc),
            "user_name": None, "action": row.action,
            "module": row.module or "Seller", "resource": row.resource, "status": "SUCCESS", "source": "seller",
        }
        for row in seller_result.scalars().all()
    ]

    combined = sorted(org_entries + seller_entries, key=lambda e: e["timestamp"], reverse=True)
    total = len(combined)
    start = (page - 1) * page_size
    return combined[start:start + page_size], total
