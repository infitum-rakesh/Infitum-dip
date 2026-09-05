"""
Seller Billing Dashboard (spec section 42, SB25) — real KPIs computed
from Invoice/Payment/Subscription/PaymentGateway rows, the same tables
every other billing phase in this feature already writes to. Nothing
here is precomputed/cached; every call re-aggregates live.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing.invoice import Invoice, InvoiceStatus
from app.models.billing.payment import Payment, PaymentStatus
from app.models.billing.payment_gateway import PaymentGateway
from app.models.organization import Organization
from app.models.subscription import Subscription, SubscriptionStatus

_ACTIVE_STATUSES = (SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING)
_OUTSTANDING_STATUSES = (InvoiceStatus.PAYMENT_PENDING, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.OVERDUE)

# Subscription.billing_cycle only has MONTHLY/ANNUAL/CUSTOM (models/plan.py's
# BillingCycle) — CUSTOM has no fixed period, so it defaults to a
# monthly-equivalent contribution below rather than being excluded from MRR.
_MONTHLY_FACTOR = {"MONTHLY": 1.0, "ANNUAL": 1 / 12}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def build_billing_dashboard(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    # --- Revenue ---
    total_revenue = (await db.execute(
        select(func.coalesce(func.sum(Invoice.total), 0.0)).where(Invoice.status == InvoiceStatus.PAID)
    )).scalar_one()
    revenue_last_30d = (await db.execute(
        select(func.coalesce(func.sum(Invoice.total), 0.0)).where(
            Invoice.status == InvoiceStatus.PAID, Invoice.paid_at.isnot(None), Invoice.paid_at >= thirty_days_ago,
        )
    )).scalar_one()

    # --- MRR: every currently-active/trial/expiring subscription's amount, normalized to a monthly figure ---
    active_subs = list((await db.execute(
        select(Subscription).where(Subscription.status.in_(_ACTIVE_STATUSES))
    )).scalars().all())
    active_subs = [s for s in active_subs if (_aware(s.end_date) is None or _aware(s.end_date) > now)]
    mrr = round(sum((s.amount or 0.0) * _MONTHLY_FACTOR.get(s.billing_cycle.value, 1.0) for s in active_subs), 2)

    # --- Outstanding invoices ---
    outstanding_result = (await db.execute(
        select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.total), 0.0))
        .where(Invoice.status.in_(_OUTSTANDING_STATUSES))
    )).one()
    outstanding_count, outstanding_amount = outstanding_result

    # --- Subscription counts ---
    active_count = len(active_subs)
    expiring_count = sum(1 for s in active_subs if s.status == SubscriptionStatus.EXPIRING)
    expired_count = (await db.execute(
        select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.EXPIRED)
    )).scalar_one()

    # --- Payment health (last 30 days) ---
    payment_counts = (await db.execute(
        select(Payment.status, func.count(Payment.id)).where(Payment.created_at >= thirty_days_ago).group_by(Payment.status)
    )).all()
    payments_by_status = {status.value: count for status, count in payment_counts}
    succeeded_30d = payments_by_status.get(PaymentStatus.SUCCEEDED.value, 0)
    failed_30d = payments_by_status.get(PaymentStatus.FAILED.value, 0)
    total_30d = succeeded_30d + failed_30d
    success_rate = round((succeeded_30d / total_30d) * 100, 1) if total_30d else None

    # --- Gateway status ---
    gateways = list((await db.execute(select(PaymentGateway))).scalars().all())
    active_gateways = [g.provider.value for g in gateways if g.is_active]

    # --- Recent activity ---
    recent_invoices_rows = (await db.execute(
        select(Invoice, Organization).join(Organization, Organization.id == Invoice.organization_id)
        .order_by(Invoice.issue_date.desc()).limit(8)
    )).all()
    recent_invoices = [
        {
            "id": inv.id, "invoice_number": inv.invoice_number, "organization_name": org.name,
            "status": inv.status.value, "total": inv.total, "currency": inv.currency, "issue_date": inv.issue_date,
        }
        for inv, org in recent_invoices_rows
    ]

    recent_payments_rows = (await db.execute(
        select(Payment, Organization).join(Organization, Organization.id == Payment.organization_id)
        .order_by(Payment.created_at.desc()).limit(8)
    )).all()
    recent_payments = [
        {
            "id": p.id, "organization_name": org.name, "amount": p.amount, "currency": p.currency,
            "status": p.status.value, "payment_method": p.payment_method, "created_at": p.created_at,
        }
        for p, org in recent_payments_rows
    ]

    return {
        "total_revenue": round(total_revenue, 2),
        "revenue_last_30d": round(revenue_last_30d, 2),
        "mrr": mrr,
        "outstanding_invoices_count": outstanding_count,
        "outstanding_invoices_amount": round(outstanding_amount, 2),
        "active_subscriptions": active_count,
        "expiring_subscriptions": expiring_count,
        "expired_subscriptions": expired_count,
        "payments_succeeded_30d": succeeded_30d,
        "payments_failed_30d": failed_30d,
        "payment_success_rate": success_rate,
        "active_gateways": active_gateways,
        "configured_gateways": len(gateways),
        "recent_invoices": recent_invoices,
        "recent_payments": recent_payments,
    }
