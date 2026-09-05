"""
Hourly billing-cycle sweep (spec section 51, SB22) — the counterpart to
expiry_job.py. Finds subscriptions whose next_billing_date has arrived
and either auto-renews them for real (advances the period, generates the
RENEWAL invoice, reactivates entitlements — all via subscription_
service.renew_subscription(), the exact same function the manual "Renew"
button in the Seller Portal calls) or, when auto_renew is off, generates
the renewal invoice for visibility and notifies without touching the
subscription's own state — expiry_job.py's end_date-driven sweep is what
actually lapses it later if nobody pays.

Like expiry_job.py, this job's cadence never gates real-time access
decisions — it only materializes billing state and fires notifications.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.models.plan import Plan
from app.models.subscription import Subscription, SubscriptionStatus
from app.services.billing import invoice_service, notification_service
from app.services.seller import subscription_service

_BILLABLE_STATUSES = (SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _plan_name(db: AsyncSession, plan_id: int | None) -> str:
    if plan_id is None:
        return "subscription"
    plan = await db.get(Plan, plan_id)
    return plan.name if plan else "subscription"


async def run_billing_sweep(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    stats = {"auto_renewed": 0, "renewal_due_notified": 0}

    result = await db.execute(
        select(Subscription).where(
            Subscription.status.in_(_BILLABLE_STATUSES), Subscription.next_billing_date.isnot(None),
        )
    )
    for sub in result.scalars().all():
        due = _aware(sub.next_billing_date)
        if due is None or due > now:
            continue
        plan_name = await _plan_name(db, sub.plan_id)

        if sub.auto_renew:
            # renew_subscription() advances next_billing_date past `now`
            # itself, so an auto-renewed row naturally stops matching
            # this filter on the next hourly run — no separate dedup needed.
            await subscription_service.renew_subscription(db, sub, actor=None)
            stats["auto_renewed"] += 1
            await notification_service.create_notification(
                db, organization_id=sub.organization_id, type="SUBSCRIPTION_RENEWED",
                title=f"Your {plan_name} subscription was renewed",
                body=f"Your {plan_name} subscription auto-renewed and is active through {sub.end_date.date() if sub.end_date else 'the new billing period'}.",
                related_object_type="subscription", related_object_id=sub.id,
            )
        else:
            # Not auto-renewing: generate the reminder invoice for
            # visibility, then clear next_billing_date so this doesn't
            # refire every hour — expiry_job.py's end_date sweep is the
            # backstop that actually lapses the subscription if nobody
            # acts on this invoice before end_date.
            invoice = await invoice_service.create_invoice_for_subscription(
                db, subscription=sub, reason="RENEWAL", actor_id=None,
            )
            sub.next_billing_date = None
            await db.commit()
            stats["renewal_due_notified"] += 1
            await notification_service.create_notification(
                db, organization_id=sub.organization_id, type="INVOICE_DUE",
                title=f"Renewal invoice ready for your {plan_name} subscription",
                body=f"Invoice {invoice.invoice_number} (${invoice.total:.2f}) is ready. Pay it to renew before your subscription expires on {sub.end_date.date() if sub.end_date else 'its expiry date'}.",
                related_object_type="invoice", related_object_id=invoice.id,
            )

    return stats


async def run_billing_sweep_standalone() -> dict:
    """Entry point for the APScheduler job (main.py)."""
    async with AsyncSessionLocal() as db:
        return await run_billing_sweep(db)
