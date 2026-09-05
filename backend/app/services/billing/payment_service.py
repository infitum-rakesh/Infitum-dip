"""
Real payment recording — spec sections 22-26, 48. Never activated by a
frontend "payment_success" claim (section 23): every path here either
comes from a cryptographically-verified webhook (api/payments/webhook.py)
or a direct gateway status re-fetch. Idempotent on
(gateway_id, gateway_transaction_id) — the same webhook arriving twice
produces exactly one Payment row, never a duplicate activation.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing.invoice import Invoice, InvoiceStatus
from app.models.billing.payment import Payment, PaymentStatus
from app.models.billing.payment_link import PaymentLink, PaymentLinkStatus
from app.models.organization import Organization
from app.models.plan import Plan
from app.models.seller_audit_log import record_seller_event
from app.models.subscription import Subscription, SubscriptionStatus
from app.services.billing import notification_service
from app.services.seller import subscription_service as sub_svc


class PaymentServiceError(Exception):
    pass


_SECRET_KEY_HINTS = ("card", "cvv", "secret", "password", "pan", "cvc", "token", "authorization", "email", "phone", "contact")


def scrub_reference(raw: dict | None) -> dict:
    """Real gateways generally don't echo card data back in webhook
    payloads/status responses, but this guards against any that do —
    same defense-in-depth precedent as connectors/base.py's
    scrub_secrets() for connection errors. Public (not just used by
    record_payment()) — api/payments/webhook.py's raw persistence of
    PaymentWebhookEvent.payload_reference calls this too (SB32: found
    live that it wasn't, despite the model's own docstring promising a
    scrubbed payload)."""
    if not raw:
        return {}
    scrubbed = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            scrubbed[k] = scrub_reference(v)
        elif isinstance(v, str) and any(hint in k.lower() for hint in _SECRET_KEY_HINTS):
            scrubbed[k] = "••••••••"
        else:
            scrubbed[k] = v
    return scrubbed


# Back-compat alias for any in-module callers written against the old name.
_scrub_reference = scrub_reference


async def record_payment(
    db: AsyncSession, *, gateway_id: int, gateway_transaction_id: str, invoice_id: int,
    amount: float, currency: str, status: str, payment_method: str | None = None,
    raw_reference: dict | None = None,
) -> tuple[Payment, bool]:
    """Returns (payment, created). created=False means this exact
    (gateway_id, gateway_transaction_id) was already recorded — a
    duplicate webhook delivery, safely treated as a no-op rather than a
    second charge/activation (spec section 48)."""
    existing = await db.execute(
        select(Payment).where(Payment.gateway_id == gateway_id, Payment.gateway_transaction_id == gateway_transaction_id)
    )
    payment = existing.scalar_one_or_none()
    if payment is not None:
        return payment, False

    invoice = await db.get(Invoice, invoice_id)
    if invoice is None:
        raise PaymentServiceError(f"Invoice {invoice_id} not found.")

    # Server-side verification, not trust (spec section 23): a claimed
    # SUCCEEDED payment must match the invoice's own amount/currency, or
    # it is rejected outright rather than activating anything.
    if status == "SUCCEEDED" and (round(amount, 2) != round(invoice.total, 2) or currency != invoice.currency):
        raise PaymentServiceError(
            f"Payment amount/currency mismatch: got {amount} {currency}, invoice {invoice.invoice_number} expects {invoice.total} {invoice.currency}."
        )

    payment = Payment(
        invoice_id=invoice.id, subscription_id=invoice.subscription_id, organization_id=invoice.organization_id,
        gateway_id=gateway_id, gateway_transaction_id=gateway_transaction_id, amount=amount, currency=currency,
        status=PaymentStatus(status), payment_method=payment_method, gateway_response_reference=_scrub_reference(raw_reference),
        paid_at=datetime.now(timezone.utc) if status == "SUCCEEDED" else None,
    )
    db.add(payment)
    await db.flush()

    if status == "SUCCEEDED":
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = payment.paid_at
        if invoice.subscription_id:
            sub = await db.get(Subscription, invoice.subscription_id)
            if sub and sub.status in (SubscriptionStatus.PENDING, SubscriptionStatus.PAYMENT_PENDING, SubscriptionStatus.PAST_DUE):
                sub.status = SubscriptionStatus.ACTIVE
                await db.flush()
                await sub_svc.sync_entitlements_status(db, sub)
                org = await db.get(Organization, sub.organization_id)
                plan = await db.get(Plan, sub.plan_id) if sub.plan_id else None
                await sub_svc.sync_org_from_subscription(db, org, sub, plan)
        pl_result = await db.execute(
            select(PaymentLink).where(PaymentLink.invoice_id == invoice.id, PaymentLink.status == PaymentLinkStatus.ACTIVE)
        )
        for pl in pl_result.scalars().all():
            pl.status = PaymentLinkStatus.PAID
    elif status == "FAILED":
        invoice.status = InvoiceStatus.PAYMENT_PENDING  # stays payable — spec section 26, "Retry Payment"
    elif status == "REFUNDED":
        invoice.status = InvoiceStatus.PARTIALLY_PAID if amount < invoice.total else InvoiceStatus.ISSUED

    await db.commit()
    await db.refresh(payment)
    await record_seller_event(
        db, seller_user_id=None, customer_id=invoice.organization_id,
        action="Payment Successful" if status == "SUCCEEDED" else "Payment Failed",
        module="Billing", resource=invoice.invoice_number, resource_id=str(payment.id),
        details=f"{amount} {currency} via gateway #{gateway_id}, transaction {gateway_transaction_id}",
    )

    # SB31, found live: a real payment through the Custom Gateway produced
    # a real Payment row + audit event but never a customer-facing
    # Notification (in-app bell, SB23/24), even though the original spec
    # explicitly lists "payment successful/failed" among the events that
    # must notify. Wired here rather than in the webhook route so it
    # fires no matter which caller reaches record_payment().
    amount_str = f"{amount:,.2f} {currency}"
    if status == "SUCCEEDED":
        await notification_service.create_notification(
            db, organization_id=invoice.organization_id, type="PAYMENT_SUCCESSFUL",
            title="Payment received", body=f"Your payment of {amount_str} for invoice {invoice.invoice_number} was successful.",
            related_object_type="payment", related_object_id=payment.id,
        )
    elif status == "FAILED":
        await notification_service.create_notification(
            db, organization_id=invoice.organization_id, type="PAYMENT_FAILED",
            title="Payment failed", body=f"Your payment of {amount_str} for invoice {invoice.invoice_number} did not go through. Please try again.",
            related_object_type="payment", related_object_id=payment.id,
        )
    return payment, True


async def list_payments(
    db: AsyncSession, *, organization_id: int | None = None, page: int = 1, page_size: int = 20,
) -> tuple[list[Payment], int]:
    from sqlalchemy import func
    stmt = select(Payment)
    if organization_id is not None:
        stmt = stmt.where(Payment.organization_id == organization_id)
    stmt = stmt.order_by(Payment.created_at.desc())
    count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = count_result.scalar_one()
    result = await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    return list(result.scalars().all()), total
