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

from app.models.billing.invoice import Invoice
from app.models.customer_module import CustomerModule
from app.models.organization import LicenseStatus, Organization
from app.models.plan import BillingCycle, Plan
from app.models.seller import Seller
from app.models.seller_audit_log import record_seller_event
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User
from app.services.billing import invoice_service
from app.services.billing.access_service import access_service

# Organization.license_status only has 5 values (trial/active/expiring/
# expired/suspended, no CANCELLED/PENDING/etc) and is a DISPLAY cache only
# now (see access_service.py) — real enforcement evaluates real
# Subscription/CustomerModule rows per module, so an approximate mapping
# here is safe. PENDING/PAST_DUE/PAYMENT_PENDING/TERMINATED are new (2026
# Multi-Subscription Billing & Payments Platform, spec section 2).
_SUB_TO_LICENSE_STATUS = {
    SubscriptionStatus.TRIAL: LicenseStatus.TRIAL,
    SubscriptionStatus.PENDING: LicenseStatus.EXPIRED,       # created, not yet paid -> no access implied
    SubscriptionStatus.ACTIVE: LicenseStatus.ACTIVE,
    SubscriptionStatus.EXPIRING: LicenseStatus.EXPIRING,
    SubscriptionStatus.PAST_DUE: LicenseStatus.EXPIRING,     # grace period — still warn, not yet hard-denied
    SubscriptionStatus.PAYMENT_PENDING: LicenseStatus.EXPIRING,
    SubscriptionStatus.EXPIRED: LicenseStatus.EXPIRED,
    SubscriptionStatus.SUSPENDED: LicenseStatus.SUSPENDED,
    SubscriptionStatus.CANCELLED: LicenseStatus.EXPIRED,  # no more service — same customer-facing state as EXPIRED
    SubscriptionStatus.TERMINATED: LicenseStatus.EXPIRED,
}

# CustomerModule.status (plain string column) derived from the owning
# Subscription's status — kept in sync purely for License
# Management/Purchased Modules UI accuracy; the access engine
# (services/billing/access_service.py) independently checks the
# Subscription's own status too, so this is defense-in-depth/display
# correctness, not the sole gate.
# Keep in sync with services/billing/expiry_job.py's
# _EXPIRING_SOON_WINDOW_DAYS — both express the same "pre-expiry warning"
# policy window; extend_subscription() below uses it to decide whether a
# pushed-out end_date still counts as EXPIRING or is safely ACTIVE again.
_EXPIRING_SOON_WINDOW_DAYS = 7

