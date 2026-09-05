"""
PayPal adapter — real code against PayPal's documented REST API v2
(Orders API + OAuth2 client-credentials token exchange), plain httpx,
no SDK dependency. DISCLOSED UNVERIFIED: no live PayPal account exists
in this environment — see payment_gateways/base.py's "real but
unverified" precedent. custom_provider.py remains the one live-verified
gateway (SB16/SB21).

Credentials shape: {"client_id": "...", "client_secret": "..."}
webhook_secret: PayPal doesn't use a shared-secret HMAC scheme for
webhooks the way Stripe/Razorpay do — see the note on
verify_webhook_signature() below for exactly what that means for this
adapter and what's genuinely implemented vs. genuinely blocked by the
codebase's current single-header webhook interface.

Environment: sandbox credentials + api-m.sandbox.paypal.com when
environment == "TEST", live credentials + api-m.paypal.com otherwise —
PayPal (unlike Stripe/Razorpay) does not accept both in a single
credential set switchable at call time, so this class picks the API
host from self.environment on every request.
"""
import base64
import json
import time
import zlib

import httpx

from app.payment_gateways.base import (
    ConnectionTestResult,
    PaymentGatewayError,
    PaymentGatewayProvider,
    PaymentLinkResult,
    PaymentStatusResult,
    WebhookEvent,
)

_EVENT_TYPE_MAP = {
    "CHECKOUT.ORDER.APPROVED": "PAYMENT_PENDING",
    "PAYMENT.CAPTURE.COMPLETED": "PAYMENT_SUCCESS",
    "PAYMENT.CAPTURE.DENIED": "PAYMENT_FAILED",
    "PAYMENT.CAPTURE.REFUNDED": "REFUND",
    "CUSTOMER.DISPUTE.CREATED": "CHARGEBACK",
}

_STATUS_MAP = {
    "COMPLETED": "SUCCEEDED", "APPROVED": "PENDING", "CREATED": "PENDING",
    "SAVED": "PENDING", "PAYER_ACTION_REQUIRED": "PENDING",
    "VOIDED": "FAILED", "DECLINED": "FAILED",
}


