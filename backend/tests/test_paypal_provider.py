"""
Automated tests for app/payment_gateways/paypal_provider.py — written in
Phase 10 to close the automated-coverage gap on this disclosed-unverified
adapter (no live PayPal account is available in this environment) and to
lock in the real bugs found and fixed by Phase 10's static review — most
notably the missing Orders-API capture step, without which a PayPal
payment could never actually complete in this codebase's webhook-driven
architecture (see capture_order()'s docstring in paypal_provider.py).

Same real-local-HTTP-server convention as test_stripe_provider.py /
test_razorpay_provider.py. PayPal's API host is chosen per-request by
_api_base() (sandbox vs. live), not a module-level constant, so the
fixture monkeypatches that method directly rather than a `_API_BASE` name.
"""
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.payment_gateways.paypal_provider import PayPalProvider


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
        if self.path.startswith("/v2/checkout/orders/"):
            order_id = self.path.rsplit("/", 1)[-1]
            order = state["orders"].get(order_id)
            if order is None:
                return self._send_json(404, {"message": "no such order"})
            return self._send_json(200, order)
        return self._send_json(404, {"message": "not found"})

    def do_POST(self):
        state = self.server.state
        body = self._read_body()
        state["requests"].append({"method": "POST", "path": self.path, "headers": dict(self.headers.items()), "body": body.decode() if body else ""})

        if self.path == "/v1/oauth2/token":
            if state.get("force_oauth_error"):
                return self._send_json(400, {"error": "invalid_client", "error_description": "simulated bad credentials"})
            return self._send_json(200, {"access_token": "fake_access_token", "token_type": "Bearer", "expires_in": 3600})

        if state.get("force_error"):
            return self._send_json(state["force_error"], {"message": "simulated PayPal error"})

        if self.path == "/v2/checkout/orders":
            payload = json.loads(body)
            order_id = f"order_test_{len(state['orders']) + 1}"
            order = {
                "id": order_id, "status": "CREATED", "purchase_units": payload["purchase_units"],
                "links": [{"rel": "approve", "href": f"https://sandbox.paypal.com/checkoutnow?token={order_id}"}],
            }
            state["orders"][order_id] = order
            return self._send_json(201, order)

        if self.path.endswith("/capture") and "/v2/checkout/orders/" in self.path:
            order_id = self.path.split("/v2/checkout/orders/")[1].split("/capture")[0]
            order = state["orders"].get(order_id, {"id": order_id, "purchase_units": [{"amount": {"currency_code": "USD", "value": "0.00"}}]})
            unit = order["purchase_units"][0]
            capture_id = f"cap_test_{len(state['captures']) + 1}"
            capture = {"id": capture_id, "status": "COMPLETED", "amount": dict(unit["amount"])}
            state["captures"][capture_id] = capture
            unit["payments"] = {"captures": [capture]}
            order["status"] = "COMPLETED"
            return self._send_json(201, order)

        if self.path.startswith("/v2/payments/captures/") and self.path.endswith("/refund"):
            return self._send_json(201, state.get("refund_response") or {
                "id": "refund_test_1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "0.00"},
            })

        if self.path == "/v1/notifications/verify-webhook-signature":
            return self._send_json(200, {"verification_status": state.get("verify_status", "SUCCESS")})

        return self._send_json(404, {"message": "not found"})