_ENTITLEMENT_STATUS_FOR_SUB = {
    SubscriptionStatus.TRIAL: "ACTIVE",
    SubscriptionStatus.PENDING: "SUSPENDED",
    SubscriptionStatus.ACTIVE: "ACTIVE",
    SubscriptionStatus.EXPIRING: "ACTIVE",
    SubscriptionStatus.PAST_DUE: "SUSPENDED",
    SubscriptionStatus.PAYMENT_PENDING: "SUSPENDED",
    SubscriptionStatus.EXPIRED: "EXPIRED",
    SubscriptionStatus.SUSPENDED: "SUSPENDED",
    SubscriptionStatus.CANCELLED: "EXPIRED",
    SubscriptionStatus.TERMINATED: "REVOKED",
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

# Lower = more access-granting. Used to pick which ONE subscription
# represents the org's overall display status when it holds several.
_STATUS_RANK = {
    SubscriptionStatus.ACTIVE: 0,
    SubscriptionStatus.TRIAL: 1,
    SubscriptionStatus.EXPIRING: 2,
    SubscriptionStatus.PAST_DUE: 3,
    SubscriptionStatus.PAYMENT_PENDING: 3,
    SubscriptionStatus.PENDING: 4,
    SubscriptionStatus.SUSPENDED: 5,
    SubscriptionStatus.EXPIRED: 6,
    SubscriptionStatus.CANCELLED: 6,
    SubscriptionStatus.TERMINATED: 6,
}


async def _sync_org_from_subscription(db: AsyncSession, org: Organization, sub: Subscription, plan: Plan | None) -> None:
    """Refreshes the Organization DISPLAY fields (license_status/plan/
    max_users/trial_expires_at/license_expires_at) and licensed_modules
    (a live UNION across every currently-active subscription — never
    `= plan.modules` wholesale, which used to silently clobber every
    other subscription's modules).

    Caught live during SB10 verification: this used to derive
    license_status/is_active purely from the ONE subscription that was
    just mutated. Cancelling a secondary add-on subscription flipped the
    whole org to "license expired" even though its original, still-active
    trial subscription meant real access was completely unaffected — the
    Licenses page and every PermissionGate check read that single flag as
    an independent gate. Fixed by picking the single BEST (most
    access-granting, `_STATUS_RANK`) subscription across the org's whole
    portfolio to drive these fields, not whichever one was mutated."""
    result = await db.execute(select(Subscription).where(Subscription.organization_id == org.id))
    all_subs = list(result.scalars().all())
    best = min(all_subs, key=lambda s: _STATUS_RANK.get(s.status, 6)) if all_subs else sub
    best_plan = plan if best.id == sub.id else (await db.get(Plan, best.plan_id) if best.plan_id else None)

    org.license_status = _SUB_TO_LICENSE_STATUS.get(best.status, org.license_status)
    org.is_active = any(s.status != SubscriptionStatus.SUSPENDED for s in all_subs) if all_subs else (sub.status != SubscriptionStatus.SUSPENDED)
    if best_plan:
        org.plan = best_plan.code
        users_limit = (best_plan.limits or {}).get("users")
        if users_limit is not None:
            org.max_users = users_limit
    if best.status == SubscriptionStatus.TRIAL:
        org.trial_expires_at = best.end_date
    else:
        org.license_expires_at = best.end_date
    org.licensed_modules = sorted(await access_service.get_active_modules(db, org.id))
    await db.commit()


async def _create_entitlements(db: AsyncSession, org: Organization, sub: Subscription, modules: list[str]) -> None:
    """Creates one CustomerModule row per module, tied to this specific
    subscription — the (subscription_id, module) unique constraint means
    a DIFFERENT subscription can independently hold its own row for the
    same module (see models/customer_module.py)."""
    for module in dict.fromkeys(modules):  # de-dupe, preserve order
        db.add(CustomerModule(
            organization_id=org.id, subscription_id=sub.id, module=module,
            enabled=True, status=_ENTITLEMENT_STATUS_FOR_SUB.get(sub.status, "ACTIVE"),
            start_date=sub.start_date, expiry_date=sub.end_date,
        ))
    await db.flush()


async def _sync_entitlements_status(db: AsyncSession, sub: Subscription) -> None:
    """Flips every CustomerModule row tied to this subscription to the
    status implied by the subscription's own new status, and refreshes
    their expiry_date to match — called after every status/date change
    (renew/suspend/resume/cancel/extend/...)."""
    new_status = _ENTITLEMENT_STATUS_FOR_SUB.get(sub.status, "ACTIVE")
    result = await db.execute(select(CustomerModule).where(CustomerModule.subscription_id == sub.id))
    for cm in result.scalars().all():
        cm.status = new_status
        cm.expiry_date = sub.end_date
    await db.flush()


async def _reconcile_entitlements_for_modules(db: AsyncSession, org: Organization, sub: Subscription, target_modules: list[str]) -> None:
    """Adds/removes CustomerModule rows tied to THIS subscription so its
    entitlement set matches `target_modules` exactly — used by
    change_plan() when the new plan's module list differs from the old
    one. Never touches another subscription's rows for the same module,
    so an overlapping grant elsewhere is untouched (spec section 4/59)."""
    target = set(target_modules)
    result = await db.execute(select(CustomerModule).where(CustomerModule.subscription_id == sub.id))
    existing_rows = {cm.module: cm for cm in result.scalars().all()}
    for module, cm in existing_rows.items():
        if module not in target:
            await db.delete(cm)
    to_add = target - set(existing_rows.keys())
    if to_add:
        await _create_entitlements(db, org, sub, list(to_add))
    await db.flush()


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
    rows = result.all()

    # SB28 — one extra query for this page's subscriptions' most recent
    # Invoice status each, rather than N+1 (a query per row). Real
    # payment state, not derived/guessed from Subscription.status.
    sub_ids = [sub.id for sub, _, _ in rows]
    payment_status_by_sub: dict[int, str] = {}
    if sub_ids:
        inv_result = await db.execute(
            select(Invoice.subscription_id, Invoice.status, Invoice.issue_date)
            .where(Invoice.subscription_id.in_(sub_ids))
            .order_by(Invoice.issue_date.desc())
        )
        for sub_id, status, _issue_date in inv_result.all():
            # First row seen per subscription_id is the most recent
            # (already ordered desc) — never overwrite with an older one.
            payment_status_by_sub.setdefault(sub_id, status.value)

    items = []
    for sub, org, plan in rows:
        items.append({
            "id": sub.id, "organization_id": org.id, "organization_name": org.name,
            "plan_id": sub.plan_id, "plan_name": plan.name if plan else sub.plan_id and "Unknown", "plan_code": plan.code if plan else None,
            "status": sub.status.value, "start_date": sub.start_date, "end_date": sub.end_date,
            "billing_cycle": sub.billing_cycle.value, "amount": sub.amount, "currency": sub.currency,
            "auto_renew": sub.auto_renew, "created_at": sub.created_at,
            "payment_status": payment_status_by_sub.get(sub.id),
        })
    return items, total


async def get_subscription(db: AsyncSession, subscription_id: int) -> Subscription | None:
    return await db.get(Subscription, subscription_id)


async def create_subscription(db: AsyncSession, *, body, actor: User) -> Subscription:
    """Creates a new Subscription — independent of, and additive to, any
    other Subscription the org already holds (Multi-Subscription Billing
    & Payments Platform, spec section 4: "A customer can have Subscription
    A, Subscription B, Subscription C"). Each gets its own CustomerModule
    entitlement rows, tied to it via subscription_id, so this subscription
    expiring later never touches a module a different subscription also
    grants. `body.modules`, when given, is unioned with the plan's own
    module list — lets a subscription carry extra ad-hoc modules beyond
    its plan, or exist with no plan at all: plan_id=None + a real modules
    list is the "Custom" plan option (frontend Plan dropdown), priced from
    the module price catalog below rather than a fixed Plan.price.

    Payment gating: only a TRIAL subscription may start granting real
    access without payment. Any other requested status is overridden to
    PENDING here regardless of what the caller asked for — a brand-new
    paid subscription never activates itself; only services/billing/
    payment_service.py's record_payment(), on an actual confirmed
    payment, flips it (and its entitlements) to ACTIVE. This governs NEW
    subscriptions only — an already-active subscription's status is never
    touched by this function."""
    org = await db.get(Organization, body.organization_id)
    if org is None:
        raise SubscriptionServiceError("Customer not found.")
    plan = await db.get(Plan, body.plan_id) if body.plan_id else None
    if body.plan_id and plan is None:
        raise SubscriptionServiceError("Plan not found.")

    seller_row = (await db.execute(select(Seller).limit(1))).scalar_one_or_none()
    requested_status = SubscriptionStatus(body.status)
    status = requested_status if requested_status == SubscriptionStatus.TRIAL else SubscriptionStatus.PENDING
    now = datetime.now(timezone.utc)
    start_date = body.start_date or now

    modules = list(dict.fromkeys((plan.modules if plan else []) + (getattr(body, "modules", None) or [])))

    if body.amount is not None:
        amount = body.amount
    elif plan:
        amount = plan.price
    elif modules:
        # "Custom" plan, no fixed Plan row — sum the per-module catalog
        # (Settings -> Module Prices) instead of defaulting to 0.
        from app.services.seller import module_service
        amount = await module_service.price_for_modules(db, modules)
    else:
        amount = 0.0

    sub = Subscription(
        seller_id=seller_row.id if seller_row else None,
        organization_id=org.id, plan_id=plan.id if plan else None,
        status=status, start_date=start_date,
        end_date=body.end_date, billing_cycle=BillingCycle(body.billing_cycle),
        amount=amount,
        currency=body.currency or (plan.currency if plan else "USD"),
        auto_renew=body.auto_renew, created_by=actor.id,
        billing_start_date=start_date, current_period_start=start_date, current_period_end=body.end_date,
        trial_start_date=start_date if status == SubscriptionStatus.TRIAL else None,
        trial_end_date=body.end_date if status == SubscriptionStatus.TRIAL else None,
    )
    db.add(sub)
    await db.flush()

    await _create_entitlements(db, org, sub, modules)

    await _sync_org_from_subscription(db, org, sub, plan)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="Subscription Created",
        resource=org.name, resource_id=str(sub.id),
        after_value={
            "plan": plan.code if plan else None, "status": sub.status.value, "requested_status": requested_status.value,
            "amount": sub.amount, "modules": modules,
        },
    )
    # Spec section 13: an invoice is generated for EVERY subscription,
    # including trial ones (at $0.00 / NO_PAYMENT_REQUIRED — see
    # invoice_service.create_invoice_for_subscription()).
    await invoice_service.create_invoice_for_subscription(
        db, subscription=sub, reason="TRIAL" if status == SubscriptionStatus.TRIAL else "CREATE", actor_id=actor.id,
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


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite/SQLAlchemy round-trips DateTime columns as naive — comparing
    a naive `sub.end_date` against an aware `datetime.now(timezone.utc)`
    raises TypeError. Found live during SB12 verification (extend_subscription
    500'd on its very first real call); renew_subscription had carried the
    exact same latent bug since long before this feature. Always normalize
    before comparing."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def renew_subscription(db: AsyncSession, sub: Subscription, *, actor: User | None = None) -> Subscription:
    """`actor=None` is the automated-renewal path (services/billing/
    billing_scheduler.py, SB22) — record_seller_event already accepts
    seller_user_id=None for system-triggered events (same convention the
    payment webhook route uses); every other call site here keeps
    passing a real actor unchanged."""
    now = datetime.now(timezone.utc)
    end_date_aware = _aware(sub.end_date)
    base = end_date_aware if end_date_aware and end_date_aware > now else now
    delta = _CYCLE_DELTA.get(sub.billing_cycle, timedelta(days=30))
    before_end = sub.end_date
    sub.current_period_start = sub.current_period_end or base
    sub.end_date = base + delta
    sub.current_period_end = sub.end_date
    sub.next_billing_date = sub.end_date
    sub.status = SubscriptionStatus.ACTIVE

    # Apply a pending downgrade (spec section 37) at the moment renewal
    # actually reaches the period boundary it was deferred to.
    if sub.pending_plan_id is not None:
        new_plan = await db.get(Plan, sub.pending_plan_id)
        sub.plan_id = sub.pending_plan_id
        sub.pending_plan_id = None
        if new_plan:
            sub.amount = new_plan.price
            sub.currency = new_plan.currency
    await db.flush()

    org, plan = await _org_and_plan_for(db, sub)
    if plan:
        await _reconcile_entitlements_for_modules(db, org, sub, plan.modules or [])
    await _sync_entitlements_status(db, sub)
    await db.commit()

    await _sync_org_from_subscription(db, org, sub, plan)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id if actor else None, customer_id=sub.organization_id, action="Subscription Changed",
        resource=org.name, resource_id=str(sub.id),
        details=f"Renewed through {sub.end_date.date()}" + ("" if actor else " (auto-renewed by billing scheduler)"),
        before_value={"end_date": before_end.isoformat() if before_end else None},
        after_value={"end_date": sub.end_date.isoformat(), "status": sub.status.value},
    )
    await invoice_service.create_invoice_for_subscription(db, subscription=sub, reason="RENEWAL", actor_id=actor.id if actor else None)
    return sub


async def change_plan(db: AsyncSession, sub: Subscription, *, new_plan_id: int, actor: User, effective: str = "immediate") -> Subscription:
    """`effective="immediate"` (default, and always used for upgrades)
    reconciles this subscription's CustomerModule entitlements to the new
    plan's module list right away. `effective="period_end"` (spec section
    37, used by the API layer for downgrades) instead only records
    `pending_plan_id` — entitlements/plan_id/amount stay exactly as they
    are until renew_subscription() (or the billing scheduler's
    auto-renewal, SB22) actually reaches the current period boundary, so
    access is never removed mid-cycle."""
    new_plan = await db.get(Plan, new_plan_id)
    if new_plan is None:
        raise SubscriptionServiceError("Plan not found.")
    old_plan = await db.get(Plan, sub.plan_id) if sub.plan_id else None
    direction = "Upgraded" if (old_plan is None or new_plan.price >= old_plan.price) else "Downgraded"

    before = {"plan": old_plan.code if old_plan else None, "amount": sub.amount}

    if effective == "period_end" and direction == "Downgraded":
        sub.pending_plan_id = new_plan.id
        await db.commit()
        org, _ = await _org_and_plan_for(db, sub)
        await db.refresh(sub)
        await record_seller_event(
            db, seller_user_id=actor.id, customer_id=sub.organization_id, action="Subscription Changed",
            resource=org.name, resource_id=str(sub.id),
            details=f"Downgrade to {new_plan.code} scheduled for {sub.end_date.date() if sub.end_date else 'end of current period'}",
            before_value=before, after_value={"pending_plan": new_plan.code},
        )
        return sub

    sub.plan_id = new_plan.id
    sub.amount = new_plan.price
    sub.currency = new_plan.currency
    sub.pending_plan_id = None
    if sub.status in (SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED, SubscriptionStatus.TERMINATED):
        sub.status = SubscriptionStatus.ACTIVE
    await db.flush()

    org, _ = await _org_and_plan_for(db, sub)
    await _reconcile_entitlements_for_modules(db, org, sub, new_plan.modules or [])
    await _sync_entitlements_status(db, sub)
    await db.commit()

    await _sync_org_from_subscription(db, org, sub, new_plan)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=sub.organization_id, action="Subscription Changed",
        resource=org.name, resource_id=str(sub.id), details=f"{direction} from {before['plan'] or 'no plan'} to {new_plan.code}",
        before_value=before, after_value={"plan": new_plan.code, "amount": sub.amount, "modules": new_plan.modules},
    )
    # No invoice for the period_end-deferred branch above — nothing
    # billable has changed yet; the RENEWAL invoice generated when
    # renew_subscription() actually applies the pending downgrade covers it.
    await invoice_service.create_invoice_for_subscription(
        db, subscription=sub, reason="UPGRADE" if direction == "Upgraded" else "DOWNGRADE", actor_id=actor.id,
    )
    return sub


