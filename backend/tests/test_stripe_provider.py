"""
Automated tests for app/payment_gateways/stripe_provider.py — written in
Phase 9 to close the automated-coverage gap on this disclosed-unverified
adapter (no live Stripe account is available in this environment) and to
lock in the real bugs found and fixed by Phase 9's static review.

stripe_provider.py hits a hardcoded https://api.stripe.com/v1 — there is
no Connection/connector seam to substitute a double at (same situation as
document_webhook_service.py, see that test file's own docstring). A real
local HTTP server standing in for api.stripe.com, with `_API_BASE`
monkeypatched to point at it, is the "real, not mocked" way to test this:
the real httpx client, real request construction/encoding, and real
response-parsing logic all run unmodified against real HTTP responses —
only the network boundary to Stripe's actual servers is swapped out
(exactly the same principle as mock_erp_adapter.py's docstring).
"""
import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.payment_gateways import stripe_provider as sp
from app.payment_gateways.stripe_provider import StripeProvider


@dataclass
class FakeInvoice:
    id: int
    invoice_number: str
    total: float
    currency: str = "USD"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state = self.server.state
        state["requests"].append({"method": "GET", "path": self.path, "headers": dict(self.headers.items())})
        if state.get("force_error"):
            return self._send_json(state["force_error"], {"error": {"message": "simulated Stripe error"}})
        if self.path == "/v1/balance":
            return self._send_json(200, {"livemode": False, "object": "balance"})
        if self.path.startswith("/v1/checkout/sessions/"):
            session_id = self.path.rsplit("/", 1)[-1]
            session = state["sessions"].get(session_id)
            if session is None:
                return self._send_json(404, {"error": {"message": "No such checkout session"}})
            return self._send_json(200, session)
        if self.path.startswith("/v1/payment_intents/"):
            intent_id = self.path.rsplit("/", 1)[-1]
            intent = state["intents"].get(intent_id, {
                "id": intent_id, "object": "payment_intent", "status": "succeeded",
                "amount": 5000, "currency": "usd", "payment_method_types": ["card"],
            })
            return self._send_json(200, intent)
        return self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        state = self.server.state
        body = self._read_body()
        state["requests"].append({"method": "POST", "path": self.path, "headers": dict(self.headers.items()), "body": body.decode()})

        if state.get("force_error"):
            return self._send_json(state["force_error"], {"error": {"message": "simulated Stripe error"}})

        if self.path == "/v1/checkout/sessions":
            session_id = f"cs_test_{len(state['sessions']) + 1}"
            session = {
                "id": session_id, "object": "checkout.session", "url": f"https://checkout.stripe.com/pay/{session_id}",
                "payment_status": "unpaid", "status": "open", "payment_intent": f"pi_test_{session_id}",
                "amount_total": 5000, "currency": "usd", "expires_at": int(time.time()) + 86400,
            }
            state["sessions"][session_id] = session
            return self._send_json(200, session)

        if self.path.endswith("/expire") and "/checkout/sessions/" in self.path:
            session_id = self.path.split("/checkout/sessions/")[1].split("/expire")[0]
            session = state["sessions"].get(session_id, {"id": session_id})
            session["status"] = "expired"
            return self._send_json(200, session)

        if self.path == "/v1/refunds":
            return self._send_json(200, state.get("refund_response", {
                "id": "re_test_1", "object": "refund", "status": "succeeded", "amount": 5000, "currency": "usd",
            }))

        return self._send_json(404, {"error": {"message": "not found"}})


