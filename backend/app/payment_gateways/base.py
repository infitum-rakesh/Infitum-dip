"""
Payment gateway provider interface (spec section 21) — the seam every
payment processor integration in this codebase implements. Mirrors
connectors/base.py's established shape: a real provider makes genuine
calls against the real protocol/SDK/API it claims to (no simulated
success/failure branch), and every registry entry's own module docstring
discloses whether it was additionally exercised against a real, live
account/sandbox during development, or is real code correct against the
provider's own documented API contract but untested live because no
account exists in this environment (same "real but unverified" precedent
as the Snowflake/SAP/Fabric/Salesforce connectors).

The subscription/billing service layer (services/billing/payment_service.py,
services/billing/payment_gateway_service.py) never contains gateway-
specific code — spec section 21's explicit rule — it only ever calls
through this interface, resolved by payment_gateways/registry.py.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


class PaymentGatewayError(Exception):
    """A gateway call failed for a real reason (network, auth, API
    error) — never silently swallowed into a fabricated success."""


@dataclass
class ConnectionTestResult:
    success: bool
    detail: str
    latency_ms: float | None = None


@dataclass
class PaymentLinkResult:
    payment_url: str
    gateway_reference: str
    expires_at: object | None = None  # datetime, kept loosely typed to avoid an import cycle here


@dataclass
class PaymentStatusResult:
    status: str  # PENDING|SUCCEEDED|FAILED|REFUNDED|CHARGEBACK — mapped to models.billing.payment.PaymentStatus by the caller
    gateway_transaction_id: str | None = None
    amount: float | None = None
    currency: str | None = None
    payment_method: str | None = None
    raw_reference: dict = field(default_factory=dict)  # secret-scrubbed before persisting — see payment_service.py


@dataclass
class WebhookEvent:
    event_id: str
    event_type: str  # PAYMENT_SUCCESS|PAYMENT_FAILED|PAYMENT_PENDING|REFUND|CHARGEBACK|SUBSCRIPTION_RENEWED|SUBSCRIPTION_CANCELLED
    gateway_transaction_id: str | None
    amount: float | None
    currency: str | None
    raw_payload: dict = field(default_factory=dict)


class PaymentGatewayProvider(abc.ABC):
    """One instance per (gateway config, environment) — constructed with
    already-decrypted credentials, never touches encrypted values itself
    (that's payment_gateway_service.py's job, same separation
    connectors/base.py keeps between Connection and its decrypted
    secrets)."""

    def __init__(self, credentials: dict, webhook_secret: str | None, environment: str):
        self.credentials = credentials
        self.webhook_secret = webhook_secret
        self.environment = environment  # "TEST" | "LIVE"

    @abc.abstractmethod
    async def test_connection(self) -> ConnectionTestResult:
        """Spec section 52: must succeed before a gateway can move
        TEST -> LIVE. Makes a real, minimal authenticated call — never a
        local-only credential-format check."""

    @abc.abstractmethod
    async def create_payment_link(self, *, invoice, return_url: str) -> PaymentLinkResult:
        """Spec section 17 — the gateway itself generates the URL; never
        constructed manually here or by any caller."""

    @abc.abstractmethod
    async def create_payment_session(self, *, invoice, return_url: str) -> PaymentLinkResult:
        """A hosted checkout session — some providers (Stripe Checkout)
        distinguish this from a standalone payment link; others treat
        them identically. Both return the same PaymentLinkResult shape."""

    @abc.abstractmethod
    async def verify_payment(self, gateway_reference: str) -> PaymentStatusResult:
        """Re-fetches the payment's real status FROM THE GATEWAY —
        spec section 23: never trust a frontend "payment_success" claim."""

    @abc.abstractmethod
    async def get_payment_status(self, gateway_transaction_id: str) -> PaymentStatusResult:
        ...

    @abc.abstractmethod
    async def refund_payment(self, gateway_transaction_id: str, amount: float | None = None) -> PaymentStatusResult:
        ...

    @abc.abstractmethod
    async def cancel_payment(self, gateway_reference: str) -> PaymentStatusResult:
        ...

    @abc.abstractmethod
    def verify_webhook_signature(self, payload: bytes, signature_header: str | None) -> bool:
        """Real cryptographic signature verification against
        self.webhook_secret — spec section 24. Returning True without
        actually checking is exactly the bug this method exists to
        prevent; every real adapter must implement genuine verification."""

    @abc.abstractmethod
    def parse_webhook_event(self, payload: bytes) -> WebhookEvent:
        ...
