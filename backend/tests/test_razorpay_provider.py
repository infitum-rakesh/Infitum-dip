"""
Automated tests for app/payment_gateways/razorpay_provider.py — written in
Phase 10 to close the automated-coverage gap on this disclosed-unverified
adapter (no live Razorpay account is available in this environment) and to
lock in the real bugs found and fixed by Phase 10's static review.

Same real-local-HTTP-server convention as test_stripe_provider.py: this
adapter hits a hardcoded https://api.razorpay.com/v1 with no
Connection/connector seam to substitute a double at, so a real local
ThreadingHTTPServer stands in for it, with `_API_BASE` monkeypatched — the
real httpx client, request construction, and response-parsing logic all
run unmodified against real HTTP responses.
"""
import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.payment_gateways import razorpay_provider as rp
from app.payment_gateways.razorpay_provider import RazorpayProvider


@dataclass
class FakeInvoice:
    id: int
    invoice_number: str
    total: float
    currency: str = "INR"


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
            return self._send_json(state["force_error"], {"error": {"description": "simulated Razorpay error"}})
        path = self.path.split("?")[0]
        if path == "/payments":
            return self._send_json(200, {"entity": "collection", "count": 0, "items": []})
        if path.startswith("/payment_links/"):
            link_id = path.rsplit("/", 1)[-1]
            link = state["payment_links"].get(link_id)
            if link is None:
                return self._send_json(404, {"error": {"description": "no such payment link"}})
            return self._send_json(200, link)
        if path.startswith("/payments/"):
            payment_id = path.rsplit("/", 1)[-1]
            payment = state["payments"].get(payment_id, {
                "id": payment_id, "entity": "payment", "status": "captured",
                "amount": 5000, "currency": "INR", "method": "card",
            })
            return self._send_json(200, payment)
        return self._send_json(404, {"error": {"description": "not found"}})

    def do_POST(self):
        state = self.server.state
        body = self._read_body()
        state["requests"].append({"method": "POST", "path": self.path, "headers": dict(self.headers.items()), "body": body.decode()})

        if state.get("force_error"):
            return self._send_json(state["force_error"], {"error": {"description": "simulated Razorpay error"}})

        if self.path == "/payment_links":
            link_id = f"plink_test_{len(state['payment_links']) + 1}"
            payload = json.loads(body)
            link = {
                "id": link_id, "entity": "payment_link", "short_url": f"https://rzp.io/i/{link_id}",
                "amount": payload.get("amount"), "amount_paid": 0, "currency": payload.get("currency"),
                "status": "created", "notes": payload.get("notes", {}), "payments": [],
            }
            state["payment_links"][link_id] = link
            return self._send_json(200, link)

        if self.path.endswith("/cancel") and "/payment_links/" in self.path:
            link_id = self.path.split("/payment_links/")[1].split("/cancel")[0]
            link = state["payment_links"].get(link_id, {"id": link_id})
            link["status"] = "cancelled"
            return self._send_json(200, link)

        if self.path.endswith("/refund") and "/payments/" in self.path:
            return self._send_json(200, state.get("refund_response") or {
                "id": "rfnd_test_1", "entity": "refund", "status": "processed", "amount": 5000, "currency": "INR",
            })

        return self._send_json(404, {"error": {"description": "not found"}})