@pytest.fixture
def stripe_server(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {"requests": [], "sessions": {}, "intents": {}, "force_error": None, "refund_response": None}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setattr(sp, "_API_BASE", f"http://127.0.0.1:{port}/v1")
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def provider():
    return StripeProvider(credentials={"secret_key": "sk_test_fake"}, webhook_secret="whsec_test_secret", environment="TEST")


@pytest.mark.asyncio
async def test_test_connection_success(stripe_server, provider):
    result = await provider.test_connection()
    assert result.success is True
    assert "TEST" in result.detail


@pytest.mark.asyncio
async def test_test_connection_failure_surfaces_stripe_error(stripe_server, provider):
    stripe_server.state["force_error"] = 401
    result = await provider.test_connection()
    assert result.success is False
    assert "Stripe API error" in result.detail


@pytest.mark.asyncio
async def test_create_payment_link_creates_a_checkout_session(stripe_server, provider):
    invoice = FakeInvoice(id=42, invoice_number="INV-1001", total=50.0)
    result = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/billing")

    assert result.gateway_reference.startswith("cs_test_")
    assert result.payment_url.startswith("https://checkout.stripe.com/pay/")
    assert result.expires_at is not None

    req = next(r for r in stripe_server.state["requests"] if r["path"] == "/v1/checkout/sessions")
    assert "Idempotency-Key" in req["headers"]
    assert req["headers"]["Idempotency-Key"] == "payment-link-invoice-42"
    assert "line_items%5B0%5D%5Bprice_data%5D%5Bunit_amount%5D=5000" in req["body"]  # $50.00 -> 5000 cents


@pytest.mark.asyncio
async def test_create_payment_session_uses_distinct_success_cancel_urls(stripe_server, provider):
    invoice = FakeInvoice(id=7, invoice_number="INV-2001", total=100.0)
    await provider.create_payment_session(invoice=invoice, return_url="https://app.example.com/pay")

    req = next(r for r in stripe_server.state["requests"] if r["path"] == "/v1/checkout/sessions")
    assert "success_url=https%3A%2F%2Fapp.example.com%2Fpay%3Fstatus%3Dsuccess" in req["body"]
    assert "cancel_url=https%3A%2F%2Fapp.example.com%2Fpay%3Fstatus%3Dcancelled" in req["body"]


@pytest.mark.asyncio
async def test_verify_payment_on_the_exact_session_create_returns(stripe_server, provider):
    """Regression for the real ID-space bug: verify_payment() must
    successfully resolve the SAME gateway_reference create_payment_link()
    just returned — before the fix this 404'd because create_payment_link
    produced a Payment Link id while verify_payment always read from
    /checkout/sessions."""
    invoice = FakeInvoice(id=1, invoice_number="INV-1", total=25.0)
    link = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/x")

    result = await provider.verify_payment(link.gateway_reference)
    assert result.status == "PENDING"  # payment_status starts "unpaid" in our fake server
    assert result.amount == 50.0  # from the fake server's fixed amount_total=5000

    stripe_server.state["sessions"][link.gateway_reference]["payment_status"] = "paid"
    result2 = await provider.verify_payment(link.gateway_reference)
    assert result2.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_cancel_payment_on_the_exact_session_create_returns(stripe_server, provider):
    invoice = FakeInvoice(id=2, invoice_number="INV-2", total=10.0)
    link = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/x")
    result = await provider.cancel_payment(link.gateway_reference)
    assert result.status == "FAILED"


@pytest.mark.asyncio
async def test_get_payment_status_maps_succeeded(stripe_server, provider):
    result = await provider.get_payment_status("pi_test_123")
    assert result.status == "SUCCEEDED"
    assert result.amount == 50.0


@pytest.mark.asyncio
async def test_refund_payment_status_mapping_and_idempotency(stripe_server, provider):
    stripe_server.state["refund_response"] = {"id": "re_1", "object": "refund", "status": "failed", "amount": 5000, "currency": "usd"}
    result = await provider.refund_payment("pi_test_abc", amount=50.0)
    assert result.status == "FAILED"  # real bug fix: used to collapse to PENDING

    req = next(r for r in stripe_server.state["requests"] if r["path"] == "/v1/refunds")
    assert req["headers"]["Idempotency-Key"] == "refund-pi_test_abc-5000"

    stripe_server.state["refund_response"] = {"id": "re_2", "object": "refund", "status": "succeeded", "amount": 5000, "currency": "usd"}
    result2 = await provider.refund_payment("pi_test_abc", amount=50.0)
    assert result2.status == "REFUNDED"


def test_verify_webhook_signature_valid(provider):
    body = b'{"id": "evt_1"}'
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec_test_secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    assert provider.verify_webhook_signature(body, header) is True


def test_verify_webhook_signature_checks_all_v1_candidates_during_rotation(provider):
    """Real bug fix: during a Stripe signing-secret rotation, the header
    can carry more than one v1= entry — verification must succeed if ANY
    of them matches, not just the last one a naive dict-comprehension
    would keep."""
    body = b'{"id": "evt_2"}'
    ts = str(int(time.time()))
    real_sig = hmac.new(b"whsec_test_secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    header = f"t={ts},v1=deadbeefdeadbeef,v1={real_sig}"
    assert provider.verify_webhook_signature(body, header) is True


def test_verify_webhook_signature_wrong_secret_fails(provider):
    body = b'{"id": "evt_3"}'
    ts = str(int(time.time()))
    sig = hmac.new(b"wrong_secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    assert provider.verify_webhook_signature(body, header) is False


def test_verify_webhook_signature_expired_timestamp_fails(provider):
    body = b'{"id": "evt_4"}'
    ts = str(int(time.time()) - 10_000)  # far outside the 5-minute tolerance
    sig = hmac.new(b"whsec_test_secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    assert provider.verify_webhook_signature(body, header) is False


def test_verify_webhook_signature_malformed_header_fails_closed_not_raises(provider):
    """Real bug fix: a garbled Stripe-Signature header (non-numeric
    timestamp) used to raise ValueError unhandled — on a public,
    unauthenticated webhook route that must fail closed, not 500."""
    body = b'{"id": "evt_5"}'
    header = "t=not-a-number,v1=deadbeef"
    assert provider.verify_webhook_signature(body, header) is False


def test_verify_webhook_signature_missing_header_fails(provider):
    assert provider.verify_webhook_signature(b"{}", None) is False


def test_verify_webhook_signature_no_secret_configured_fails():
    provider = StripeProvider(credentials={"secret_key": "sk_test_x"}, webhook_secret=None, environment="TEST")
    assert provider.verify_webhook_signature(b"{}", "t=1,v1=abc") is False


def _event(event_type: str, obj: dict, event_id: str = "evt_1") -> bytes:
    return json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}}).encode()


def test_parse_checkout_session_completed_paid_is_payment_success(provider):
    payload = _event("checkout.session.completed", {
        "id": "cs_1", "payment_status": "paid", "payment_intent": "pi_1",
        "amount_total": 5000, "currency": "usd", "metadata": {"invoice_id": "42"},
    })
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_SUCCESS"
    assert event.gateway_transaction_id == "pi_1"
    assert event.amount == 50.0
    assert event.currency == "USD"
    assert event.raw_payload["invoice_id"] == 42


def test_parse_checkout_session_completed_unpaid_is_payment_pending(provider):
    """Real bug fix: an async-payment-method checkout can fire
    checkout.session.completed with payment_status=unpaid — must not be
    reported as PAYMENT_SUCCESS."""
    payload = _event("checkout.session.completed", {
        "id": "cs_2", "payment_status": "unpaid", "payment_intent": "pi_2", "amount_total": 5000, "currency": "usd",
    })
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_PENDING"


def test_parse_payment_intent_succeeded(provider):
    payload = _event("payment_intent.succeeded", {"id": "pi_3", "amount": 10000, "currency": "usd"})
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_SUCCESS"
    assert event.gateway_transaction_id == "pi_3"
    assert event.amount == 100.0


def test_parse_payment_intent_payment_failed(provider):
    payload = _event("payment_intent.payment_failed", {"id": "pi_4", "amount": 2000, "currency": "usd"})
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_FAILED"


def test_parse_charge_refunded_uses_amount_refunded_not_full_charge(provider):
    """Real bug fix: charge.refunded's `amount` field is the ORIGINAL
    charge total, not what was actually refunded (a partial refund).
    That's `amount_refunded`."""
    payload = _event("charge.refunded", {
        "id": "ch_1", "payment_intent": "pi_5", "amount": 10000, "amount_refunded": 3000, "currency": "usd",
    })
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "REFUND"
    assert event.amount == 30.0  # the partial refund amount, not the full $100 charge


def test_parse_charge_dispute_created_is_chargeback(provider):
    payload = _event("charge.dispute.created", {"id": "dp_1", "amount": 5000, "currency": "usd"})
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "CHARGEBACK"


def test_parse_unknown_event_type_falls_back_to_uppercased_raw_type(provider):
    payload = _event("customer.updated", {"id": "cus_1"})
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "CUSTOMER.UPDATED"