async def extend_subscription(db: AsyncSession, sub: Subscription, *, days: int, actor: User) -> Subscription:
    """Extends the hard expiry date without changing plan (spec section
    43's Seller Subscription Screen 'Extend' action) — distinct from
    renew_subscription(), which also advances the billing period.

    Status IS adjusted here, though: access_service.has_module_access()
    gates on sub.status being one of the granting statuses (TRIAL/ACTIVE/
    EXPIRING), not on end_date alone (found live, SB28) — so extending a
    subscription whose status had already flipped to EXPIRING (the soft
    pre-expiry warning) or hard EXPIRED must also flip status back, or
    the pushed-out end_date is cosmetic and access stays denied/stale.
    SUSPENDED/CANCELLED/TERMINATED are deliberate seller/customer
    decisions independent of the expiry date and are intentionally left
    alone here — those need their own Resume/reactivation action."""
    before_end = sub.end_date
    before_status = sub.status
    now = datetime.now(timezone.utc)
    end_date_aware = _aware(sub.end_date)
    base = end_date_aware if end_date_aware and end_date_aware > now else now
    sub.end_date = base + timedelta(days=days)
    sub.current_period_end = sub.end_date

    reactivated = False
    if before_status in (SubscriptionStatus.EXPIRING, SubscriptionStatus.EXPIRED):
        still_within_warning_window = sub.end_date <= now + timedelta(days=_EXPIRING_SOON_WINDOW_DAYS)
        sub.status = SubscriptionStatus.EXPIRING if still_within_warning_window else SubscriptionStatus.ACTIVE
        if before_status == SubscriptionStatus.EXPIRED:
            sub.cancelled_at = None
            reactivated = True
    await db.flush()

    await _sync_entitlements_status(db, sub)
    await db.commit()

    org, plan = await _org_and_plan_for(db, sub)
    await _sync_org_from_subscription(db, org, sub, plan)
    await db.refresh(sub)

    details = f"Extended by {days} days, through {sub.end_date.date()}"
    if reactivated:
        details += " (reactivated from EXPIRED)"
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=sub.organization_id, action="Subscription Changed",
        resource=org.name, resource_id=str(sub.id), details=details,
        before_value={"end_date": before_end.isoformat() if before_end else None, "status": before_status.value},
        after_value={"end_date": sub.end_date.isoformat(), "status": sub.status.value},
    )
    await invoice_service.create_invoice_for_subscription(db, subscription=sub, reason="EXTENSION", actor_id=actor.id)
    return sub


