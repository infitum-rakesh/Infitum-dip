"""
SubscriptionAccessService — the centralized, multi-subscription-aware
access engine (Multi-Subscription Billing & Payments Platform, 2026,
spec section 39). Every module route now funnels through this via
core/deps.require_action() and api/sql_workspace.py's
require_sql_action() (see SB5) instead of reading
Organization.licensed_modules directly — no module router talks to
Subscription/CustomerModule itself, per the spec's explicit "do not
duplicate subscription logic across ETL/OCR/SQL/Pipeline/..." rule.

Core mechanic: a module is accessible if ANY of an org's currently-active
Subscriptions carries an ACTIVE, unexpired CustomerModule entitlement for
it — not "is this module in the org's single cached licensed_modules
list". This is what makes one subscription expiring never take down a
module a DIFFERENT, still-active subscription also grants (spec section
4's Subscription A/B example, section 59's John/ETL/OCR example).

Backward-compat fallback: an org with zero Subscription rows AND zero
CustomerModule rows at all (pre-existing/orphaned data, or an org that
somehow never got seeded) falls back to reading
Organization.licensed_modules directly — the exact behavior every router
had before this feature existed, so nothing regresses.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer_module import CustomerModule
from app.models.organization import Organization
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User
from app.services.license_service import ALL_MODULES

# Subscription statuses that still grant access to their modules — TRIAL
# and EXPIRING both still work (EXPIRING is a soft pre-expiry warning
# state, not a denial state); PENDING/PAST_DUE/PAYMENT_PENDING/EXPIRED/
# SUSPENDED/CANCELLED/TERMINATED do not.
_GRANTING_STATUSES = {SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING}

# The real "expiring soon" pre-warning window — must match
# services/billing/expiry_job.py's _EXPIRING_SOON_WINDOW_DAYS (the job
# that actually flips a subscription's status to EXPIRING and fires the
# notification) and services/seller/subscription_service.py's own copy
# (used by extend_subscription() to decide EXPIRING vs ACTIVE). Found
# live, SB30: get_expiring_modules() previously defaulted to 30 days,
# which made the Dashboard/Sidebar/modules-summary call a module
# "expiring soon" over three weeks before the scheduler itself would.
_EXPIRING_SOON_WINDOW_DAYS = 7

# CustomerModule.status is a plain string column (see models/customer_module.py).
_GRANTING_ENTITLEMENT_STATUSES = {"ACTIVE"}


class DenialReason(str, enum.Enum):
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
    TENANT_SUSPENDED = "TENANT_SUSPENDED"
    USER_INACTIVE = "USER_INACTIVE"
    USER_NOT_GRANTED_MODULE = "USER_NOT_GRANTED_MODULE"
    MODULE_UNKNOWN = "MODULE_UNKNOWN"
    MODULE_NOT_PURCHASED = "MODULE_NOT_PURCHASED"
    MODULE_EXPIRED = "MODULE_EXPIRED"
    NO_ACTIVE_SUBSCRIPTION = "NO_ACTIVE_SUBSCRIPTION"


@dataclass
class AccessResult:
    granted: bool
    reason: DenialReason | None = None
    expired_at: datetime | None = None
    # True when a DIFFERENT active subscription than the one that just
    # expired still covers this module — lets callers distinguish "this
    # exact grant lapsed but you're still fine" from a real denial. Not
    # currently surfaced further than the dataclass field itself.
    covered_by_other_subscription: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _entitlement_active(cm: CustomerModule) -> bool:
    if cm.status not in _GRANTING_ENTITLEMENT_STATUSES:
        return False
    expiry = _aware(cm.expiry_date)
    if expiry is not None and expiry <= _now():
        return False
    return True


def _subscription_active(sub: Subscription) -> bool:
    if sub.status not in _GRANTING_STATUSES:
        return False
    end = _aware(sub.end_date)
    if end is not None and end <= _now():
        return False
    return True


class SubscriptionAccessService:
    async def _all_subscriptions(self, db: AsyncSession, organization_id: int) -> list[Subscription]:
        result = await db.execute(select(Subscription).where(Subscription.organization_id == organization_id))
        return list(result.scalars().all())

    async def get_active_subscriptions(self, db: AsyncSession, organization_id: int) -> list[Subscription]:
        subs = await self._all_subscriptions(db, organization_id)
        return [s for s in subs if _subscription_active(s)]

    async def _entitlements_for_org(self, db: AsyncSession, organization_id: int) -> list[CustomerModule]:
        result = await db.execute(select(CustomerModule).where(CustomerModule.organization_id == organization_id))
        return list(result.scalars().all())

    async def has_tenant_access(self, db: AsyncSession, organization_id: int) -> bool:
        org = await db.get(Organization, organization_id)
        if org is None or not org.is_active:
            return False
        all_subs = await self._all_subscriptions(db, organization_id)
        if any(_subscription_active(s) for s in all_subs):
            return True
        if all_subs:
            # Found live, SB30: this org DOES have real Subscription rows —
            # they're just all currently expired/inactive — so it must be
            # denied, never silently waved through via the org.
            # licensed_modules cache below (that cache can lag up to an
            # hour behind reality between expiry_job.py sweeps; this is
            # the "access must be evaluated live" path, not the display
            # cache's job). Previously this fell through to the legacy
            # fallback unconditionally whenever get_active_subscriptions()
            # was empty, regardless of whether the org had ever had a
            # subscription at all.
            return False
        # Legacy fallback — genuinely zero Subscription rows ever created
        # for this org (pre-existing/orphaned data), the exact case this
        # fallback was written for.
        return bool(org.licensed_modules)

    async def get_active_modules(self, db: AsyncSession, organization_id: int) -> set[str]:
        active_sub_ids = {s.id for s in await self.get_active_subscriptions(db, organization_id)}
        entitlements = await self._entitlements_for_org(db, organization_id)
        if not active_sub_ids and not entitlements:
            org = await db.get(Organization, organization_id)
            return set(org.licensed_modules or []) if org else set()
        return {cm.module for cm in entitlements if cm.subscription_id in active_sub_ids and _entitlement_active(cm)}

    async def get_expired_modules(self, db: AsyncSession, organization_id: int) -> dict[str, datetime | None]:
        """module -> most recent expiry among its lapsed entitlements,
        excluding any module still covered by a different active
        subscription (spec section 4/59: that module is NOT expired from
        the tenant's point of view)."""
        active_modules = await self.get_active_modules(db, organization_id)
        entitlements = await self._entitlements_for_org(db, organization_id)
        expired: dict[str, datetime | None] = {}
        for cm in entitlements:
            if cm.module in active_modules or _entitlement_active(cm):
                continue
            existing = expired.get(cm.module)
            if existing is None or (cm.expiry_date and cm.expiry_date > existing):
                expired[cm.module] = cm.expiry_date
        return expired

    async def get_expiring_modules(self, db: AsyncSession, organization_id: int, within_days: int = _EXPIRING_SOON_WINDOW_DAYS) -> list[dict]:
        horizon = _now() + timedelta(days=within_days)
        active_sub_ids = {s.id for s in await self.get_active_subscriptions(db, organization_id)}
        entitlements = await self._entitlements_for_org(db, organization_id)
        out: list[dict] = []
        for cm in entitlements:
            if cm.subscription_id not in active_sub_ids or not _entitlement_active(cm):
                continue
            expiry = _aware(cm.expiry_date)
            if expiry is not None and expiry <= horizon:
                out.append({"module": cm.module, "expiry_date": cm.expiry_date, "subscription_id": cm.subscription_id})
        return out

    async def has_module_access(self, db: AsyncSession, organization_id: int, module: str) -> AccessResult:
        if module not in ALL_MODULES:
            return AccessResult(granted=False, reason=DenialReason.MODULE_UNKNOWN)

        org = await db.get(Organization, organization_id)
        if org is None:
            return AccessResult(granted=False, reason=DenialReason.TENANT_NOT_FOUND)
        if not org.is_active:
            return AccessResult(granted=False, reason=DenialReason.TENANT_SUSPENDED)

        active_subs = await self.get_active_subscriptions(db, organization_id)
        entitlements = await self._entitlements_for_org(db, organization_id)

        if not active_subs and not entitlements:
            # Legacy org with no Subscription/CustomerModule rows at all —
            # fall back to the pre-existing single-flag behavior.
            if module in (org.licensed_modules or []):
                return AccessResult(granted=True)
            return AccessResult(granted=False, reason=DenialReason.MODULE_NOT_PURCHASED)

        active_sub_ids = {s.id for s in active_subs}
        module_rows = [cm for cm in entitlements if cm.module == module]
        if not module_rows:
            return AccessResult(granted=False, reason=DenialReason.MODULE_NOT_PURCHASED)

        for cm in module_rows:
            if cm.subscription_id in active_sub_ids and _entitlement_active(cm):
                return AccessResult(granted=True)

        expiries = [cm.expiry_date for cm in module_rows if cm.expiry_date is not None]
        latest_expiry = max(expiries) if expiries else None
        if not active_subs:
            return AccessResult(granted=False, reason=DenialReason.NO_ACTIVE_SUBSCRIPTION, expired_at=latest_expiry)
        return AccessResult(granted=False, reason=DenialReason.MODULE_EXPIRED, expired_at=latest_expiry)

    async def has_user_access(self, db: AsyncSession, user: User, module: str) -> AccessResult:
        if not user.is_active:
            return AccessResult(granted=False, reason=DenialReason.USER_INACTIVE)
        if user.organization_id is None:
            # Seller/Super Admin — no organization, so tenant-subscription
            # gating doesn't apply to them; their access is entirely
            # role-based elsewhere (core/deps.py's ROLE_ACTIONS).
            return AccessResult(granted=True)
        tenant_result = await self.has_module_access(db, user.organization_id, module)
        if not tenant_result.granted:
            return tenant_result
        accessible = user.accessible_modules
        if accessible is not None and module not in accessible:
            return AccessResult(granted=False, reason=DenialReason.USER_NOT_GRANTED_MODULE)
        return AccessResult(granted=True)

    async def check_module_access(
        self, db: AsyncSession, *, organization_id: int | None, user: User, module: str
    ) -> AccessResult:
        """The full chain from spec section 5: tenant exists/active -> user
        active -> module exists -> user permission -> >=1 subscription
        grants it -> that subscription active/unexpired. `organization_id`
        is accepted separately from `user.organization_id` so a Seller
        checking a specific customer's access (not their own, since they
        have none) can call this too."""
        if organization_id is None:
            return AccessResult(granted=True)  # Seller/Super Admin, no tenant scope
        if user.organization_id == organization_id:
            return await self.has_user_access(db, user, module)
        # Seller/Super Admin inspecting a different org's access —
        # tenant+module evaluation only, user-grant layer doesn't apply
        # (they aren't a member of that org).
        return await self.has_module_access(db, organization_id, module)


access_service = SubscriptionAccessService()
