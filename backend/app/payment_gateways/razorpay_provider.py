"""
Razorpay adapter — real code against Razorpay's documented REST API
(plain httpx, HTTP Basic auth with key_id/key_secret — Razorpay doesn't
ship a first-party async Python SDK, so this talks to api.razorpay.com
directly rather than pulling in the sync-only official `razorpay`
package). DISCLOSED UNVERIFIED: no live Razorpay account exists in this
environment — see payment_gateways/base.py's "real but unverified"
precedent. custom_provider.py remains the one live-verified gateway.

Credentials shape: {"key_id": "rzp_test_...", "key_secret": "..."}
webhook_secret: the secret configured against this webhook URL in the
Razorpay Dashboard's Webhooks settings (separate from key_secret).

Razorpay has no distinct hosted "Checkout Session" product the way
Stripe does — create_payment_session() is a real alias for
create_payment_link() (Razorpay's own Payment Links API IS its hosted
checkout), not a shortcut taken to skip real integration work.
"""
import hashlib
import hmac
import json
import time

import httpx

from app.payment_gateways.base import (
    ConnectionTestResult,
    PaymentGatewayError,
    PaymentGatewayProvider,
    PaymentLinkResult,
    PaymentStatusResult,
    WebhookEvent,
)

_API_BASE = "https://api.razorpay.com/v1"

# Razorpay webhook `event` field -> this codebase's normalized event_type
_EVENT_TYPE_MAP = {
    "payment_link.paid": "PAYMENT_SUCCESS",
    "payment.captured": "PAYMENT_SUCCESS",
    "payment.failed": "PAYMENT_FAILED",
    "payment_link.expired": "PAYMENT_FAILED",
    "refund.processed": "REFUND",
}

_STATUS_MAP = {
    "paid": "SUCCEEDED", "captured": "SUCCEEDED",
    "created": "PENDING", "authorized": "PENDING", "pending": "PENDING",
    "failed": "FAILED", "expired": "FAILED", "cancelled": "FAILED",
    "refunded": "REFUNDED",
}


class RazorpayProvider(PaymentGatewayProvider):
    def _auth(self) -> tuple[str, str]:
        key_id, key_secret = self.credentials.get("key_id"), self.credentials.get("key_secret")
        if not key_id or not key_secret:
            raise PaymentGatewayError("Razorpay is not configured — set key_id and key_secret in its credentials.")
        return key_id, key_secret

    async def _request(self, method: str, path: str, json_body: dict | None = None, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(method, f"{_API_BASE}{path}", auth=self._auth(), json=json_body, params=params)
        try:
            body = resp.json()
        except ValueError:
            raise PaymentGatewayError(f"Razorpay returned a non-JSON response (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            message = body.get("error", {}).get("description", str(body))
            raise PaymentGatewayError(f"Razorpay API error: {message}")
        return body

    async def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        try:
            await self._request("GET", "/payments", params={"count": 1})
        except PaymentGatewayError as exc:
            return ConnectionTestResult(success=False, detail=str(exc))
        return ConnectionTestResult(success=True, latency_ms=(time.monotonic() - started) * 1000, detail="Connected to Razorpay.")

    async def create_payment_link(self, *, invoice, return_url: str) -> PaymentLinkResult:
        body = {
            "amount": round(invoice.total * 100),  # Razorpay amounts are in paise (smallest currency unit)
            "currency": invoice.currency.upper(),
            "description": f"Invoice {invoice.invoice_number}",
            "callback_url": return_url,
            "callback_method": "get",
            "notes": {"invoice_id": str(invoice.id)},
        }
        result = await self._request("POST", "/payment_links", json_body=body)
        return PaymentLinkResult(payment_url=result["short_url"], gateway_reference=result["id"])

    async def create_payment_session(self, *, invoice, return_url: str) -> PaymentLinkResult:
        return await self.create_payment_link(invoice=invoice, return_url=return_url)

    async def verify_payment(self, gateway_reference: str) -> PaymentStatusResult:
        link = await self._request("GET", f"/payment_links/{gateway_reference}")
        payments = link.get("payments") or []
        latest = payments[-1] if payments else {}
        return PaymentStatusResult(
            status=_STATUS_MAP.get(link.get("status"), "PENDING"), gateway_transaction_id=latest.get("payment_id"),
            amount=(link.get("amount_paid") or 0) / 100, currency=(link.get("currency") or "INR").upper(),
            raw_reference=link,
        )

    async def get_payment_status(self, gateway_transaction_id: str) -> PaymentStatusResult:
        payment = await self._request("GET", f"/payments/{gateway_transaction_id}")
        return PaymentStatusResult(
            status=_STATUS_MAP.get(payment.get("status"), "PENDING"), gateway_transaction_id=payment.get("id"),
            amount=(payment.get("amount") or 0) / 100, currency=(payment.get("currency") or "INR").upper(),
            payment_method=payment.get("method"), raw_reference=payment,
        )

    async def refund_payment(self, gateway_transaction_id: str, amount: float | None = None) -> PaymentStatusResult:
        body = {}
        if amount is not None:
            body["amount"] = round(amount * 100)
        refund = await self._request("POST", f"/payments/{gateway_transaction_id}/refund", json_body=body)
        return PaymentStatusResult(
            status="REFUNDED" if refund.get("status") in ("processed", None) else "PENDING",
            gateway_transaction_id=gateway_transaction_id, amount=(refund.get("amount") or 0) / 100,
            currency=(refund.get("currency") or "INR").upper(), raw_reference=refund,
        )

    async def cancel_payment(self, gateway_reference: str) -> PaymentStatusResult:
        link = await self._request("POST", f"/payment_links/{gateway_reference}/cancel")
        return PaymentStatusResult(status="FAILED", gateway_transaction_id=None, raw_reference=link)

    def verify_webhook_signature(self, payload: bytes, signature_header: str | None) -> bool:
        """Razorpay's real scheme: HMAC-SHA256 of the raw request body
        using the webhook secret (configured separately from the API
        key/secret in the Dashboard), sent as X-Razorpay-Signature."""
        if not signature_header or not self.webhook_secret:
            return False
        expected = hmac.new(self.webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header)

    def parse_webhook_event(self, payload: bytes) -> WebhookEvent:
        data = json.loads(payload)
        event = data.get("event", "")
        entity_key = "payment_link" if event.startswith("payment_link.") else "payment" if event.startswith("payment.") else "refund"
        entity = (data.get("payload", {}).get(entity_key, {}) or {}).get("entity", {})
        # Razorpay does not send a dedicated top-level event id field on
        # every webhook version — build a stable synthetic id from the
        # entity id + event + created_at so PaymentWebhookEvent's
        # UniqueConstraint(gateway_id, event_id) still dedupes real
        # retried deliveries correctly.
        synthetic_id = f"{event}:{entity.get('id', '')}:{data.get('created_at', '')}"
        notes = entity.get("notes") or {}
        raw_invoice_id = notes.get("invoice_id")
        return WebhookEvent(
            event_id=synthetic_id, event_type=_EVENT_TYPE_MAP.get(event, event.upper()),
            gateway_transaction_id=entity.get("id"),
            amount=(entity.get("amount") / 100) if entity.get("amount") is not None else None,
            currency=(entity.get("currency") or "").upper() or None,
            raw_payload={**data, "invoice_id": int(raw_invoice_id) if raw_invoice_id and str(raw_invoice_id).isdigit() else None},
        )