async def add_module(
    db: AsyncSession, *, organization_id: int, module: str, actor: User,
    amount: float | None = None, currency: str = "USD", billing_cycle: str = "MONTHLY",
    auto_renew: bool = False, end_date: datetime | None = None,
) -> Subscription:
    """Spec section 38, "Add Module": creates a small, dedicated
    Subscription carrying exactly this one module. Consistent with the
    rest of this model (every entitlement always belongs to exactly one
    Subscription) rather than inventing a second "line item on an
    existing subscription" concept. Rejects outright if the org already
    has an ACTIVE entitlement for this module anywhere — "do not create
    duplicate entitlements" (spec section 38's explicit instruction).

    Payment gating, same rule as create_subscription(): only a
    trial-mode org gets this for free and immediately active. Any other
    org gets a real, catalog-priced (Settings -> Module Prices) PENDING
    subscription — no access until services/billing/payment_service.py
    records a real payment for its invoice. "Trial mode" here means the
    org's own most-recent Subscription is itself a TRIAL — the same
    signal Purchased Modules' _block_reason()/pricing decisions use."""
    org = await db.get(Organization, organization_id)
    if org is None:
        raise SubscriptionServiceError("Customer not found.")
    existing_access = await access_service.has_module_access(db, organization_id, module)
    if existing_access.granted:
        raise SubscriptionServiceError(f"{module.replace('_', ' ').title()} is already active for this organization.")

    # `existing_access.granted` above is False for an unpaid grant too
    # (PENDING subscription -> SUSPENDED CustomerModule, see
    # _ENTITLEMENT_STATUS_FOR_SUB) — so without this second check, clicking
    # "Enable" twice on Purchased Modules (or hitting this endpoint twice)
    # before the first invoice is paid created a SECOND Subscription (and a
    # second invoice) for the exact same module every time. Found live.
    # Block on ANY non-terminal subscription already covering this module,
    # paid or not, not just an active one.
    outstanding_result = await db.execute(
        select(CustomerModule.id).join(Subscription, Subscription.id == CustomerModule.subscription_id)
        .where(
            CustomerModule.organization_id == organization_id,
            CustomerModule.module == module,
            Subscription.status.notin_([
                SubscriptionStatus.CANCELLED, SubscriptionStatus.TERMINATED, SubscriptionStatus.EXPIRED,
            ]),
        )
    )
    if outstanding_result.scalars().first() is not None:
        raise SubscriptionServiceError(
            f"{module.replace('_', ' ').title()} already has a subscription for this organization — "
            "check Subscriptions or Invoices; it may be pending payment."
        )

    seller_row = (await db.execute(select(Seller).limit(1))).scalar_one_or_none()
    now = datetime.now(timezone.utc)

    latest_sub_result = await db.execute(
        select(Subscription).where(Subscription.organization_id == organization_id).order_by(Subscription.created_at.desc()).limit(1)
    )
    latest_sub = latest_sub_result.scalar_one_or_none()
    is_trial_org = latest_sub is not None and latest_sub.status == SubscriptionStatus.TRIAL

    if is_trial_org:
        status = SubscriptionStatus.TRIAL
        amount = 0.0
    else:
        status = SubscriptionStatus.PENDING
        if amount is None:
            from app.services.seller import module_service
            amount = await module_service.price_for_modules(db, [module])

    sub = Subscription(
        seller_id=seller_row.id if seller_row else None, organization_id=org.id, plan_id=None,
        status=status, start_date=now, end_date=end_date,
        billing_cycle=BillingCycle(billing_cycle), amount=amount, currency=currency,
        auto_renew=auto_renew, created_by=actor.id,
        billing_start_date=now, current_period_start=now, current_period_end=end_date,
        trial_start_date=now if status == SubscriptionStatus.TRIAL else None,
        trial_end_date=end_date if status == SubscriptionStatus.TRIAL else None,
    )
    db.add(sub)
    await db.flush()
    await _create_entitlements(db, org, sub, [module])
    await _sync_org_from_subscription(db, org, sub, None)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=org.id, action="Module Purchased",
        module=module, resource=org.name, resource_id=str(sub.id),
        details=f"Added {module} as a standalone subscription ({status.value})",
        after_value={"module": module, "subscription_id": sub.id, "status": status.value, "amount": amount},
    )
    await invoice_service.create_invoice_for_subscription(db, subscription=sub, reason="ADD_MODULE", actor_id=actor.id)
    return sub