@pytest.fixture
def paypal_server(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {
        "requests": [], "orders": {}, "captures": {}, "force_error": None, "force_oauth_error": False,
        "refund_response": None, "verify_status": "SUCCESS",
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setattr(PayPalProvider, "_api_base", lambda self: f"http://127.0.0.1:{port}")
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def provider():
    return PayPalProvider(credentials={"client_id": "fake_id", "client_secret": "fake_secret"}, webhook_secret=None, environment="TEST")


@pytest.mark.asyncio
async def test_test_connection_success(paypal_server, provider):
    result = await provider.test_connection()
    assert result.success is True


@pytest.mark.asyncio
async def test_test_connection_failure_surfaces_oauth_error(paypal_server, provider):
    paypal_server.state["force_oauth_error"] = True
    result = await provider.test_connection()
    assert result.success is False
    assert "PayPal OAuth error" in result.detail


@pytest.mark.asyncio
async def test_create_payment_link_returns_approve_url_and_idempotency_key(paypal_server, provider):
    invoice = FakeInvoice(id=42, invoice_number="INV-1001", total=50.0)
    result = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/billing")

    assert result.gateway_reference.startswith("order_test_")
    assert "checkoutnow?token=" in result.payment_url

    req = next(r for r in paypal_server.state["requests"] if r["path"] == "/v2/checkout/orders")
    assert req["headers"]["PayPal-Request-Id"] == "order-42"
    body = json.loads(req["body"])
    assert body["purchase_units"][0]["amount"]["value"] == "50.00"
    assert body["purchase_units"][0]["custom_id"] == "42"


@pytest.mark.asyncio
async def test_create_payment_session_is_alias_for_create_payment_link(paypal_server, provider):
    invoice = FakeInvoice(id=7, invoice_number="INV-2001", total=10.0)
    result = await provider.create_payment_session(invoice=invoice, return_url="https://app.example.com/pay")
    assert result.gateway_reference.startswith("order_test_")


@pytest.mark.asyncio
async def test_capture_order_is_the_real_mandatory_second_step(paypal_server, provider):
    """Real, critical bug fix: PayPal's Orders API with intent=CAPTURE does
    NOT auto-capture on buyer approval — without an explicit call to this
    endpoint, an order stays APPROVED forever and no money ever moves."""
    invoice = FakeInvoice(id=1, invoice_number="INV-1", total=25.0)
    link = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/x")

    captured = await provider.capture_order(link.gateway_reference)
    assert captured["status"] == "COMPLETED"

    req = next(r for r in paypal_server.state["requests"] if r["path"].endswith("/capture"))
    assert req["headers"]["PayPal-Request-Id"] == f"capture-{link.gateway_reference}"

    result = await provider.verify_payment(link.gateway_reference)
    assert result.status == "SUCCEEDED"
    assert result.amount == 25.0


@pytest.mark.asyncio
async def test_verify_payment_prefers_capture_amount_over_purchase_unit_amount(paypal_server, provider):
    """Real bug fix: a partial capture's actual moved amount can differ
    from the purchase unit's originally ordered amount — the old code
    always reported the latter."""
    invoice = FakeInvoice(id=2, invoice_number="INV-2", total=100.0)
    link = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/x")
    order = paypal_server.state["orders"][link.gateway_reference]
    order["purchase_units"][0]["payments"] = {
        "captures": [{"id": "cap_partial", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "40.00"}}],
    }
    order["status"] = "COMPLETED"

    result = await provider.verify_payment(link.gateway_reference)
    assert result.gateway_transaction_id == "cap_partial"
    assert result.amount == 40.0  # not the full $100.00 ordered


@pytest.mark.asyncio
async def test_get_payment_status_delegates_to_verify_payment(paypal_server, provider):
    invoice = FakeInvoice(id=3, invoice_number="INV-3", total=15.0)
    link = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/x")
    await provider.capture_order(link.gateway_reference)
    result = await provider.get_payment_status(link.gateway_reference)
    assert result.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_refund_payment_status_mapping_and_amount(paypal_server, provider):
    invoice = FakeInvoice(id=4, invoice_number="INV-4", total=60.0)
    link = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/x")
    await provider.capture_order(link.gateway_reference)

    paypal_server.state["refund_response"] = {"id": "refund_1", "status": "CANCELLED", "amount": {"currency_code": "USD", "value": "60.00"}}
    result = await provider.refund_payment(link.gateway_reference, amount=60.0)
    # Real bug fix: CANCELLED used to collapse into PENDING, masking a
    # genuinely failed/cancelled refund as merely in-progress.
    assert result.status == "FAILED"

    paypal_server.state["refund_response"] = {"id": "refund_2", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "60.00"}}
    result2 = await provider.refund_payment(link.gateway_reference, amount=60.0)
    assert result2.status == "REFUNDED"
    assert result2.amount == 60.0

    req = next(r for r in paypal_server.state["requests"] if r["path"].endswith("/refund"))
    assert req["headers"]["PayPal-Request-Id"] == "refund-cap_test_1-6000"