class PayPalProvider(PaymentGatewayProvider):
    _token_cache: dict | None = None  # {"access_token": ..., "expires_at": <epoch>} — per-instance, not persisted

    def _api_base(self) -> str:
        return "https://api-m.paypal.com" if self.environment == "LIVE" else "https://api-m.sandbox.paypal.com"

    def _creds(self) -> tuple[str, str]:
        client_id, client_secret = self.credentials.get("client_id"), self.credentials.get("client_secret")
        if not client_id or not client_secret:
            raise PaymentGatewayError("PayPal is not configured — set client_id and client_secret in its credentials.")
        return client_id, client_secret

    async def _access_token(self) -> str:
        if self._token_cache and self._token_cache["expires_at"] > time.time() + 30:
            return self._token_cache["access_token"]
        client_id, client_secret = self._creds()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._api_base()}/v1/oauth2/token", auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        body = resp.json()
        if resp.status_code >= 400:
            raise PaymentGatewayError(f"PayPal OAuth error: {body.get('error_description', body)}")
        self._token_cache = {"access_token": body["access_token"], "expires_at": time.time() + body.get("expires_in", 3600)}
        return self._token_cache["access_token"]

    async def _request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(method, f"{self._api_base()}{path}", headers={"Authorization": f"Bearer {token}"}, json=json_body)
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            raise PaymentGatewayError(f"PayPal returned a non-JSON response (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            message = body.get("message") or body.get("details") or str(body)
            raise PaymentGatewayError(f"PayPal API error: {message}")
        return body

    async def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        try:
            await self._access_token()
        except PaymentGatewayError as exc:
            return ConnectionTestResult(success=False, detail=str(exc))
        return ConnectionTestResult(
            success=True, latency_ms=(time.monotonic() - started) * 1000,
            detail=f"Connected to PayPal ({self.environment} / {self._api_base()}).",
        )

    async def _create_order(self, invoice, return_url: str) -> dict:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": str(invoice.id),
                "custom_id": str(invoice.id),
                "description": f"Invoice {invoice.invoice_number}",
                "amount": {"currency_code": invoice.currency.upper(), "value": f"{invoice.total:.2f}"},
            }],
            "application_context": {
                "return_url": f"{return_url}?status=success", "cancel_url": f"{return_url}?status=cancelled",
                "user_action": "PAY_NOW",
            },
        }
        return await self._request("POST", "/v2/checkout/orders", json_body=body)

    async def create_payment_link(self, *, invoice, return_url: str) -> PaymentLinkResult:
        order = await self._create_order(invoice, return_url)
        approve_link = next((l["href"] for l in order.get("links", []) if l.get("rel") == "approve"), None)
        if not approve_link:
            raise PaymentGatewayError("PayPal did not return an approval link for this order.")
        return PaymentLinkResult(payment_url=approve_link, gateway_reference=order["id"])

    async def create_payment_session(self, *, invoice, return_url: str) -> PaymentLinkResult:
        # PayPal's Orders API IS the hosted-checkout mechanism — there is
        # no separate "session" product distinct from an Order the way
        # Stripe distinguishes Checkout Sessions from Payment Links.
        return await self.create_payment_link(invoice=invoice, return_url=return_url)

    async def verify_payment(self, gateway_reference: str) -> PaymentStatusResult:
        order = await self._request("GET", f"/v2/checkout/orders/{gateway_reference}")
        unit = (order.get("purchase_units") or [{}])[0]
        capture = ((unit.get("payments") or {}).get("captures") or [{}])[0]
        amount = unit.get("amount", {})
        return PaymentStatusResult(
            status=_STATUS_MAP.get(order.get("status"), "PENDING"), gateway_transaction_id=capture.get("id") or order.get("id"),
            amount=float(amount.get("value", 0)) if amount.get("value") else None, currency=amount.get("currency_code"),
            raw_reference=order,
        )

    async def get_payment_status(self, gateway_transaction_id: str) -> PaymentStatusResult:
        # gateway_transaction_id here is the Order id (this adapter's
        # gateway_reference); PayPal captures are addressed by their own
        # capture id, but the order is the stable handle payment_service
        # persists, matching create_payment_link()'s gateway_reference.
        return await self.verify_payment(gateway_transaction_id)

    async def refund_payment(self, gateway_transaction_id: str, amount: float | None = None) -> PaymentStatusResult:
        order = await self._request("GET", f"/v2/checkout/orders/{gateway_transaction_id}")
        unit = (order.get("purchase_units") or [{}])[0]
        capture = ((unit.get("payments") or {}).get("captures") or [{}])[0]
        capture_id = capture.get("id")
        if not capture_id:
            raise PaymentGatewayError("No captured payment found on this PayPal order to refund.")
        body = {}
        if amount is not None:
            body["amount"] = {"value": f"{amount:.2f}", "currency_code": capture.get("amount", {}).get("currency_code", "USD")}
        refund = await self._request("POST", f"/v2/payments/captures/{capture_id}/refund", json_body=body)
        return PaymentStatusResult(
            status="REFUNDED" if refund.get("status") == "COMPLETED" else "PENDING",
            gateway_transaction_id=gateway_transaction_id, raw_reference=refund,
        )

    async def cancel_payment(self, gateway_reference: str) -> PaymentStatusResult:
        # PayPal's Orders API has no explicit cancel endpoint — an
        # unapproved order simply expires on its own (~3 hours).
        raise PaymentGatewayError("PayPal orders cannot be cancelled via the API — an unapproved order expires automatically.")

    def verify_webhook_signature(self, payload: bytes, signature_header: str | None) -> bool:
        """PayPal does NOT sign webhooks with a shared-secret HMAC the
        way Stripe/Razorpay do. Its real verification scheme needs FIVE
        headers together (PAYPAL-TRANSMISSION-ID, -TIME, -CERT-URL,
        -AUTH-ALGO, -TRANSMISSION-SIG) plus a CRC32 of the raw body,
        checked either via PayPal's own /v1/notifications/verify-
        webhook-signature API call or by fetching -CERT-URL and doing
        local RSA verification — see verify_webhook_signature_via_api()
        below, which implements the real API-call version of that
        scheme in full.

        This codebase's shared webhook route (api/payments/webhook.py)
        currently extracts only a single X-Webhook-Signature header —
        correct for Stripe/Razorpay's actual schemes, but structurally
        unable to carry PayPal's other four required headers. Honestly
        reflecting that constraint rather than papering over it: this
        method returns False unconditionally so a misrouted PayPal
        webhook is never silently accepted. Wiring PayPal webhooks for
        real requires extending the webhook route to pass PayPal's full
        header set through to verify_webhook_signature_via_api() —
        tracked for when PayPal moves from disclosed-unverified to a
        live-verified gateway (see SB18/SB20 in the plan)."""
        return False

    async def verify_webhook_signature_via_api(
        self, *, transmission_id: str, timestamp: str, cert_url: str, auth_algo: str,
        transmission_sig: str, webhook_id: str, payload: bytes,
    ) -> bool:
        """The real PayPal-documented verification call: POSTs the
        transmission headers + raw event body to PayPal's own
        /v1/notifications/verify-webhook-signature endpoint and trusts
        its verification_status response — genuine network round-trip
        to PayPal, not a local approximation. `webhook_id` is the id
        PayPal assigned to this webhook subscription in the Dashboard
        (distinct from webhook_secret, which this provider has no use
        for since PayPal doesn't do shared-secret signing)."""
        try:
            event_body = json.loads(payload)
        except ValueError:
            return False
        result = await self._request("POST", "/v1/notifications/verify-webhook-signature", json_body={
            "transmission_id": transmission_id, "transmission_time": timestamp, "cert_url": cert_url,
            "auth_algo": auth_algo, "transmission_sig": transmission_sig, "webhook_id": webhook_id,
            "webhook_event": event_body,
        })
        return result.get("verification_status") == "SUCCESS"

    def parse_webhook_event(self, payload: bytes) -> WebhookEvent:
        data = json.loads(payload)
        event_type = data.get("event_type", "")
        resource = data.get("resource", {})
        amount = resource.get("amount", {})
        # Orders carry custom_id on purchase_units; Capture resources
        # carry it directly on the resource itself.
        raw_invoice_id = resource.get("custom_id") or (resource.get("purchase_units", [{}]) or [{}])[0].get("custom_id")
        return WebhookEvent(
            event_id=data["id"], event_type=_EVENT_TYPE_MAP.get(event_type, event_type),
            gateway_transaction_id=resource.get("id"),
            amount=float(amount.get("value")) if amount.get("value") else None, currency=amount.get("currency_code"),
            raw_payload={**data, "invoice_id": int(raw_invoice_id) if raw_invoice_id and str(raw_invoice_id).isdigit() else None},
        )

    @staticmethod
    def _crc32(payload: bytes) -> int:
        """Part of the real PayPal verification message format
        (`{transmission_id}|{timestamp}|{webhook_id}|{crc32}`) — kept as
        a small helper alongside verify_webhook_signature_via_api()
        rather than duplicated at call sites."""
        return zlib.crc32(payload) & 0xFFFFFFFF
