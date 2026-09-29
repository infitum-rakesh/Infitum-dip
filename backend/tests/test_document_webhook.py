"""
Automated tests for app/services/document_webhook_service.py (spec item 7,
session Phase 6 / P6-1..P6-6) — written in Phase 7 to close the gap P6-6
left: that work was verified live via a hand-built mock HTTP receiver and
manual curl driving, never captured as a repeatable pytest suite.

document_webhook_service.py calls httpx directly against a customer-owned
URL — there is no Connection/connector seam to substitute a double at (see
mock_erp_adapter.py's docstring for the contrasting case where one
exists). A real local HTTP server bound to 127.0.0.1 on an OS-assigned
port is therefore the only "real, not mocked" way to test this module,
exactly mirroring the manual mock receiver P6-6's live verification used.

Retry/circuit-breaker timing constants are monkeypatched to
millisecond-scale so the whole suite runs fast — same real code path,
just tuned for test speed rather than production reality (same idea as
loop-until-dry style tests using shrunk sleep values elsewhere).
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.models.connection import Connection  # noqa: F401 - ensures TenantBase metadata includes it before create_all
from app.models.document import Document, DocumentStatus, DocumentType
from app.services import document_webhook_service as svc


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        state = self.server.state

        if self.path == "/oauth/token":
            state["oauth_token_requests"].append(dict(self.headers))
            self._send(200, {"access_token": state.get("oauth_access_token", "test-access-token"), "expires_in": 3600})
            return

        state["requests"].append({
            "method": self.command,
            "idempotency_key": self.headers.get("X-Idempotency-Key"),
            "signature": self.headers.get("X-Signature"),
            "authorization": self.headers.get("Authorization"),
            "x_tenant_id": self.headers.get("X-Tenant-Id"),
            "body": body,
        })
        mode = state["mode"]
        if mode == "success":
            self._send(200, {"ok": True})
        elif mode == "fail_then_succeed":
            key = self.headers.get("X-Idempotency-Key")
            n = state["fail_counts"].get(key, 0)
            if n < state["fail_times"]:
                state["fail_counts"][key] = n + 1
                self._send(500, {"error": "transient"})
            else:
                self._send(200, {"ok": True})
        elif mode == "always_fail":
            self._send(500, {"error": "down"})
        elif mode == "client_error":
            self._send(400, {"error": "bad request"})

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def receiver():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {"mode": "success", "fail_times": 0, "fail_counts": {}, "requests": [], "oauth_token_requests": [], "oauth_access_token": "test-access-token"}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield server, f"http://127.0.0.1:{port}/webhook"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch):
    """Millisecond-scale retry/backoff/cooldown so the suite runs fast,
    and a clean in-process circuit-breaker state per test — the breaker
    dict is a module-level global that would otherwise leak between
    tests (config ids get reused since the test DB is truncated, not
    dropped, between tests — see conftest.py's _clean_tables)."""
    monkeypatch.setattr(svc, "BASE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(svc, "MAX_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(svc, "CIRCUIT_COOLDOWN_SECONDS", 0.3)
    svc._circuit_state.clear()
    yield
    svc._circuit_state.clear()


async def _make_document(db, *, organization_id: int) -> Document:
    doc = Document(
        organization_id=organization_id, filename="invoice.pdf", doc_type=DocumentType.PDF,
        storage_path="uploads/1/invoice.pdf", status=DocumentStatus.COMPLETED, confidence=95.0,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _enable_webhook(db, org_id: int, url: str):
    config = await svc.get_or_create_config(db, org_id)
    config = await svc.update_config(db, config, updated_by=None, enabled=True, url=url, auth_type="NONE")
    return config


async def _wait_until(predicate, *, timeout=5.0, interval=0.02):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(interval)
    return False


@pytest.mark.asyncio
async def test_disabled_webhook_never_makes_a_request(db, make_org, receiver):
    server, url = receiver
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await svc.get_or_create_config(db, org.id)
    assert not config.enabled

    await svc.push_document(db, doc, organization_id=org.id)
    await asyncio.sleep(0.1)

    assert server.state["requests"] == []


@pytest.mark.asyncio
async def test_success_delivers_a_signed_payload(db, make_org, receiver):
    server, url = receiver
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await _enable_webhook(db, org.id, url)
    assert config.encrypted_signing_secret  # auto-generated on first enable

    await svc.push_document(db, doc, organization_id=org.id)
    ok = await _wait_until(lambda: _refreshed_status(db, config))
    assert ok

    assert len(server.state["requests"]) == 1
    req = server.state["requests"][0]
    assert req["idempotency_key"] == f"doc-{doc.id}-webhook-{config.id}"
    assert req["signature"] and req["signature"].startswith("sha256=")

    import hashlib
    import hmac
    from app.core.crypto import decrypt_secret
    signing_secret = decrypt_secret(config.encrypted_signing_secret)
    expected = hmac.new(signing_secret.encode(), req["body"], hashlib.sha256).hexdigest()
    assert req["signature"] == f"sha256={expected}"

    payload = json.loads(req["body"])
    assert payload["document_id"] == doc.id
    assert payload["filename"] == "invoice.pdf"


async def _refreshed_status(db, config):
    await db.refresh(config)
    return config.last_status.value != "NEVER_RUN"


@pytest.mark.asyncio
async def test_retry_then_succeed_backoff(db, make_org, receiver):
    server, url = receiver
    server.state["mode"] = "fail_then_succeed"
    server.state["fail_times"] = 2
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await _enable_webhook(db, org.id, url)

    await svc.push_document(db, doc, organization_id=org.id)
    ok = await _wait_until(lambda: _refreshed_status(db, config))
    assert ok
    assert config.last_status.value == "SUCCESS"
    assert len(server.state["requests"]) == 3  # 2 failures + 1 success

    deliveries, total = await svc.list_deliveries(db, org.id)
    assert total == 3
    statuses = sorted(d.status.value for d in deliveries)
    assert statuses == ["RETRYING", "RETRYING", "SUCCEEDED"]


@pytest.mark.asyncio
async def test_retries_exhausted_lands_dead_letter_and_raises_incident(db, make_org, receiver):
    server, url = receiver
    server.state["mode"] = "always_fail"
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await _enable_webhook(db, org.id, url)

    await svc.push_document(db, doc, organization_id=org.id)
    ok = await _wait_until(lambda: _refreshed_status(db, config), timeout=5.0)
    assert ok
    assert config.last_status.value == "FAILED"
    assert len(server.state["requests"]) == svc.MAX_ATTEMPTS

    deliveries, total = await svc.list_deliveries(db, org.id)
    assert total == svc.MAX_ATTEMPTS
    # SQLite's server_default=func.now() has only 1-second resolution, so
    # deliveries created within the same second can tie on created_at —
    # find the final attempt by attempt_number instead of trusting list
    # order for this assertion.
    final = max(deliveries, key=lambda d: d.attempt_number)
    assert final.status.value == "DEAD_LETTER"

    from sqlalchemy import select
    from app.models.incident import Incident, IncidentSource
    result = await db.execute(select(Incident).where(Incident.source == IncidentSource.DOCUMENT_WEBHOOK))
    incidents = result.scalars().all()
    assert len(incidents) == 1
    assert f"document #{doc.id}" in incidents[0].title


@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_blocks_without_a_network_call(db, make_org, receiver):
    server, url = receiver
    server.state["mode"] = "always_fail"
    org = await make_org()
    doc1 = await _make_document(db, organization_id=org.id)
    config = await _enable_webhook(db, org.id, url)

    await svc.push_document(db, doc1, organization_id=org.id)
    await _wait_until(lambda: _refreshed_status(db, config))
    assert svc.circuit_status(config.id)["state"] == "OPEN"

    request_count_before = len(server.state["requests"])
    doc2 = await _make_document(db, organization_id=org.id)
    await svc.push_document(db, doc2, organization_id=org.id)
    await asyncio.sleep(0.1)

    # No new HTTP request was made — the breaker blocked it before any network call.
    assert len(server.state["requests"]) == request_count_before

    deliveries, _ = await svc.list_deliveries(db, org.id)
    # Same same-second created_at caveat as above — filter by document_id
    # rather than trusting list order.
    doc2_deliveries = [d for d in deliveries if d.document_id == doc2.id]
    assert len(doc2_deliveries) == 1
    latest = doc2_deliveries[0]
    assert latest.status.value == "DEAD_LETTER"
    assert latest.error_code == "CIRCUIT_OPEN"
    assert latest.http_status_code is None


@pytest.mark.asyncio
async def test_circuit_reopens_after_cooldown_and_admits_a_probe(db, make_org, receiver):
    server, url = receiver
    server.state["mode"] = "always_fail"
    org = await make_org()
    doc1 = await _make_document(db, organization_id=org.id)
    config = await _enable_webhook(db, org.id, url)

    await svc.push_document(db, doc1, organization_id=org.id)
    await _wait_until(lambda: _refreshed_status(db, config))
    assert svc.circuit_status(config.id)["state"] == "OPEN"

    await asyncio.sleep(svc.CIRCUIT_COOLDOWN_SECONDS + 0.15)  # let the cooldown elapse
    server.state["mode"] = "success"  # endpoint is "fixed" now
    server.state["requests"].clear()

    doc2 = await _make_document(db, organization_id=org.id)
    await svc.push_document(db, doc2, organization_id=org.id)
    await asyncio.sleep(0.3)

    # The probe reached the network this time (cooldown elapsed -> HALF_OPEN).
    assert len(server.state["requests"]) == 1
    assert svc.circuit_status(config.id)["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_permanent_client_error_fails_immediately_with_no_retry(db, make_org, receiver):
    server, url = receiver
    server.state["mode"] = "client_error"
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await _enable_webhook(db, org.id, url)

    await svc.push_document(db, doc, organization_id=org.id)
    ok = await _wait_until(lambda: _refreshed_status(db, config))
    assert ok
    assert config.last_status.value == "FAILED"
    assert len(server.state["requests"]) == 1  # no retry for a permanent 4xx

    deliveries, total = await svc.list_deliveries(db, org.id)
    assert total == 1
    assert deliveries[0].status.value == "FAILED"
    assert deliveries[0].is_retryable is False

    from sqlalchemy import select
    from app.models.incident import Incident, IncidentSource
    result = await db.execute(select(Incident).where(Incident.source == IncidentSource.DOCUMENT_WEBHOOK))
    assert result.scalars().all() == []  # a permanent failure never raises an incident


@pytest.mark.asyncio
async def test_idempotency_key_is_deterministic_and_stable_across_pushes(db, make_org, receiver):
    server, url = receiver
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await _enable_webhook(db, org.id, url)

    await svc.push_document(db, doc, organization_id=org.id)
    await _wait_until(lambda: _refreshed_status(db, config))
    first_key = server.state["requests"][0]["idempotency_key"]

    await svc.push_document(db, doc, organization_id=org.id)
    await asyncio.sleep(0.2)
    second_key = server.state["requests"][1]["idempotency_key"]

    assert first_key == second_key == f"doc-{doc.id}-webhook-{config.id}"


@pytest.mark.asyncio
async def test_field_gating_respects_include_flags(db, make_org, receiver):
    server, url = receiver
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    doc.ocr_result = {"full_text": "hello"}
    doc.extraction_result = {"fields": {"a": 1}}
    await db.commit()

    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, updated_by=None, enabled=True, url=url, auth_type="NONE",
        include_ocr_text=False, include_extraction_fields=True, include_dataset=False,
    )

    await svc.push_document(db, doc, organization_id=org.id)
    await _wait_until(lambda: _refreshed_status(db, config))

    payload = json.loads(server.state["requests"][0]["body"])
    assert "ocr_text" not in payload
    assert "extraction_fields" in payload
    assert "dataset" not in payload


# ── Part 7 additions: OAuth2 + configurable method/headers/mapping/timeout ──


@pytest.mark.asyncio
async def test_configurable_http_method_put_is_honored(db, make_org, receiver):
    server, url = receiver
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(db, config, updated_by=None, enabled=True, url=url, auth_type="NONE", http_method="PUT")
    assert config.http_method == "PUT"

    await svc.push_document(db, doc, organization_id=org.id)
    ok = await _wait_until(lambda: _refreshed_status(db, config))
    assert ok
    assert server.state["requests"][0]["method"] == "PUT"


@pytest.mark.asyncio
async def test_invalid_http_method_is_rejected(db, make_org):
    org = await make_org()
    config = await svc.get_or_create_config(db, org.id)
    with pytest.raises(svc.DocumentWebhookError, match="HTTP method"):
        await svc.update_config(db, config, updated_by=None, http_method="DELETE")


@pytest.mark.asyncio
async def test_custom_headers_are_sent_and_cannot_override_idempotency_key(db, make_org, receiver):
    server, url = receiver
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, updated_by=None, enabled=True, url=url, auth_type="NONE",
        custom_headers={"X-Tenant-Id": "tenant-42", "X-Idempotency-Key": "should-not-win"},
    )

    await svc.push_document(db, doc, organization_id=org.id)
    ok = await _wait_until(lambda: _refreshed_status(db, config))
    assert ok
    req = server.state["requests"][0]
    assert req["x_tenant_id"] == "tenant-42"
    assert req["idempotency_key"] == f"doc-{doc.id}-webhook-{config.id}"  # real key, not clobbered by the custom header


@pytest.mark.asyncio
async def test_field_mapping_renames_outbound_keys(db, make_org, receiver):
    server, url = receiver
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, updated_by=None, enabled=True, url=url, auth_type="NONE",
        field_mapping={"document_id": "docId", "filename": "fileName"},
    )

    await svc.push_document(db, doc, organization_id=org.id)
    ok = await _wait_until(lambda: _refreshed_status(db, config))
    assert ok
    payload = json.loads(server.state["requests"][0]["body"])
    assert payload["docId"] == doc.id
    assert payload["fileName"] == "invoice.pdf"
    assert "document_id" not in payload
    assert "filename" not in payload
    assert payload["status"] == "COMPLETED"  # unmapped keys pass through unchanged


@pytest.mark.asyncio
async def test_oauth2_fetches_and_reuses_cached_token(db, make_org, receiver):
    server, url = receiver
    token_url = url.replace("/webhook", "/oauth/token")
    org = await make_org()
    doc1 = await _make_document(db, organization_id=org.id)
    doc2 = await _make_document(db, organization_id=org.id)
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, updated_by=None, url=url, auth_type="OAUTH2",
        oauth_token_url=token_url, oauth_client_id="client-1", oauth_client_secret="secret-1",
    )
    config = await svc.update_config(db, config, updated_by=None, enabled=True)

    await svc.push_document(db, doc1, organization_id=org.id)
    ok = await _wait_until(lambda: _refreshed_status(db, config))
    assert ok
    assert server.state["requests"][0]["authorization"] == "Bearer test-access-token"
    assert len(server.state["oauth_token_requests"]) == 1  # fetched once

    await svc.push_document(db, doc2, organization_id=org.id)
    await asyncio.sleep(0.2)
    assert server.state["requests"][1]["authorization"] == "Bearer test-access-token"
    assert len(server.state["oauth_token_requests"]) == 1  # cached token reused, no second fetch


@pytest.mark.asyncio
async def test_enabling_oauth2_without_credentials_is_rejected(db, make_org):
    org = await make_org()
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(db, config, updated_by=None, url="https://example.com/hook", auth_type="OAUTH2")
    with pytest.raises(svc.DocumentWebhookError, match="OAuth2"):
        await svc.update_config(db, config, updated_by=None, enabled=True)


@pytest.mark.asyncio
async def test_configurable_timeout_is_applied(db, make_org, receiver):
    """Not a real slow-server test (that would make the suite slow) — just
    confirms the per-config timeout_seconds value is validated and stored,
    and that update_config() rejects a non-positive timeout."""
    org = await make_org()
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(db, config, updated_by=None, timeout_seconds=45.0)
    assert config.timeout_seconds == 45.0

    with pytest.raises(svc.DocumentWebhookError, match="Timeout"):
        await svc.update_config(db, config, updated_by=None, timeout_seconds=0)
