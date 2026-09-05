"""
Hourly expiry sweep (spec sections 8/51, SB22). Two real, independent
sweeps over actual Subscription rows:

  1. Hard-expire: any granting-status subscription whose end_date has
     actually passed -> subscription_service.expire_subscription(),
     which flips CustomerModule entitlements + the Organization display
     cache in the exact same code path every other lifecycle mutation
     uses (no separate "expiry" logic duplicated here).
  2. Soft warn: granting-status subscriptions inside the pre-expiry
     window get a real Notification row (once — see the dedup note
     below), without changing status for TRIAL (which stays TRIAL until
     it actually lapses) but DOES flip paid subscriptions to EXPIRING
     (access_service already treats EXPIRING as a granting status —
     this is a display/warning state, never a denial).

This job's own cadence is NEVER what real-time access decisions depend
on — services/billing/access_service.py evaluates end_date/expiry_date
live, per request, against the actual current time. This job exists
purely to materialize status transitions + fire notifications, so a
sweep that's an hour late never grants access it shouldn't; it only
delays the notification/status-cache refresh by that hour.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.models.billing.notification import Notification
from app.models.plan import Plan
from app.models.subscription import Subscription, SubscriptionStatus
from app.services.billing import notification_service
from app.services.seller import subscription_service

_EXPIRING_SOON_WINDOW_DAYS = 7
_GRANTING_STATUSES = (SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _already_notified(db: AsyncSession, *, subscription_id: int, type: str) -> bool:
    result = await db.execute(
        select(Notification.id).where(
            Notification.related_object_type == "subscription",
            Notification.related_object_id == subscription_id,
            Notification.type == type,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _plan_name(db: AsyncSession, plan_id: int | None) -> str:
    if plan_id is None:
        return "subscription"
    plan = await db.get(Plan, plan_id)
    return plan.name if plan else "subscription"


async def run_expiry_sweep(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    stats = {"expired": 0, "marked_expiring": 0, "notified": 0}

    # 1. Hard expiry — status flips away from _GRANTING_STATUSES so this
    #    is naturally idempotent: an already-expired row never matches
    #    the filter again on the next hourly run.
    result = await db.execute(select(Subscription).where(Subscription.status.in_(_GRANTING_STATUSES)))
    for sub in result.scalars().all():
        end = _aware(sub.end_date)
        if end is None or end > now:
            continue
        was_trial = sub.status == SubscriptionStatus.TRIAL
        plan_name = await _plan_name(db, sub.plan_id)
        await subscription_service.expire_subscription(db, sub, actor=None)
        stats["expired"] += 1
        await notification_service.create_notification(
            db, organization_id=sub.organization_id,
            type="TRIAL_EXPIRED" if was_trial else "SUBSCRIPTION_EXPIRED",
            title="Your trial has expired" if was_trial else f"Your {plan_name} subscription has expired",
            body=(
                "Your trial period ended and access to its modules has been suspended. Subscribe to a paid plan to continue."
                if was_trial else
                f"Your {plan_name} subscription expired on {end.date()}. Renew to restore access to its modules."
            ),
            related_object_type="subscription", related_object_id=sub.id,
        )
        stats["notified"] += 1

    # 2. Soft pre-expiry warning — dedup via _already_notified() since,
    #    unlike the hard-expiry branch, a TRIAL subscription's status
    #    deliberately does NOT change here, so it would otherwise still
    #    match this same filter on every subsequent hourly run.
    window = now + timedelta(days=_EXPIRING_SOON_WINDOW_DAYS)
    result = await db.execute(select(Subscription).where(Subscription.status.in_(_GRANTING_STATUSES)))
    for sub in result.scalars().all():
        end = _aware(sub.end_date)
        if end is None or not (now < end <= window):
            continue
        is_trial = sub.status == SubscriptionStatus.TRIAL
        notif_type = "TRIAL_ENDING_SOON" if is_trial else "SUBSCRIPTION_EXPIRING_SOON"
        if await _already_notified(db, subscription_id=sub.id, type=notif_type):
            continue
        plan_name = await _plan_name(db, sub.plan_id)
        if not is_trial and sub.status != SubscriptionStatus.EXPIRING:
            await subscription_service.mark_expiring(db, sub, actor=None)
        stats["marked_expiring"] += 1
        await notification_service.create_notification(
            db, organization_id=sub.organization_id, type=notif_type,
            title="Your trial ends soon" if is_trial else f"Your {plan_name} subscription is expiring soon",
            body=(
                f"Your trial ends on {end.date()}. Subscribe to a paid plan before then to avoid losing access."
                if is_trial else
                f"Your {plan_name} subscription expires on {end.date()}. Renew to keep uninterrupted access."
            ),
            related_object_type="subscription", related_object_id=sub.id,
        )
        stats["notified"] += 1

    return stats


async def run_expiry_sweep_standalone() -> dict:
    """Entry point for the APScheduler job (main.py) — opens its own
    session, same pattern every other scheduled/background task in this
    codebase uses (there's no request-scoped session to reuse here)."""
    async with AsyncSessionLocal() as db:
        return await run_expiry_sweep(db)
