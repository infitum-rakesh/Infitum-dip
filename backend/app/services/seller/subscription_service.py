"""
Seller Portal -> Subscriptions (Phase 3). Owns two related surfaces per the
spec's section 9 file layout (no separate plan_service.py listed there):

  - Plan CRUD — the configurable plan catalog (SELLER PORTAL spec section 2:
    "Support configurable plans... Do NOT hardcode these limits. Store
    plans and limits in the backend"). Seeded starters live in
    services/seller/plan_seed.py; this module is where a Seller edits them
    or adds new (custom) plans for real.
  - Subscription lifecycle — Create/Edit/Renew/Upgrade/Downgrade/Suspend/
    Resume/Cancel. Every mutation keeps the linked Organization's
    license_status/plan/licensed_modules/*_expires_at fields in sync
    (_sync_org_from_subscription) — those Organization fields are what the
    REST of the app actually reads for gating (core/deps.require_action,
    frontend evaluateAccess()), so Subscription is the system of record but
    Organization must never drift out of sync with it. Every mutation also
    writes a SellerAuditLog event (spec: "Every subscription change must
    create an audit event").
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import LicenseStatus, Organization
from app.models.plan import BillingCycle, Plan
from app.models.seller import Seller
from app.models.seller_audit_log import record_seller_event
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User

_SUB_TO_LICENSE_STATUS = {
    SubscriptionStatus.TRIAL: LicenseStatus.TRIAL,
    SubscriptionStatus.ACTIVE: LicenseStatus.ACTIVE,
    SubscriptionStatus.EXPIRING: LicenseStatus.EXPIRING,
    SubscriptionStatus.EXPIRED: LicenseStatus.EXPIRED,
    SubscriptionStatus.SUSPENDED: LicenseStatus.SUSPENDED,
    SubscriptionStatus.CANCELLED: LicenseStatus.EXPIRED,  # no more service — same customer-facing state as EXPIRED
}


class SubscriptionServiceError(Exception):
    pass


# ── Plans ────────────────────────────────────────────────────────

async def list_plans(db: AsyncSession, *, include_inactive: bool = True) -> list[Plan]:
    stmt = select(Plan).order_by(Plan.price.asc())
    if not include_inactive:
        stmt = stmt.where(Plan.is_active.is_(True))
    return list((await db.execute(stmt)).scalars().all())


async def get_plan(db: AsyncSession, plan_id: int) -> Plan | None:
    return await db.get(Plan, plan_id)


async def create_plan(db: AsyncSession, *, body, actor: User) -> Plan:
    existing = await db.execute(select(Plan).where(Plan.code == body.code))
    if existing.scalar_one_or_none():
        raise SubscriptionServiceError(f"A plan with code '{body.code}' already exists.")
    seller_row = (await db.execute(select(Seller).limit(1))).scalar_one_or_none()
    plan = Plan(
        seller_id=seller_row.id if seller_row else None,
        code=body.code, name=body.name, description=body.description,
        modules=body.modules, limits=body.limits, price=body.price, currency=body.currency,
        billing_cycle=BillingCycle(body.billing_cycle), trial_days=body.trial_days,
        is_system=False, is_active=True,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Subscription Changed",
        module="Plans", resource=plan.name, resource_id=str(plan.id),
        details=f"Plan '{plan.name}' ({plan.code}) created", after_value={"code": plan.code, "modules": plan.modules, "limits": plan.limits},
    )
    return plan


async def update_plan(db: AsyncSession, plan: Plan, *, body, actor: User) -> Plan:
    before = {"modules": plan.modules, "limits": plan.limits, "price": plan.price, "is_active": plan.is_active}
    for field in ("name", "description", "modules", "limits", "price", "currency", "trial_days", "is_active"):
        value = getattr(body, field, None)
        if value is not None:
            setattr(plan, field, value)
    if body.billing_cycle is not None:
        plan.billing_cycle = BillingCycle(body.billing_cycle)
    await db.commit()
    await db.refresh(plan)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Subscription Changed",
        module="Plans", resource=plan.name, resource_id=str(plan.id),
        details=f"Plan '{plan.name}' updated", before_value=before,
        after_value={"modules": plan.modules, "limits": plan.limits, "price": plan.price, "is_active": plan.is_active},
    )
    return plan


# ── Subscriptions ────────────────────────────────────────────────

async def _sync_org_from_subscription(db: AsyncSession, org: Organization, sub: Subscription, plan: Plan | None) -> None:
    org.license_status = _SUB_TO_LICENSE_STATUS[sub.status]
    org.is_active = sub.status != SubscriptionStatus.SUSPENDED
    if plan:
        org.plan = plan.code
        org.licensed_modules = plan.modules or []
        users_limit = (plan.limits or {}).get("users")
        if users_limit is not None:
            org.max_users = users_limit
    if sub.status == SubscriptionStatus.TRIAL:
        org.trial_expires_at = sub.end_date
    else:
        org.license_expires_at = sub.end_date
    await db.commit()


async def list_subscriptions(
    db: AsyncSession, *, customer_id: int | None = None, status_filter: str | None = None,
    search: str = "", page: int = 1, page_size: int = 20,
) -> tuple[list[dict], int]:
    stmt = select(Subscription, Organization, Plan).join(
        Organization, Organization.id == Subscription.organization_id
    ).outerjoin(Plan, Plan.id == Subscription.plan_id)

    if customer_id is not None:
        stmt = stmt.where(Subscription.organization_id == customer_id)
    if status_filter:
        stmt = stmt.where(Subscription.status == status_filter)
    if search:
        stmt = stmt.where(Organization.name.ilike(f"%{search}%"))
    stmt = stmt.order_by(Subscription.created_at.desc())

    count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = count_result.scalar_one()

    result = await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    items = []
    for sub, org, plan in result.all():
        items.append({
            "id": sub.id, "organization_id": org.id, "organization_name": org.name,
            "plan_id": sub.plan_id, "plan_name": plan.name if plan else sub.plan_id and "Unknown", "plan_code": plan.code if plan else None,
            "status": sub.status.value, "start_date": sub.start_date, "end_date": sub.end_date,
            "billing_cycle": sub.billing_cycle.value, "amount": sub.amount, "currency": sub.currency,
            "auto_renew": sub.auto_renew, "created_at": sub.created_at,
        })
    return items, total


async def get_subscription(db: AsyncSession, subscription_id: int) -> Subscription | None:
    return await db.get(Subscription, subscription_id)


async def create_subscription(db: AsyncSession, *, body, actor: User) -> Subscription:
    org = await db.get(Organization, body.organization_id)
    if org is None:
        raise SubscriptionServiceError("Customer not found.")
    plan = await db.get(Plan, body.plan_id) if body.plan_id else None
    if body.plan_id and plan is None:
        raise SubscriptionServiceError("Plan not found.")

    seller_row = (await db.execute(select(Seller).limit(1))).scalar_one_or_none()
    sub = Subscription(
        seller_id=seller_row.id if seller_row else None,
        organization_id=org.id, plan_id=plan.id if plan else None,
        status=SubscriptionStatus(body.status), start_date=body.start_date or datetime.now(timezone.utc),
        end_date=body.end_date, billing_cycle=BillingCycle(body.billing_cycle),
        amount=body.amount if body.amount is not None else (plan.price if plan else 0.0),
        currency=body.currency or (plan.currency if plan else "USD"),
        auto_renew=body.auto_renew, created_by=actor.id,
    )
    db.add(sub)
    await db.flush()
    await _sync_org_from_subscription(db, org, sub, plan)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="Subscription Created",
        resource=org.name, resource_id=str(sub.id),
        after_value={"plan": plan.code if plan else None, "status": sub.status.value, "amount": sub.amount},
    )
    return sub


async def _org_and_plan_for(db: AsyncSession, sub: Subscription) -> tuple[Organization, Plan | None]:
    org = await db.get(Organization, sub.organization_id)
    plan = await db.get(Plan, sub.plan_id) if sub.plan_id else None
    return org, plan


async def update_subscription(db: AsyncSession, sub: Subscription, *, body, actor: User) -> Subscription:
    before = {"end_date": sub.end_date.isoformat() if sub.end_date else None, "amount": sub.amount, "auto_renew": sub.auto_renew}
    for field in ("end_date", "amount", "currency", "auto_renew"):
        value = getattr(body, field, None)
        if value is not None:
            setattr(sub, field, value)
    if body.billing_cycle is not None:
        sub.billing_cycle = BillingCycle(body.billing_cycle)
    await db.commit()

    org, plan = await _org_and_plan_for(db, sub)
    await _sync_org_from_subscription(db, org, sub, plan)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=sub.organization_id, action="Subscription Changed",
        resource=org.name, resource_id=str(sub.id), details="Subscription details updated",
        before_value=before, after_value={"end_date": sub.end_date.isoformat() if sub.end_date else None, "amount": sub.amount, "auto_renew": sub.auto_renew},
    )
    return sub


_CYCLE_DELTA = {
    BillingCycle.MONTHLY: timedelta(days=30),
    BillingCycle.ANNUAL: timedelta(days=365),
}


async def renew_subscription(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    now = datetime.now(timezone.utc)
    base = sub.end_date if sub.end_date and sub.end_date > now else now
    base = base if base.tzinfo else base.replace(tzinfo=timezone.utc)
    delta = _CYCLE_DELTA.get(sub.billing_cycle, timedelta(days=30))
    before_end = sub.end_date
    sub.end_date = base + delta
    sub.status = SubscriptionStatus.ACTIVE
    await db.commit()

    org, plan = await _org_and_plan_for(db, sub)
    await _sync_org_from_subscription(db, org, sub, plan)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=sub.organization_id, action="Subscription Changed",
        resource=org.name, resource_id=str(sub.id), details=f"Renewed through {sub.end_date.date()}",
        before_value={"end_date": before_end.isoformat() if before_end else None},
        after_value={"end_date": sub.end_date.isoformat(), "status": sub.status.value},
    )
    return sub


async def change_plan(db: AsyncSession, sub: Subscription, *, new_plan_id: int, actor: User) -> Subscription:
    new_plan = await db.get(Plan, new_plan_id)
    if new_plan is None:
        raise SubscriptionServiceError("Plan not found.")
    old_plan = await db.get(Plan, sub.plan_id) if sub.plan_id else None
    direction = "Upgraded" if (old_plan is None or new_plan.price >= old_plan.price) else "Downgraded"

    before = {"plan": old_plan.code if old_plan else None, "amount": sub.amount}
    sub.plan_id = new_plan.id
    sub.amount = new_plan.price
    sub.currency = new_plan.currency
    if sub.status in (SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED):
        sub.status = SubscriptionStatus.ACTIVE
    await db.commit()

    org, _ = await _org_and_plan_for(db, sub)
    await _sync_org_from_subscription(db, org, sub, new_plan)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=sub.organization_id, action="Subscription Changed",
        resource=org.name, resource_id=str(sub.id), details=f"{direction} from {before['plan'] or 'no plan'} to {new_plan.code}",
        before_value=before, after_value={"plan": new_plan.code, "amount": sub.amount},
    )
    return sub


async def _set_status(db: AsyncSession, sub: Subscription, *, new_status: SubscriptionStatus, action_detail: str, actor: User) -> Subscription:
    before_status = sub.status.value
    sub.status = new_status
    if new_status == SubscriptionStatus.CANCELLED:
        sub.auto_renew = False
    await db.commit()

    org, plan = await _org_and_plan_for(db, sub)
    await _sync_org_from_subscription(db, org, sub, plan)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=sub.organization_id, action="Subscription Changed",
        resource=org.name, resource_id=str(sub.id), details=action_detail,
        before_value={"status": before_status}, after_value={"status": sub.status.value},
    )
    return sub


async def suspend_subscription(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    return await _set_status(db, sub, new_status=SubscriptionStatus.SUSPENDED, action_detail="Suspended", actor=actor)


async def resume_subscription(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    return await _set_status(db, sub, new_status=SubscriptionStatus.ACTIVE, action_detail="Resumed", actor=actor)


async def cancel_subscription(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    return await _set_status(db, sub, new_status=SubscriptionStatus.CANCELLED, action_detail="Cancelled", actor=actor)
