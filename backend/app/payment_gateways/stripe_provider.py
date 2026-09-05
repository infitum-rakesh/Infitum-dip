"""
Stripe adapter — real code against Stripe's documented REST API (no
`stripe` SDK dependency; a plain httpx client against api.stripe.com,
same "call the vendor's real HTTP contract directly" approach already
used for e.g. the SAP HANA/Fabric connectors' native protocols).
DISCLOSED UNVERIFIED: no live Stripe account exists in this environment,
so this has never been exercised against a real charge — see
payment_gateways/base.py's module docstring for the "real but
unverified" precedent (Snowflake/SAP/Fabric/Salesforce connectors). The
Custom Gateway (custom_provider.py) is the one provider this codebase's
subscription -> invoice -> payment -> webhook architecture is actually
live-verified through (SB16/SB21).

Credentials shape (encrypted at rest by payment_gateway_service.py):
    {"secret_key": "sk_test_... or sk_live_...", "publishable_key": "pk_..."}
webhook_secret: the "whsec_..." signing secret from the Stripe Dashboard's
webhook endpoint configuration.

API version pinned via the Stripe-Version header so behavior doesn't
silently drift if Stripe's default API version changes.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx

from app.payment_gateways.base import (
    ConnectionTestResult,
    PaymentGatewayError,
    PaymentGatewayProvider,
    PaymentLinkResult,
    PaymentStatusResult,
    WebhookEvent,
)

_API_BASE = "https://api.stripe.com/v1"
_API_VERSION = "2024-06-20"
_WEBHOOK_TOLERANCE_SECONDS = 300  # reject replayed webhooks older than 5 minutes, per Stripe's own guidance

# Stripe event type -> this codebase's normalized WebhookEvent.event_type
_EVENT_TYPE_MAP = {
    "checkout.session.completed": "PAYMENT_SUCCESS",
    "payment_intent.succeeded": "PAYMENT_SUCCESS",
    "payment_intent.payment_failed": "PAYMENT_FAILED",
    "charge.refunded": "REFUND",
    "charge.dispute.created": "CHARGEBACK",
}

# Stripe payment_intent/checkout status -> this codebase's PaymentStatusResult.status
_STATUS_MAP = {
    "succeeded": "SUCCEEDED", "complete": "SUCCEEDED", "paid": "SUCCEEDED",
    "requires_payment_method": "PENDING", "requires_confirmation": "PENDING",
    "requires_action": "PENDING", "processing": "PENDING", "open": "PENDING",
    "canceled": "FAILED", "expired": "FAILED",
}


class StripeProvider(PaymentGatewayProvider):
    def _secret_key(self) -> str:
        key = self.credentials.get("secret_key")
        if not key:
            raise PaymentGatewayError("Stripe is not configured — set a secret_key in its credentials.")
        return key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._secret_key()}", "Stripe-Version": _API_VERSION}

    async def _post(self, path: str, data: dict) -> dict:
        # Stripe's REST API takes application/x-www-form-urlencoded with
        # bracketed keys for nested params (e.g. line_items[0][quantity]).
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{_API_BASE}{path}", headers=self._headers(), content=urlencode(data))
        return self._unwrap(resp)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{_API_BASE}{path}", headers=self._headers(), params=params or {})
        return self._unwrap(resp)

    @staticmethod
    def _unwrap(resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            raise PaymentGatewayError(f"Stripe returned a non-JSON response (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            message = body.get("error", {}).get("message", str(body))
            raise PaymentGatewayError(f"Stripe API error: {message}")
        return body

    async def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        try:
            balance = await self._get("/balance")
        except PaymentGatewayError as exc:
            return ConnectionTestResult(success=False, detail=str(exc))
        latency_ms = (time.monotonic() - started) * 1000
        live_mode = balance.get("livemode", False)
        return ConnectionTestResult(
            success=True, latency_ms=latency_ms,
            detail=f"Connected to Stripe ({'LIVE' if live_mode else 'TEST'} mode).",
        )

    def _line_item(self, invoice) -> dict:
        return {
            "line_items[0][price_data][currency]": invoice.currency.lower(),
            "line_items[0][price_data][product_data][name]": f"Invoice {invoice.invoice_number}",
            "line_items[0][price_data][unit_amount]": str(round(invoice.total * 100)),  # Stripe amounts are in the smallest currency unit
            "line_items[0][quantity]": "1",
        }

    async def create_payment_link(self, *, invoice, return_url: str) -> PaymentLinkResult:
        data = {
            **self._line_item(invoice),
            "after_completion[type]": "redirect",
            "after_completion[redirect][url]": return_url,
            "metadata[invoice_id]": str(invoice.id),
        }
        result = await self._post("/payment_links", data)
        return PaymentLinkResult(payment_url=result["url"], gateway_reference=result["id"])

    async def create_payment_session(self, *, invoice, return_url: str) -> PaymentLinkResult:
        data = {
            **self._line_item(invoice),
            "mode": "payment",
            "success_url": f"{return_url}?status=success",
            "cancel_url": f"{return_url}?status=cancelled",
            "metadata[invoice_id]": str(invoice.id),
        }
        result = await self._post("/checkout/sessions", data)
        return PaymentLinkResult(payment_url=result["url"], gateway_reference=result["id"])

    async def verify_payment(self, gateway_reference: str) -> PaymentStatusResult:
        session = await self._get(f"/checkout/sessions/{gateway_reference}")
        status = "SUCCEEDED" if session.get("payment_status") == "paid" else _STATUS_MAP.get(session.get("status"), "PENDING")
        return PaymentStatusResult(
            status=status, gateway_transaction_id=session.get("payment_intent"),
            amount=(session.get("amount_total") or 0) / 100, currency=(session.get("currency") or "usd").upper(),
            raw_reference=session,
        )

    async def get_payment_status(self, gateway_transaction_id: str) -> PaymentStatusResult:
        intent = await self._get(f"/payment_intents/{gateway_transaction_id}")
        return PaymentStatusResult(
            status=_STATUS_MAP.get(intent.get("status"), "PENDING"), gateway_transaction_id=intent.get("id"),
            amount=(intent.get("amount") or 0) / 100, currency=(intent.get("currency") or "usd").upper(),
            payment_method=intent.get("payment_method_types", [None])[0], raw_reference=intent,
        )

    async def refund_payment(self, gateway_transaction_id: str, amount: float | None = None) -> PaymentStatusResult:
        data = {"payment_intent": gateway_transaction_id}
        if amount is not None:
            data["amount"] = str(round(amount * 100))
        refund = await self._post("/refunds", data)
        return PaymentStatusResult(
            status="REFUNDED" if refund.get("status") == "succeeded" else "PENDING",
            gateway_transaction_id=gateway_transaction_id, amount=(refund.get("amount") or 0) / 100,
            currency=(refund.get("currency") or "usd").upper(), raw_reference=refund,
        )

    async def cancel_payment(self, gateway_reference: str) -> PaymentStatusResult:
        session = await self._post(f"/checkout/sessions/{gateway_reference}/expire", {})
        return PaymentStatusResult(status="FAILED", gateway_transaction_id=session.get("payment_intent"), raw_reference=session)

    def verify_webhook_signature(self, payload: bytes, signature_header: str | None) -> bool:
        """Stripe's real scheme: the Stripe-Signature header carries
        `t=<unix ts>,v1=<hex hmac>[,v0=...]`. Verify HMAC-SHA256 of
        "{t}.{payload}" against webhook_secret, constant-time, and
        reject anything outside the replay-tolerance window — exactly
        what Stripe's own client libraries do."""
        if not signature_header or not self.webhook_secret:
            return False
        parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
        timestamp, v1 = parts.get("t"), parts.get("v1")
        if not timestamp or not v1:
            return False
        if abs(time.time() - int(timestamp)) > _WEBHOOK_TOLERANCE_SECONDS:
            return False
        signed_payload = f"{timestamp}.".encode() + payload
        expected = hmac.new(self.webhook_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v1)

    def parse_webhook_event(self, payload: bytes) -> WebhookEvent:
        data = json.loads(payload)
        stripe_type = data.get("type", "")
        obj = data.get("data", {}).get("object", {})
        amount = obj.get("amount_total") or obj.get("amount")
        # metadata.invoice_id round-trips as a string through Stripe —
        # cast back to int here so payment_service.record_payment()'s
        # db.get(Invoice, invoice_id) gets the type it expects.
        raw_invoice_id = (obj.get("metadata") or {}).get("invoice_id")
        invoice_id = int(raw_invoice_id) if raw_invoice_id is not None and raw_invoice_id.isdigit() else None
        return WebhookEvent(
            event_id=data["id"], event_type=_EVENT_TYPE_MAP.get(stripe_type, stripe_type.upper()),
            gateway_transaction_id=obj.get("payment_intent") or obj.get("id"),
            amount=(amount / 100) if amount is not None else None,
            currency=(obj.get("currency") or "").upper() or None,
            raw_payload={**data, "invoice_id": invoice_id},
        )
