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
from app.database import tenant_engine_registry
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

    # One extra query for this page's orgs' most recent Invoice each
    # (status + id), rather than N+1 — same pattern subscription_service.
    # list_subscriptions() already uses (SB28). Drives the Customers
    # table's Payment Status column + "Pay" row action (pending_invoice_id).
    org_ids = [org.id for org in orgs]
    payment_by_org: dict[int, dict] = {}
    if org_ids:
        from app.models.billing.invoice import Invoice, InvoiceStatus
        inv_result = await db.execute(
            select(Invoice.organization_id, Invoice.id, Invoice.status, Invoice.issue_date)
            .where(Invoice.organization_id.in_(org_ids))
            .order_by(Invoice.issue_date.desc())
        )
        for org_id, inv_id, inv_status, _issue_date in inv_result.all():
            if org_id in payment_by_org:
                continue  # already have this org's most recent invoice (rows are ordered desc)
            payment_by_org[org_id] = {
                "payment_status": inv_status.value,
                "pending_invoice_id": inv_id if inv_status in (InvoiceStatus.PAYMENT_PENDING, InvoiceStatus.OVERDUE, InvoiceStatus.PARTIALLY_PAID) else None,
            }

    items = []
    for org in orgs:
        sub = await _latest_subscription(db, org.id)
        users_count = await _users_count(db, org.id)
        payment = payment_by_org.get(org.id, {"payment_status": None, "pending_invoice_id": None})
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
            "payment_status": payment["payment_status"],
            "pending_invoice_id": payment["pending_invoice_id"],
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

    # license_service.build_usage()/build_module_licenses() read the
    # CUSTOMER's own tenant database (document/record counts live there,
    # not in the platform db this function otherwise runs against) — a
    # Seller has no request-scoped tenant_db (they aren't attached to any
    # one org), so resolve it explicitly the same way background tasks
    # do (see tenant_engine_registry.get_background_sessionmaker's
    # docstring) rather than the get_tenant_db request dependency.
    tenant_sessionmaker = await tenant_engine_registry.get_background_sessionmaker(org.id)
    async with tenant_sessionmaker() as tenant_db:
        usage = await license_service.build_usage(db, tenant_db, org)
        modules = await license_service.build_module_licenses(tenant_db, org)
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

    company_name = None
    if org.company_id:
        from app.models.company import Company
        company = await db.get(Company, org.company_id)
        company_name = company.name if company else None

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
        "address_line": org.address_line,
        "city": org.city,
        "branch": org.branch,
        "postal_code": org.postal_code,
        "company_id": org.company_id,
        "company_name": company_name,
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
    # Without a real email there is nowhere to send login credentials —
    # the org would be created with no way for anyone to actually sign
    # into it. Reject outright rather than silently onboarding a customer
    # nobody can log in as.
    if not body.primary_contact_email:
        raise CustomerServiceError("A primary contact email is required — without one, login credentials can't be generated or delivered.")

    slug = _slugify(body.organization_name)
    existing = await db.execute(select(Organization).where(Organization.slug == slug))
    if existing.scalar_one_or_none():
        raise CustomerServiceError("An organization with this name already exists.")

    # "custom" is not a real Plan row — it means "price these individual
    # modules from the catalog instead of a fixed bundle" (frontend Plan
    # dropdown's Custom option). Anything else must resolve to a real Plan.
    plan = None
    is_custom_plan = body.plan_code == "custom"
    if body.plan_code and not is_custom_plan:
        plan_result = await db.execute(select(Plan).where(Plan.code == body.plan_code))
        plan = plan_result.scalar_one_or_none()
        if plan is None:
            raise CustomerServiceError(f"Unknown plan: {body.plan_code}")

    license_status = LicenseStatus(body.status) if body.status else LicenseStatus.TRIAL

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
        address_line=body.address_line,
        city=body.city,
        branch=body.branch,
        postal_code=body.postal_code,
        company_id=body.company_id,
        # Placeholder — subscription_service.create_subscription() (below)
        # immediately overwrites license_status/plan/licensed_modules/
        # max_users/*_expires_at via its own _sync_org_from_subscription(),
        # derived from the real Subscription it creates, not guessed here.
        license_status=LicenseStatus.TRIAL,
        plan=body.plan_code or "trial",
        licensed_modules=[],
        max_users=5,
        is_active=license_status != LicenseStatus.SUSPENDED,
    )
    db.add(org)
    await db.flush()

    # Delegate the actual Subscription + CustomerModule entitlements +
    # Invoice creation to subscription_service.create_subscription() — the
    # single, already-correct implementation of this (reused, not
    # duplicated, to avoid the exact "two divergent paths" bug class found
    # live here before: a hand-rolled inline Subscription block in this
    # function once created a Subscription with no CustomerModule rows at
    # all, and separately never generated an invoice for a new customer).
    # This also means new paid (non-trial) customers now correctly start
    # PENDING — no module access — until a real payment is recorded; only
    # a TRIAL request activates immediately (see create_subscription()'s
    # own gating docstring).
    from app.schemas.seller.subscriptions import CreateSubscriptionRequest
    from app.services.seller import subscription_service
    sub_body = CreateSubscriptionRequest(
        organization_id=org.id,
        plan_id=plan.id if plan else None,
        status=SubscriptionStatus(license_status.value.upper()).value,
        start_date=body.start_date,
        end_date=body.expiry_date,
        billing_cycle=(plan.billing_cycle.value if plan else "MONTHLY"),
        amount=None,
        currency=(plan.currency if plan else "USD"),
        auto_renew=False,
        modules=(body.modules or []) if is_custom_plan else None,
    )
    sub = await subscription_service.create_subscription(db, body=sub_body, actor=actor)
    await db.refresh(org)

    temp_password = None
    admin_email = None
    if body.primary_contact_email:
        exists_user = await db.execute(select(User).where(User.email == body.primary_contact_email.lower()))
        if not exists_user.scalar_one_or_none():
            temp_password = _generate_temp_password()
            admin_email = body.primary_contact_email.lower()
            contact_user = User(
                email=admin_email,
                full_name=body.primary_contact_name or body.primary_contact_email,
                hashed_password=hash_password(temp_password),
                role=UserRole.ORG_ADMIN,
                is_active=True,
                is_verified=False,
                organization_id=org.id,
                must_change_password=True,
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

    email_result = None
    if temp_password and admin_email:
        from app.core.config import get_settings
        from app.services import email_service
        subject, body_text = email_service.build_welcome_email(
            org_name=org.name, full_name=body.primary_contact_name or admin_email,
            email=admin_email, temp_password=temp_password,
            login_url=f"{get_settings().frontend_base_url}/login",
        )
        email_result = await email_service.send_email(db, to=admin_email, subject=subject, body=body_text)
        await record_seller_event(
            db, seller_user_id=actor.id, customer_id=org.id, action="Customer Admin Welcome Email",
            resource=admin_email, resource_id=str(org.id), ip_address=ip_address,
            details=email_result.detail, after_value={"delivered": email_result.delivered},
        )

    return org, temp_password, admin_email, email_result


async def resend_admin_welcome_email(db: AsyncSession, org: Organization, actor: User, ip_address: str | None = None):
    """No plaintext temp password is ever persisted (it's shown once, same
    as user_admin_service.invite_user), so a genuine resend must issue a
    NEW one — the org's org_admin user gets their password reset and a
    fresh email sent. Picks the org's primary_contact_email if it matches
    a real org_admin user, else the first org_admin found."""
    from app.services import email_service

    stmt = select(User).where(User.organization_id == org.id, User.role == UserRole.ORG_ADMIN)
    admins = list((await db.execute(stmt)).scalars().all())
    if not admins:
        raise CustomerServiceError("This organization has no Admin user to resend credentials to.")
    target = next((u for u in admins if org.primary_contact_email and u.email == org.primary_contact_email.lower()), admins[0])

    temp_password = _generate_temp_password()
    target.hashed_password = hash_password(temp_password)
    target.must_change_password = True
    db.add(UserActivityLog(
        user_id=target.id, event_type=UserActivityType.INVITED,
        message="Password reset and welcome email resent by Seller",
    ))
    await db.commit()

    from app.core.config import get_settings
    subject, body_text = email_service.build_welcome_email(
        org_name=org.name, full_name=target.full_name, email=target.email,
        temp_password=temp_password, login_url=f"{get_settings().frontend_base_url}/login",
    )
    email_result = await email_service.send_email(db, to=target.email, subject=subject, body=body_text)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="Customer Admin Welcome Email",
        resource=target.email, resource_id=str(org.id), ip_address=ip_address,
        details=f"Resent: {email_result.detail}", after_value={"delivered": email_result.delivered},
    )
    return target.email, temp_password, email_result


async def update_customer(db: AsyncSession, org: Organization, body, actor: User, ip_address: str | None = None) -> Organization:
    before = {"name": org.name, "industry": org.industry, "country": org.country}
    for field in (
        "name", "legal_name", "industry", "country", "timezone",
        "primary_contact_name", "primary_contact_email", "primary_contact_phone",
        "address_line", "city", "branch", "postal_code", "company_id",
        "max_users",
    ):
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
