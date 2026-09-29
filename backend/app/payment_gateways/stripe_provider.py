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
from datetime import datetime, timezone
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

    async def _post(self, path: str, data: dict, *, idempotency_key: str | None = None) -> dict:
        # Stripe's REST API takes application/x-www-form-urlencoded with
        # bracketed keys for nested params (e.g. line_items[0][quantity]).
        # An explicit Idempotency-Key means a network-level retry of this
        # exact call (e.g. from httpx's own connection handling, or a
        # caller re-trying after a timeout) can never create a duplicate
        # object on Stripe's side — most load-bearing on /refunds, where a
        # duplicate would move real money twice.
        headers = self._headers()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{_API_BASE}{path}", headers=headers, content=urlencode(data))
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

    @staticmethod
    def _parse_expires_at(unix_ts) -> datetime | None:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc) if unix_ts is not None else None

    async def _create_checkout_session(self, *, invoice, success_url: str, cancel_url: str, idempotency_key: str) -> PaymentLinkResult:
        """Real bug found live (Phase 9): create_payment_link() used to
        call the Payment Links API (returning a `plink_...` id), while
        verify_payment()/cancel_payment() below assume a Checkout Session
        (`cs_...`) id — a Payment Link is a reusable, standing link with
        no per-attempt "payment status" of its own, so those calls would
        404 against a real Stripe account. Both create_* methods now
        create a real Checkout Session — a Payment Link and a one-time
        Checkout Session are functionally identical for "pay this one
        invoice" (Stripe's own docs recommend Checkout Sessions for
        exactly this use case) — so `gateway_reference` is always a
        Checkout Session id everywhere it's produced and consumed."""
        data = {
            **self._line_item(invoice),
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata[invoice_id]": str(invoice.id),
        }
        result = await self._post("/checkout/sessions", data, idempotency_key=idempotency_key)
        return PaymentLinkResult(
            payment_url=result["url"], gateway_reference=result["id"],
            expires_at=self._parse_expires_at(result.get("expires_at")),
        )

    async def create_payment_link(self, *, invoice, return_url: str) -> PaymentLinkResult:
        return await self._create_checkout_session(
            invoice=invoice, success_url=return_url, cancel_url=return_url,
            idempotency_key=f"payment-link-invoice-{invoice.id}",
        )

    async def create_payment_session(self, *, invoice, return_url: str) -> PaymentLinkResult:
        return await self._create_checkout_session(
            invoice=invoice, success_url=f"{return_url}?status=success", cancel_url=f"{return_url}?status=cancelled",
            idempotency_key=f"payment-session-invoice-{invoice.id}",
        )

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

    # Stripe refund status -> this codebase's PaymentStatusResult.status.
    # Real bug found live: the old code collapsed everything that wasn't
    # exactly "succeeded" into PENDING, so a genuinely FAILED refund (a
    # dead card, insufficient balance) silently looked like it was still
    # in progress instead of surfacing as something needing attention.
    _REFUND_STATUS_MAP = {"succeeded": "REFUNDED", "pending": "PENDING", "failed": "FAILED", "canceled": "FAILED"}

    async def refund_payment(self, gateway_transaction_id: str, amount: float | None = None) -> PaymentStatusResult:
        data = {"payment_intent": gateway_transaction_id}
        if amount is not None:
            data["amount"] = str(round(amount * 100))
        # Deterministic on (transaction, amount) — a genuine network-level
        # retry of the exact same refund call reuses this same key
        # (Stripe then just returns the already-created refund instead of
        # creating a second one); a real uuid here would defeat that by
        # minting a fresh key on every retry.
        idempotency_key = f"refund-{gateway_transaction_id}-{round((amount or 0) * 100)}"
        refund = await self._post("/refunds", data, idempotency_key=idempotency_key)
        return PaymentStatusResult(
            status=self._REFUND_STATUS_MAP.get(refund.get("status"), "PENDING"),
            gateway_transaction_id=gateway_transaction_id, amount=(refund.get("amount") or 0) / 100,
            currency=(refund.get("currency") or "usd").upper(), raw_reference=refund,
        )

    async def cancel_payment(self, gateway_reference: str) -> PaymentStatusResult:
        session = await self._post(f"/checkout/sessions/{gateway_reference}/expire", {})
        return PaymentStatusResult(status="FAILED", gateway_transaction_id=session.get("payment_intent"), raw_reference=session)

    def verify_webhook_signature(self, payload: bytes, signature_header: str | None) -> bool:
        """Stripe's real scheme: the Stripe-Signature header carries
        `t=<unix ts>,v1=<hex hmac>[,v1=<hex hmac during a secret
        rotation>,v0=...]`. Verify HMAC-SHA256 of "{t}.{payload}" against
        webhook_secret, constant-time, matching against ANY v1 value
        present (real bug found live: Stripe's docs explicitly say to
        check every v1 entry during a signing-secret rotation window —
        collapsing them into a plain dict kept only the last one, so a
        rotation-window webhook could spuriously fail verification), and
        reject anything outside the replay-tolerance window. A garbled
        header (non-numeric timestamp) fails closed (False) rather than
        raising — this runs on a public, unauthenticated webhook route,
        where an unhandled exception would surface as a 500 instead of a
        clean rejection."""
        if not signature_header or not self.webhook_secret:
            return False
        timestamp = None
        v1_candidates = []
        for part in signature_header.split(","):
            if "=" not in part:
                continue
            key, _, value = part.partition("=")
            if key == "t":
                timestamp = value
            elif key == "v1":
                v1_candidates.append(value)
        if not timestamp or not v1_candidates:
            return False
        try:
            if abs(time.time() - int(timestamp)) > _WEBHOOK_TOLERANCE_SECONDS:
                return False
        except ValueError:
            return False
        signed_payload = f"{timestamp}.".encode() + payload
        expected = hmac.new(self.webhook_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, v1) for v1 in v1_candidates)

    def parse_webhook_event(self, payload: bytes) -> WebhookEvent:
        data = json.loads(payload)
        stripe_type = data.get("type", "")
        obj = data.get("data", {}).get("object", {})

        # checkout.session.completed can fire with payment_status="unpaid"
        # for async payment methods (real bug found live: the old code
        # mapped this event straight to PAYMENT_SUCCESS regardless —
        # verify_payment() already checks payment_status correctly, this
        # brings the webhook path in line with it).
        event_type = _EVENT_TYPE_MAP.get(stripe_type, stripe_type.upper())
        if stripe_type == "checkout.session.completed" and obj.get("payment_status") != "paid":
            event_type = "PAYMENT_PENDING"

        # charge.refunded's `amount` is the ORIGINAL charge total, not
        # what was refunded — that's `amount_refunded` (real bug found
        # live: a partial refund was reported as the full charge amount).
        # Every other mapped event type (checkout session, payment
        # intent) genuinely wants amount_total/amount.
        if stripe_type == "charge.refunded":
            amount = obj.get("amount_refunded")
        else:
            amount = obj.get("amount_total") or obj.get("amount")

        # metadata.invoice_id round-trips as a string through Stripe —
        # cast back to int here so payment_service.record_payment()'s
        # db.get(Invoice, invoice_id) gets the type it expects.
        raw_invoice_id = (obj.get("metadata") or {}).get("invoice_id")
        invoice_id = int(raw_invoice_id) if raw_invoice_id is not None and raw_invoice_id.isdigit() else None
        return WebhookEvent(
            event_id=data["id"], event_type=event_type,
            gateway_transaction_id=obj.get("payment_intent") or obj.get("id"),
            amount=(amount / 100) if amount is not None else None,
            currency=(obj.get("currency") or "").upper() or None,
            raw_payload={**data, "invoice_id": invoice_id},
        )
