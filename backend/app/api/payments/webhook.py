"""
POST /api/payments/webhook/{gateway} — spec sections 24/48. Deliberately
PUBLIC (no auth dependency) — a payment gateway cannot authenticate as a
platform user — secured instead by real cryptographic signature
verification against that gateway's own webhook secret
(PaymentGatewayProvider.verify_webhook_signature()). Idempotent: the
same event_id for the same gateway is recorded and processed exactly
once, via PaymentWebhookEvent's UniqueConstraint(gateway_id, event_id).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.billing.payment_gateway import GatewayProvider, PaymentGateway
from app.models.billing.payment_webhook_event import PaymentWebhookEvent, WebhookEventStatus
from app.models.seller_audit_log import record_seller_event
from app.services.billing import payment_gateway_service, payment_service

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/webhook/{gateway_name}")
async def receive_webhook(
    gateway_name: str,
    request: Request,
    x_webhook_signature: str | None = Header(None, alias="X-Webhook-Signature"),
    db: AsyncSession = Depends(get_db),
):
    try:
        provider_enum = GatewayProvider(gateway_name.upper())
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown gateway.")

    result = await db.execute(select(PaymentGateway).where(PaymentGateway.provider == provider_enum))
    gateway = result.scalar_one_or_none()
    if gateway is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gateway not configured.")

    raw_body = await request.body()
    try:
        provider = await payment_gateway_service.build_provider(db, gateway, environment=gateway.environment.value)
    except payment_gateway_service.PaymentGatewayServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    if not provider.verify_webhook_signature(raw_body, x_webhook_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature.")

    try:
        event = provider.parse_webhook_event(raw_body)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed webhook payload.")

    existing = await db.execute(
        select(PaymentWebhookEvent).where(PaymentWebhookEvent.gateway_id == gateway.id, PaymentWebhookEvent.event_id == event.event_id)
    )
    if existing.scalar_one_or_none() is not None:
        return {"status": "ignored_duplicate"}

    webhook_row = PaymentWebhookEvent(
        gateway_id=gateway.id, event_id=event.event_id, event_type=event.event_type,
        # SB32, found live: this stored event.raw_payload verbatim — the
        # model's own docstring promises a scrubbed payload, but nothing
        # here actually scrubbed it. Real Stripe/Razorpay webhook bodies
        # carry customer email/phone/card-brand fields alongside the
        # event; scrub the same way payment_service.record_payment()
        # already scrubs before persisting Payment.gateway_response_reference.
        payload_reference=payment_service.scrub_reference(event.raw_payload),
        status=WebhookEventStatus.RECEIVED,
    )
    db.add(webhook_row)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent delivery of the same event beat us to the unique
        # constraint — the exact race idempotency exists to catch.
        await db.rollback()
        return {"status": "ignored_duplicate"}
    await db.refresh(webhook_row)

    await record_seller_event(
        db, seller_user_id=None, customer_id=None, action="Payment Webhook Received",
        module="Billing", resource=gateway.display_name, resource_id=event.event_id,
        details=f"{event.event_type} ({gateway_name})",
    )

    invoice_id = event.raw_payload.get("invoice_id")
    try:
        if event.event_type == "PAYMENT_SUCCESS":
            if invoice_id is None:
                raise ValueError("Missing invoice_id in webhook payload.")
            await payment_service.record_payment(
                db, gateway_id=gateway.id, gateway_transaction_id=event.gateway_transaction_id,
                invoice_id=invoice_id, amount=event.amount, currency=event.currency, status="SUCCEEDED",
                payment_method=gateway_name.lower(), raw_reference=event.raw_payload,
            )
            webhook_row.status = WebhookEventStatus.PROCESSED
        elif event.event_type == "PAYMENT_FAILED":
            if invoice_id is not None:
                await payment_service.record_payment(
                    db, gateway_id=gateway.id, gateway_transaction_id=event.gateway_transaction_id,
                    invoice_id=invoice_id, amount=event.amount or 0.0, currency=event.currency or "USD",
                    status="FAILED", payment_method=gateway_name.lower(), raw_reference=event.raw_payload,
                )
            webhook_row.status = WebhookEventStatus.PROCESSED
        else:
            # A real, recognized-but-not-yet-handled event type (e.g.
            # REFUND/CHARGEBACK/SUBSCRIPTION_RENEWED) — acknowledged, not
            # an error, just no dispatch logic wired for it yet.
            webhook_row.status = WebhookEventStatus.PROCESSED
    except Exception as exc:
        webhook_row.status = WebhookEventStatus.FAILED
        webhook_row.processed_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    webhook_row.processed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "ok"}
