"""
Custom/Manual Gateway — spec section 18's "Custom Gateway" example,
implemented for real: a self-hosted checkout flow with a real HMAC-SHA256
signed payment link, a real signature-verified webhook callback, and no
external SDK/account dependency. This is the ONE provider this codebase
live-verifies the entire subscription -> invoice -> payment-link ->
checkout -> webhook -> activation architecture through (confirmed with
the user — see SB16 in ~/.claude/plans/inherited-inventing-harp.md).
Stripe/Razorpay/PayPal (stripe_provider.py etc.) are real adapter code
against each provider's own documented API, disclosed unverified without
a live account — same "real but unverified" precedent already
established for the Snowflake/SAP/Fabric/Salesforce connectors.

Disclosed plainly in the UI (schemas/billing/payment_gateways.py's
description field): this is not a real card processor. It's the
reference implementation proving the abstraction, and a legitimate
choice for a Seller who takes payment offline/via wire transfer and
wants to record it against a real invoice/payment-link flow.
"""
import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

from app.payment_gateways.base import (
    ConnectionTestResult,
    PaymentGatewayError,
    PaymentGatewayProvider,
    PaymentLinkResult,
    PaymentStatusResult,
    WebhookEvent,
)


class CustomProvider(PaymentGatewayProvider):
    def _secret(self) -> str:
        secret = self.credentials.get("internal_secret")
        if not secret:
            raise PaymentGatewayError("Custom Gateway is not configured — set an internal signing secret in its credentials.")
        return secret

    async def test_connection(self) -> ConnectionTestResult:
        try:
            self._secret()
        except PaymentGatewayError as exc:
            return ConnectionTestResult(success=False, detail=str(exc))
        return ConnectionTestResult(success=True, detail="Custom Gateway ready — internal signing secret configured.")

    def sign_token(self, invoice_id: int, expires_at: datetime) -> str:
        payload = f"{invoice_id}:{int(expires_at.timestamp())}"
        sig = hmac.new(self._secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        raw = f"{payload}:{sig}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    def decode_token(self, token: str) -> tuple[int, datetime] | None:
        """Returns (invoice_id, expires_at) if the token's signature is
        valid, else None (tampered, malformed, or wrong secret)."""
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode()).decode()
            invoice_id_str, ts_str, sig = raw.split(":")
            expected_sig = hmac.new(self._secret().encode(), f"{invoice_id_str}:{ts_str}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                return None
            return int(invoice_id_str), datetime.fromtimestamp(int(ts_str), tz=timezone.utc)
        except Exception:
            return None

    async def create_payment_link(self, *, invoice, return_url: str) -> PaymentLinkResult:
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        token = self.sign_token(invoice.id, expires_at)
        from app.core.config import get_settings
        base = get_settings().frontend_base_url
        return PaymentLinkResult(payment_url=f"{base}/pay/{token}", gateway_reference=token, expires_at=expires_at)

    async def create_payment_session(self, *, invoice, return_url: str) -> PaymentLinkResult:
        return await self.create_payment_link(invoice=invoice, return_url=return_url)

    async def verify_payment(self, gateway_reference: str) -> PaymentStatusResult:
        # Push-only — this gateway has no external status API to poll;
        # the webhook fired at checkout submission is authoritative.
        return PaymentStatusResult(status="PENDING", gateway_transaction_id=None)

    async def get_payment_status(self, gateway_transaction_id: str) -> PaymentStatusResult:
        return PaymentStatusResult(status="PENDING", gateway_transaction_id=gateway_transaction_id)

    async def refund_payment(self, gateway_transaction_id: str, amount: float | None = None) -> PaymentStatusResult:
        raise PaymentGatewayError("Refunds are not supported by the Custom Gateway — record manually if needed.")

    async def cancel_payment(self, gateway_reference: str) -> PaymentStatusResult:
        return PaymentStatusResult(status="FAILED", gateway_transaction_id=None)

    def verify_webhook_signature(self, payload: bytes, signature_header: str | None) -> bool:
        if not signature_header:
            return False
        expected = hmac.new(self._secret().encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature_header, expected)

    def parse_webhook_event(self, payload: bytes) -> WebhookEvent:
        data = json.loads(payload)
        return WebhookEvent(
            event_id=data["event_id"], event_type=data["event_type"],
            gateway_transaction_id=data.get("gateway_transaction_id"),
            amount=data.get("amount"), currency=data.get("currency"), raw_payload=data,
        )

    def build_webhook_payload(
        self, *, event_type: str, gateway_transaction_id: str, amount: float, currency: str, invoice_id: int,
    ) -> bytes:
        """The counterpart to parse_webhook_event() — used by the /pay/
        {token}/submit endpoint to construct the exact payload it then
        signs and POSTs to /api/payments/webhook/custom, exercising the
        real signature-verification path end to end rather than calling
        payment_service directly."""
        data = {
            "event_id": str(uuid.uuid4()), "event_type": event_type,
            "gateway_transaction_id": gateway_transaction_id, "amount": amount, "currency": currency,
            "invoice_id": invoice_id,
        }
        return json.dumps(data).encode()

    def sign_payload(self, payload: bytes) -> str:
        return hmac.new(self._secret().encode(), payload, hashlib.sha256).hexdigest()