async def _set_status(
    db: AsyncSession, sub: Subscription, *, new_status: SubscriptionStatus, action_detail: str, actor: User | None,
) -> Subscription:
    """`actor=None` is the automated path (services/billing/expiry_job.py,
    SB22) — same system-triggered-event convention as renew_subscription()
    above."""
    before_status = sub.status.value
    sub.status = new_status
    if new_status in (SubscriptionStatus.CANCELLED, SubscriptionStatus.TERMINATED, SubscriptionStatus.EXPIRED):
        sub.auto_renew = False
        sub.cancelled_at = sub.cancelled_at or datetime.now(timezone.utc)
    await db.flush()

    await _sync_entitlements_status(db, sub)
    await db.commit()

    org, plan = await _org_and_plan_for(db, sub)
    await _sync_org_from_subscription(db, org, sub, plan)
    await db.refresh(sub)

    await record_seller_event(
        db, seller_user_id=actor.id if actor else None, customer_id=sub.organization_id, action="Subscription Changed",
        resource=org.name, resource_id=str(sub.id), details=action_detail + ("" if actor else " (automated)"),
        before_value={"status": before_status}, after_value={"status": sub.status.value},
    )
    return sub


# Public re-exports of the two sync helpers above — services/billing/
# payment_service.py needs them too (a successful payment activates the
# subscription an invoice belongs to, which means refreshing the exact
# same org-display-cache and entitlement-status fields any other
# lifecycle mutation does). Kept as thin wrappers rather than renaming
# the originals so none of this file's ~10 existing internal call sites
# needed touching.
async def sync_org_from_subscription(db: AsyncSession, org: Organization, sub: Subscription, plan: Plan | None) -> None:
    return await _sync_org_from_subscription(db, org, sub, plan)