@pytest.mark.asyncio
async def test_refund_payment_raises_if_no_capture_found(paypal_server, provider):
    invoice = FakeInvoice(id=5, invoice_number="INV-5", total=20.0)
    link = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/x")
    with pytest.raises(Exception):
        await provider.refund_payment(link.gateway_reference, amount=20.0)


@pytest.mark.asyncio
async def test_cancel_payment_is_not_supported_by_paypal(paypal_server, provider):
    with pytest.raises(Exception):
        await provider.cancel_payment("order_test_1")


def test_verify_webhook_signature_always_returns_false(provider):
    """Documented, intentional limitation (not a bug): PayPal's real
    verification needs 5 headers this codebase's shared single-header
    webhook route can't carry, so this must fail closed rather than fake
    a pass."""
    assert provider.verify_webhook_signature(b"{}", "some-signature") is False
    assert provider.verify_webhook_signature(b"{}", None) is False


@pytest.mark.asyncio
async def test_verify_webhook_signature_via_api_success(paypal_server, provider):
    paypal_server.state["verify_status"] = "SUCCESS"
    result = await provider.verify_webhook_signature_via_api(
        transmission_id="t1", timestamp="2024-01-01T00:00:00Z", cert_url="https://api.paypal.com/cert",
        auth_algo="SHA256withRSA", transmission_sig="sig", webhook_id="wh_1", payload=b'{"id": "evt_1"}',
    )
    assert result is True


@pytest.mark.asyncio
async def test_verify_webhook_signature_via_api_failure(paypal_server, provider):
    paypal_server.state["verify_status"] = "FAILURE"
    result = await provider.verify_webhook_signature_via_api(
        transmission_id="t1", timestamp="2024-01-01T00:00:00Z", cert_url="https://api.paypal.com/cert",
        auth_algo="SHA256withRSA", transmission_sig="sig", webhook_id="wh_1", payload=b'{"id": "evt_1"}',
    )
    assert result is False


@pytest.mark.asyncio
async def test_verify_webhook_signature_via_api_malformed_payload_fails_closed(provider):
    result = await provider.verify_webhook_signature_via_api(
        transmission_id="t1", timestamp="ts", cert_url="url", auth_algo="algo", transmission_sig="sig",
        webhook_id="wh_1", payload=b"not json",
    )
    assert result is False


def _event(event_type: str, resource: dict, event_id: str = "evt_1") -> bytes:
    return json.dumps({"id": event_id, "event_type": event_type, "resource": resource}).encode()


def test_parse_checkout_order_approved_is_payment_pending(provider):
    payload = _event("CHECKOUT.ORDER.APPROVED", {
        "id": "order_1", "purchase_units": [{"custom_id": "42", "amount": {"value": "50.00", "currency_code": "USD"}}],
    })
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_PENDING"
    assert event.gateway_transaction_id == "order_1"
    assert event.raw_payload["invoice_id"] == 42


def test_parse_payment_capture_completed_is_payment_success(provider):
    payload = _event("PAYMENT.CAPTURE.COMPLETED", {
        "id": "cap_1", "custom_id": "42", "amount": {"value": "50.00", "currency_code": "USD"},
    })
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_SUCCESS"
    assert event.gateway_transaction_id == "cap_1"
    assert event.amount == 50.0
    assert event.raw_payload["invoice_id"] == 42


def test_parse_payment_capture_denied_is_payment_failed(provider):
    payload = _event("PAYMENT.CAPTURE.DENIED", {"id": "cap_2", "amount": {"value": "10.00", "currency_code": "USD"}})
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_FAILED"


def test_parse_payment_capture_refunded_is_refund(provider):
    payload = _event("PAYMENT.CAPTURE.REFUNDED", {"id": "cap_3", "amount": {"value": "10.00", "currency_code": "USD"}})
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "REFUND"


def test_parse_customer_dispute_created_is_chargeback(provider):
    payload = _event("CUSTOMER.DISPUTE.CREATED", {"id": "dp_1"})
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "CHARGEBACK"


def test_parse_unknown_event_type_falls_back_to_raw_type(provider):
    payload = _event("CHECKOUT.ORDER.COMPLETED", {"id": "order_2"})
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "CHECKOUT.ORDER.COMPLETED"
