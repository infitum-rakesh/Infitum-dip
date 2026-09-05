"""
GET /api/pay/{token}, POST /api/pay/{token}/submit — the Custom
Gateway's public checkout flow (spec sections 17/58's "Payment Link
UX"). Deliberately public/no-auth: the HMAC-signed token itself (see
payment_gateways/custom_provider.py) is the credential, exactly like a
real hosted-checkout link from any external gateway. On submit, this
fires a real, cryptographically-signed webhook back to
/api/payments/webhook/custom over an actual HTTP round-trip — the same
signature-verification + idempotency path a genuine external gateway's
callback would use — rather than calling payment_service directly, so
the whole architecture gets exercised end to end.
"""
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.models.billing.invoice import Invoice, InvoiceStatus
from app.models.billing.payment_gateway import GatewayProvider, PaymentGateway
from app.models.organization import Organization
from app.payment_gateways.custom_provider import CustomProvider
from app.services.billing import payment_gateway_service

router = APIRouter(prefix="/pay", tags=["pay"])


class CheckoutInvoiceOut(BaseModel):
    invoice_number: str
    organization_name: str
    total: float
    currency: str
    status: str
    already_paid: bool


async def _resolve_custom_gateway(db: AsyncSession) -> tuple[PaymentGateway, CustomProvider]:
    result = await db.execute(select(PaymentGateway).where(PaymentGateway.provider == GatewayProvider.CUSTOM))
    gateway = result.scalar_one_or_none()
    if gateway is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Custom Gateway is not configured.")
    try:
        provider = await payment_gateway_service.build_provider(db, gateway, environment=gateway.environment.value)
    except payment_gateway_service.PaymentGatewayServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if not isinstance(provider, CustomProvider):
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Custom Gateway misconfigured.")
    return gateway, provider


async def _decode_or_404(provider: CustomProvider, token: str) -> tuple[int, datetime]:
    decoded = provider.decode_token(token)
    if decoded is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This payment link is invalid.")
    invoice_id, expires_at = decoded
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This payment link has expired.")
    return invoice_id, expires_at


@router.get("/{token}", response_model=CheckoutInvoiceOut)
async def get_checkout_invoice(token: str, db: AsyncSession = Depends(get_db)):
    _, provider = await _resolve_custom_gateway(db)
    invoice_id, _ = await _decode_or_404(provider, token)
    invoice = await db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found.")
    org = await db.get(Organization, invoice.organization_id)
    return CheckoutInvoiceOut(
        invoice_number=invoice.invoice_number, organization_name=org.name if org else "—",
        total=invoice.total, currency=invoice.currency, status=invoice.status.value,
        already_paid=invoice.status == InvoiceStatus.PAID,
    )


@router.post("/{token}/submit")
async def submit_checkout(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    _, provider = await _resolve_custom_gateway(db)
    invoice_id, _ = await _decode_or_404(provider, token)
    invoice = await db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found.")
    if invoice.status == InvoiceStatus.PAID:
        return {"status": "already_paid"}

    gateway_transaction_id = f"custom_{uuid.uuid4()}"
    payload = provider.build_webhook_payload(
        event_type="PAYMENT_SUCCESS", gateway_transaction_id=gateway_transaction_id,
        amount=invoice.total, currency=invoice.currency, invoice_id=invoice.id,
    )
    signature = provider.sign_payload(payload)

    # Call back into this same origin — whatever base URL this request
    # actually arrived on (direct backend port, or via the frontend dev
    # proxy) — never a hardcoded host, so this works the same in every
    # environment.
    webhook_url = f"{str(request.base_url).rstrip('/')}/api/payments/webhook/custom"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                webhook_url, content=payload,
                headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not reach payment webhook: {exc}")

    if resp.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Webhook processing failed: {resp.text}")

    return {"status": "submitted", "webhook_response": resp.json()}