async def sync_entitlements_status(db: AsyncSession, sub: Subscription) -> None:
    return await _sync_entitlements_status(db, sub)


async def suspend_subscription(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    return await _set_status(db, sub, new_status=SubscriptionStatus.SUSPENDED, action_detail="Suspended", actor=actor)


async def resume_subscription(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    return await _set_status(db, sub, new_status=SubscriptionStatus.ACTIVE, action_detail="Resumed", actor=actor)


async def cancel_subscription(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    return await _set_status(db, sub, new_status=SubscriptionStatus.CANCELLED, action_detail="Cancelled", actor=actor)


async def mark_past_due(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    return await _set_status(db, sub, new_status=SubscriptionStatus.PAST_DUE, action_detail="Marked past due", actor=actor)


async def mark_payment_pending(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    return await _set_status(db, sub, new_status=SubscriptionStatus.PAYMENT_PENDING, action_detail="Payment pending", actor=actor)


async def mark_expiring(db: AsyncSession, sub: Subscription, *, actor: User | None = None) -> Subscription:
    """SB22's expiry_job — flips a subscription into the pre-expiry
    warning window (spec section 8's "expiring soon" state), distinct
    from the hard EXPIRED transition below."""
    return await _set_status(db, sub, new_status=SubscriptionStatus.EXPIRING, action_detail="Marked expiring soon", actor=actor)


async def expire_subscription(db: AsyncSession, sub: Subscription, *, actor: User | None = None) -> Subscription:
    """SB22's expiry_job — the hard EXPIRED transition once end_date has
    actually passed. auto_renew is force-cleared by _set_status() above
    for this status, same as CANCELLED/TERMINATED, so a subscription
    that already lapsed once can never silently auto-renew again."""
    return await _set_status(db, sub, new_status=SubscriptionStatus.EXPIRED, action_detail="Expired", actor=actor)


async def terminate_subscription(db: AsyncSession, sub: Subscription, *, actor: User) -> Subscription:
    return await _set_status(db, sub, new_status=SubscriptionStatus.TERMINATED, action_detail="Terminated", actor=actor)