@pytest.fixture
def razorpay_server(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {"requests": [], "payment_links": {}, "payments": {}, "force_error": None, "refund_response": None}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setattr(rp, "_API_BASE", f"http://127.0.0.1:{port}")
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def provider():
    return RazorpayProvider(
        credentials={"key_id": "rzp_test_fake", "key_secret": "fake_secret"},
        webhook_secret="whsec_test_secret", environment="TEST",
    )


@pytest.mark.asyncio
async def test_test_connection_success(razorpay_server, provider):
    result = await provider.test_connection()
    assert result.success is True


@pytest.mark.asyncio
async def test_test_connection_failure_surfaces_razorpay_error(razorpay_server, provider):
    razorpay_server.state["force_error"] = 401
    result = await provider.test_connection()
    assert result.success is False
    assert "Razorpay API error" in result.detail


@pytest.mark.asyncio
async def test_create_payment_link_uses_paise_and_idempotency_key(razorpay_server, provider):
    invoice = FakeInvoice(id=42, invoice_number="INV-1001", total=50.0)
    result = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/billing")

    assert result.gateway_reference.startswith("plink_test_")
    assert result.payment_url.startswith("https://rzp.io/i/")

    req = next(r for r in razorpay_server.state["requests"] if r["path"] == "/payment_links")
    assert req["headers"]["X-Razorpay-Idempotency-Key"] == "payment-link-42"
    body = json.loads(req["body"])
    assert body["amount"] == 5000  # $50.00 -> 5000 paise
    assert body["notes"]["invoice_id"] == "42"


@pytest.mark.asyncio
async def test_create_payment_session_is_alias_for_create_payment_link(razorpay_server, provider):
    invoice = FakeInvoice(id=7, invoice_number="INV-2001", total=10.0)
    result = await provider.create_payment_session(invoice=invoice, return_url="https://app.example.com/pay")
    assert result.gateway_reference.startswith("plink_test_")


@pytest.mark.asyncio
async def test_verify_payment_reads_latest_entry_from_payments_array(razorpay_server, provider):
    invoice = FakeInvoice(id=1, invoice_number="INV-1", total=25.0)
    link = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/x")

    stored = razorpay_server.state["payment_links"][link.gateway_reference]
    stored["status"] = "paid"
    stored["amount_paid"] = 2500
    stored["payments"] = [{"payment_id": "pay_old", "status": "failed"}, {"payment_id": "pay_new", "status": "captured"}]

    result = await provider.verify_payment(link.gateway_reference)
    assert result.status == "SUCCEEDED"
    assert result.gateway_transaction_id == "pay_new"
    assert result.amount == 25.0


@pytest.mark.asyncio
async def test_get_payment_status_maps_captured_to_succeeded(razorpay_server, provider):
    result = await provider.get_payment_status("pay_test_123")
    assert result.status == "SUCCEEDED"
    assert result.amount == 50.0
    assert result.payment_method == "card"


@pytest.mark.asyncio
async def test_refund_payment_status_mapping_and_idempotency(razorpay_server, provider):
    razorpay_server.state["refund_response"] = {"id": "rfnd_1", "entity": "refund", "status": "pending", "amount": 5000, "currency": "INR"}
    result = await provider.refund_payment("pay_test_abc", amount=50.0)
    assert result.status == "PENDING"

    req = next(r for r in razorpay_server.state["requests"] if r["path"] == "/payments/pay_test_abc/refund")
    assert req["headers"]["X-Razorpay-Idempotency-Key"] == "refund-pay_test_abc-5000"

    razorpay_server.state["refund_response"] = {"id": "rfnd_2", "entity": "refund", "status": "processed", "amount": 5000, "currency": "INR"}
    result2 = await provider.refund_payment("pay_test_abc", amount=50.0)
    assert result2.status == "REFUNDED"


@pytest.mark.asyncio
async def test_refund_payment_missing_status_is_not_treated_as_refunded(razorpay_server, provider):
    """Real bug fix: a response with no recognizable status field used to
    default to REFUNDED (`status in ("processed", None)`), which could mask
    an ambiguous or failed refund as a false success."""
    razorpay_server.state["refund_response"] = {"id": "rfnd_3", "entity": "refund", "amount": 5000, "currency": "INR"}
    result = await provider.refund_payment("pay_test_xyz", amount=50.0)
    assert result.status == "PENDING"


@pytest.mark.asyncio
async def test_cancel_payment(razorpay_server, provider):
    invoice = FakeInvoice(id=2, invoice_number="INV-2", total=10.0)
    link = await provider.create_payment_link(invoice=invoice, return_url="https://app.example.com/x")
    result = await provider.cancel_payment(link.gateway_reference)
    assert result.status == "FAILED"


def test_verify_webhook_signature_valid(provider):
    body = b'{"event": "payment_link.paid"}'
    sig = hmac.new(b"whsec_test_secret", body, hashlib.sha256).hexdigest()
    assert provider.verify_webhook_signature(body, sig) is True


def test_verify_webhook_signature_wrong_secret_fails(provider):
    body = b'{"event": "payment_link.paid"}'
    sig = hmac.new(b"wrong_secret", body, hashlib.sha256).hexdigest()
    assert provider.verify_webhook_signature(body, sig) is False


def test_verify_webhook_signature_missing_header_fails(provider):
    assert provider.verify_webhook_signature(b"{}", None) is False


def test_verify_webhook_signature_no_secret_configured_fails():
    provider = RazorpayProvider(credentials={"key_id": "rzp_test_x", "key_secret": "s"}, webhook_secret=None, environment="TEST")
    assert provider.verify_webhook_signature(b"{}", "deadbeef") is False


def _payment_link_paid_payload(*, link_id="plink_1", payment_id="pay_1", invoice_id="42", amount=5000, created_at=1700000000):
    return json.dumps({
        "event": "payment_link.paid", "created_at": created_at,
        "payload": {
            "payment_link": {"entity": {"id": link_id, "notes": {"invoice_id": invoice_id}}},
            "payment": {"entity": {"id": payment_id, "amount": amount, "currency": "INR"}},
        },
    }).encode()


def test_parse_payment_link_paid_uses_nested_payment_entity_not_link_id(provider):
    """Real bug fix: the old code always read gateway_transaction_id from
    the payment_link entity (id "plink_..."), never the nested payment
    entity (id "pay_...") that get_payment_status()/refund_payment()
    actually require."""
    event = provider.parse_webhook_event(_payment_link_paid_payload())
    assert event.event_type == "PAYMENT_SUCCESS"
    assert event.gateway_transaction_id == "pay_1"
    assert event.amount == 50.0
    assert event.currency == "INR"
    assert event.raw_payload["invoice_id"] == 42
    # synthetic id still keys off the link entity — stable across retries
    assert event.event_id == "payment_link.paid:plink_1:1700000000"


def test_parse_payment_link_paid_falls_back_to_link_id_if_no_nested_payment(provider):
    payload = json.dumps({
        "event": "payment_link.paid", "created_at": 1700000001,
        "payload": {"payment_link": {"entity": {"id": "plink_2", "notes": {}}}},
    }).encode()
    event = provider.parse_webhook_event(payload)
    assert event.gateway_transaction_id == "plink_2"


def test_parse_payment_captured_reads_payment_entity_directly(provider):
    payload = json.dumps({
        "event": "payment.captured", "created_at": 1700000002,
        "payload": {"payment": {"entity": {"id": "pay_2", "amount": 10000, "currency": "INR"}}},
    }).encode()
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_SUCCESS"
    assert event.gateway_transaction_id == "pay_2"
    assert event.amount == 100.0


def test_parse_payment_failed(provider):
    payload = json.dumps({
        "event": "payment.failed", "created_at": 1700000003,
        "payload": {"payment": {"entity": {"id": "pay_3", "amount": 2000, "currency": "INR"}}},
    }).encode()
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_FAILED"


def test_parse_payment_link_expired_is_payment_failed(provider):
    payload = json.dumps({
        "event": "payment_link.expired", "created_at": 1700000004,
        "payload": {"payment_link": {"entity": {"id": "plink_3", "notes": {}}}},
    }).encode()
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "PAYMENT_FAILED"


def test_parse_refund_processed(provider):
    payload = json.dumps({
        "event": "refund.processed", "created_at": 1700000005,
        "payload": {"refund": {"entity": {"id": "rfnd_1", "amount": 1500, "currency": "INR"}}},
    }).encode()
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "REFUND"
    assert event.gateway_transaction_id == "rfnd_1"


def test_parse_unknown_event_type_falls_back_to_uppercased_raw_type(provider):
    payload = json.dumps({"event": "order.paid", "created_at": 1700000006, "payload": {}}).encode()
    event = provider.parse_webhook_event(payload)
    assert event.event_type == "ORDER.PAID"
