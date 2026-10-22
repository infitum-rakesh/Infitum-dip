"""
Automated tests for the Multi-Client TallyPrime Integration Platform
(Agent Relay) — Phase Group 6, TA23. Covers the plan's acceptance
checklist: enrollment lifecycle (single-use token, expiry, lockout),
credential verify/rotate/revoke, the in-memory WS connection registry
(register/supersede/unregister/force_disconnect), the async dispatch
mechanism (success/timeout/offline/mid-call-disconnect), connector-level
validation, and tenant isolation.

No real WebSocket or Windows agent process here — same self-contained,
no-external-process convention as test_india_verification.py and
test_tallyprime_connector.py. The registry/dispatch tests use a small
in-process fake websocket (an object with async send_text()/close(),
duck-typed to what AgentConnectionState actually needs) rather than a
real socket — this exercises the REAL registry/dispatch code
(app/ws/tally_agent_registry.py, app/ws/tally_agent_dispatch.py)
unmodified; only the network transport is faked. The real end-to-end
WS protocol (auth handshake, heartbeat, the cross-thread
run_coroutine_threadsafe bridge) was already live-verified against real
sockets during this feature's build (see TA7/TA10/TA13/TA19) — this
suite exists to catch regressions in the service/registry/dispatch
logic on every future change, not to re-prove the live-verification
findings.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.connectors.application.tallyprime_agent import AgentAwareTallyConnector
from app.connectors.base import ConnectorError, ConnectorValidationError
from app.models.tally_agent import (
    AgentCredential,
    AgentCredentialStatus,
    AgentInstallation,
    AgentInstallationStatus,
)
from app.services import tally_agent_service as svc
from app.services.tally_agent_service import TallyAgentServiceError
from app.ws import tally_agent_dispatch, tally_agent_registry
from app.ws.tally_agent_registry import AgentConnectionState, AgentDisconnectedError


# ── Fake WebSocket (self-contained — no real socket) ─────────────────

class _FakeWebSocket:
    def __init__(self):
        self.sent: list[str] = []
        self.closed_with: tuple[int, str] | None = None

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)


def _make_state(installation_id: int, organization_id: int | None = None) -> tuple[AgentConnectionState, _FakeWebSocket]:
    ws = _FakeWebSocket()
    now = datetime.now(timezone.utc)
    state = AgentConnectionState(
        websocket=ws, agent_installation_id=installation_id, organization_id=organization_id,
        connected_at=now, last_heartbeat_at=now,
    )
    return state, ws


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is module-level global state — reset it around every
    test so one test's connections never leak into the next."""
    tally_agent_registry._connections.clear()
    yield
    tally_agent_registry._connections.clear()


# ── Enrollment lifecycle ──────────────────────────────────────────────

async def test_create_installation_starts_pending_enrollment(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    assert installation.status == AgentInstallationStatus.PENDING_ENROLLMENT
    assert installation.tally_host == "127.0.0.1"
    assert installation.tally_port == 9000


async def test_generate_enrollment_token_rejects_non_pending_installation(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    installation.status = AgentInstallationStatus.ENROLLED
    await db.commit()
    with pytest.raises(TallyAgentServiceError, match="already"):
        await svc.generate_enrollment_token(db, org.id, installation.id, created_by=None)


async def test_exchange_enrollment_token_success_flips_to_enrolled(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    _, token, _ = await svc.generate_enrollment_token(db, org.id, installation.id, created_by=None)

    enrolled, credential_id, credential_secret = await svc.exchange_enrollment_token(
        db, token=token, hostname="TEST-PC", agent_version="0.1.0", os_version="Test OS",
    )
    assert enrolled.id == installation.id
    assert enrolled.status == AgentInstallationStatus.ENROLLED
    assert enrolled.hostname == "TEST-PC"
    assert credential_id and credential_secret

    cred = (await db.execute(select(AgentCredential).where(AgentCredential.credential_id == credential_id))).scalar_one()
    assert cred.status == AgentCredentialStatus.ACTIVE
    assert cred.secret_hash != credential_secret  # never stored in plaintext


async def test_exchange_enrollment_token_rejects_reuse(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    _, token, _ = await svc.generate_enrollment_token(db, org.id, installation.id, created_by=None)
    await svc.exchange_enrollment_token(db, token=token, hostname=None, agent_version=None, os_version=None)

    with pytest.raises(TallyAgentServiceError, match="already been used"):
        await svc.exchange_enrollment_token(db, token=token, hostname=None, agent_version=None, os_version=None)


async def test_exchange_enrollment_token_rejects_expired(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    _, token, _ = await svc.generate_enrollment_token(db, org.id, installation.id, created_by=None)

    from app.models.tally_agent import EnrollmentToken
    row = (await db.execute(select(EnrollmentToken).where(EnrollmentToken.agent_installation_id == installation.id))).scalar_one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()

    with pytest.raises(TallyAgentServiceError, match="expired"):
        await svc.exchange_enrollment_token(db, token=token, hostname=None, agent_version=None, os_version=None)


async def test_exchange_enrollment_token_rejects_unknown_token(db, make_org):
    with pytest.raises(TallyAgentServiceError, match="Invalid"):
        await svc.exchange_enrollment_token(db, token="not-a-real-token", hostname=None, agent_version=None, os_version=None)


async def test_exchange_enrollment_token_locks_after_max_failed_attempts(db, make_org):
    """Repeatedly presenting a VALID, UNEXPIRED token against an
    installation that's no longer eligible (e.g. revoked out from under
    it) increments failed_attempts; past the threshold the token locks
    even though it was never actually consumed."""
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    _, token, _ = await svc.generate_enrollment_token(db, org.id, installation.id, created_by=None)
    installation.status = AgentInstallationStatus.REVOKED
    await db.commit()

    for _ in range(svc.ENROLLMENT_MAX_FAILED_ATTEMPTS):
        with pytest.raises(TallyAgentServiceError, match="no longer eligible"):
            await svc.exchange_enrollment_token(db, token=token, hostname=None, agent_version=None, os_version=None)

    with pytest.raises(TallyAgentServiceError, match="locked"):
        await svc.exchange_enrollment_token(db, token=token, hostname=None, agent_version=None, os_version=None)


# ── Credential verify / rotate / revoke ───────────────────────────────

async def _enroll(db, org) -> tuple[AgentInstallation, str, str]:
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    _, token, _ = await svc.generate_enrollment_token(db, org.id, installation.id, created_by=None)
    enrolled, credential_id, credential_secret = await svc.exchange_enrollment_token(
        db, token=token, hostname=None, agent_version=None, os_version=None,
    )
    return enrolled, credential_id, credential_secret


async def test_verify_agent_credential_success(db, make_org):
    org = await make_org()
    installation, credential_id, credential_secret = await _enroll(db, org)
    result = await svc.verify_agent_credential(db, credential_id=credential_id, credential_secret=credential_secret)
    assert result is not None
    verified_installation, _cred = result
    assert verified_installation.id == installation.id


async def test_verify_agent_credential_wrong_secret_fails(db, make_org):
    org = await make_org()
    _installation, credential_id, _secret = await _enroll(db, org)
    result = await svc.verify_agent_credential(db, credential_id=credential_id, credential_secret="totally-wrong")
    assert result is None


async def test_verify_agent_credential_locks_after_threshold(db, make_org):
    org = await make_org()
    _installation, credential_id, _secret = await _enroll(db, org)
    for _ in range(svc.CREDENTIAL_LOCKOUT_THRESHOLD):
        assert await svc.verify_agent_credential(db, credential_id=credential_id, credential_secret="wrong") is None

    cred = (await db.execute(select(AgentCredential).where(AgentCredential.credential_id == credential_id))).scalar_one()
    assert cred.locked_until is not None
    locked_until = cred.locked_until if cred.locked_until.tzinfo else cred.locked_until.replace(tzinfo=timezone.utc)
    assert locked_until > datetime.now(timezone.utc)


async def test_verify_agent_credential_rejects_revoked_installation(db, make_org):
    org = await make_org()
    installation, credential_id, credential_secret = await _enroll(db, org)
    installation.status = AgentInstallationStatus.REVOKED
    await db.commit()
    result = await svc.verify_agent_credential(db, credential_id=credential_id, credential_secret=credential_secret)
    assert result is None


async def test_rotate_credential_marks_old_rotated_and_issues_new(db, make_org):
    org = await make_org()
    installation, old_credential_id, _old_secret = await _enroll(db, org)
    rotated_installation, new_credential_id, new_secret = await svc.rotate_credential(
        db, org.id, installation.id, actor_id=None,
    )
    assert rotated_installation.id == installation.id
    assert new_credential_id != old_credential_id

    old_cred = (await db.execute(select(AgentCredential).where(AgentCredential.credential_id == old_credential_id))).scalar_one()
    assert old_cred.status == AgentCredentialStatus.ROTATED
    new_cred = (await db.execute(select(AgentCredential).where(AgentCredential.credential_id == new_credential_id))).scalar_one()
    assert new_cred.status == AgentCredentialStatus.ACTIVE

    # The old credential must no longer authenticate.
    assert await svc.verify_agent_credential(db, credential_id=old_credential_id, credential_secret=_old_secret) is None
    # The new one must.
    assert await svc.verify_agent_credential(db, credential_id=new_credential_id, credential_secret=new_secret) is not None


async def test_rotate_credential_rejects_non_enrolled_installation(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")  # still PENDING_ENROLLMENT
    with pytest.raises(TallyAgentServiceError, match="Cannot rotate"):
        await svc.rotate_credential(db, org.id, installation.id, actor_id=None)


async def test_revoke_installation_revokes_all_credentials(db, make_org):
    org = await make_org()
    installation, credential_id, credential_secret = await _enroll(db, org)
    revoked = await svc.revoke_installation(db, org.id, installation.id, actor_id=None, reason="test revoke")
    assert revoked.status == AgentInstallationStatus.REVOKED
    assert revoked.revoked_reason == "test revoke"

    cred = (await db.execute(select(AgentCredential).where(AgentCredential.credential_id == credential_id))).scalar_one()
    assert cred.status == AgentCredentialStatus.REVOKED
    assert await svc.verify_agent_credential(db, credential_id=credential_id, credential_secret=credential_secret) is None


async def test_revoke_installation_rejects_double_revoke(db, make_org):
    org = await make_org()
    installation, _cid, _secret = await _enroll(db, org)
    await svc.revoke_installation(db, org.id, installation.id, actor_id=None)
    with pytest.raises(TallyAgentServiceError, match="already revoked"):
        await svc.revoke_installation(db, org.id, installation.id, actor_id=None)


# ── Tenant isolation ───────────────────────────────────────────────────

async def test_get_installation_rejects_cross_org_access(db, make_org):
    org_a = await make_org()
    org_b = await make_org()
    installation = await svc.create_installation(db, org_a.id, name="Org A's Agent")
    assert await svc.get_installation(db, org_a.id, installation.id) is not None
    assert await svc.get_installation(db, org_b.id, installation.id) is None


async def test_list_installations_scoped_to_organization(db, make_org):
    org_a = await make_org()
    org_b = await make_org()
    await svc.create_installation(db, org_a.id, name="Org A's Agent")
    await svc.create_installation(db, org_b.id, name="Org B's Agent")
    org_a_list = await svc.list_installations(db, org_a.id)
    assert len(org_a_list) == 1
    assert org_a_list[0].name == "Org A's Agent"


# ── WS connection registry (fake websocket, real registry code) ───────

async def test_register_and_is_connected():
    state, _ws = _make_state(installation_id=1)
    await tally_agent_registry.register(1, state)
    assert tally_agent_registry.is_connected(1) is True
    assert tally_agent_registry.get(1) is state
    assert 1 in tally_agent_registry.connected_installation_ids()


async def test_register_supersedes_existing_connection_and_fails_its_pending_requests():
    old_state, old_ws = _make_state(installation_id=1)
    await tally_agent_registry.register(1, old_state)
    pending_fut = asyncio.get_running_loop().create_future()
    old_state.pending_requests["req-1"] = pending_fut

    new_state, _new_ws = _make_state(installation_id=1)
    await tally_agent_registry.register(1, new_state)

    assert tally_agent_registry.get(1) is new_state  # last writer wins
    assert old_ws.closed_with is not None and old_ws.closed_with[0] == 4409
    assert pending_fut.done()
    with pytest.raises(AgentDisconnectedError):
        pending_fut.result()


async def test_unregister_fails_pending_requests_and_removes_entry():
    state, _ws = _make_state(installation_id=1)
    await tally_agent_registry.register(1, state)
    fut = asyncio.get_running_loop().create_future()
    state.pending_requests["req-1"] = fut

    tally_agent_registry.unregister(1)

    assert tally_agent_registry.is_connected(1) is False
    assert fut.done()
    with pytest.raises(AgentDisconnectedError):
        fut.result()


async def test_unregister_unknown_installation_is_a_safe_no_op():
    tally_agent_registry.unregister(999)  # must not raise


async def test_force_disconnect_closes_socket_but_does_not_unregister():
    """force_disconnect() deliberately leaves unregister() to the WS
    endpoint's own try/finally cleanup (see that function's docstring) —
    it must still show as connected in the registry immediately after,
    until that cleanup runs."""
    state, ws = _make_state(installation_id=1)
    await tally_agent_registry.register(1, state)

    disconnected = await tally_agent_registry.force_disconnect(1, code=4403, reason="test")
    assert disconnected is True
    assert ws.closed_with == (4403, "test")
    assert tally_agent_registry.is_connected(1) is True  # still registered — caller's own cleanup unregisters


async def test_force_disconnect_returns_false_when_not_connected():
    assert await tally_agent_registry.force_disconnect(999) is False


def test_get_main_loop_raises_before_set():
    tally_agent_registry._main_loop = None
    with pytest.raises(RuntimeError, match="never captured"):
        tally_agent_registry.get_main_loop()


# ── Dispatch mechanism (submit_command) ────────────────────────────────

async def test_submit_command_success_resolves_with_agent_response(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    state, ws = _make_state(installation.id, organization_id=org.id)
    await tally_agent_registry.register(installation.id, state)

    async def _simulate_agent_response():
        # Wait for the real command frame to be sent, then resolve the
        # future exactly as the WS endpoint's own receive loop would on
        # a real command_result frame (see api/ws_tally_agent.py).
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        import json
        frame = json.loads(ws.sent[0])
        request_id = frame["request_id"]
        fut = state.pending_requests.pop(request_id)
        fut.set_result({"success": True, "data": {"vouchers": [{"voucher_number": "S-1"}]}, "error": None})

    asyncio.create_task(_simulate_agent_response())
    result = await tally_agent_dispatch.submit_command(
        installation.id, "req-1", "get_sales_vouchers", {"company": "Acme"}, timeout_seconds=5,
    )
    assert result == {"vouchers": [{"voucher_number": "S-1"}]}


async def test_submit_command_agent_reports_failure_raises_connector_error(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    state, ws = _make_state(installation.id, organization_id=org.id)
    await tally_agent_registry.register(installation.id, state)

    async def _simulate_agent_failure():
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        import json
        request_id = json.loads(ws.sent[0])["request_id"]
        fut = state.pending_requests.pop(request_id)
        fut.set_result({"success": False, "data": None, "error": "TallyPrime rejected the request."})

    asyncio.create_task(_simulate_agent_failure())
    with pytest.raises(ConnectorError, match="rejected"):
        await tally_agent_dispatch.submit_command(installation.id, "req-1", "list_companies", {}, timeout_seconds=5)


async def test_submit_command_offline_agent_raises_immediately(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    with pytest.raises(ConnectorError, match="not currently connected"):
        await tally_agent_dispatch.submit_command(installation.id, "req-1", "list_companies", {}, timeout_seconds=5)


async def test_submit_command_timeout_raises_connector_error(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    state, _ws = _make_state(installation.id, organization_id=org.id)
    await tally_agent_registry.register(installation.id, state)
    # Nobody ever resolves the future — must time out, not hang forever.
    with pytest.raises(ConnectorError, match="Timed out"):
        await tally_agent_dispatch.submit_command(installation.id, "req-1", "list_companies", {}, timeout_seconds=0.3)


async def test_submit_command_mid_call_disconnect_raises_connector_error(db, make_org):
    org = await make_org()
    installation = await svc.create_installation(db, org.id, name="Test Agent")
    state, ws = _make_state(installation.id, organization_id=org.id)
    await tally_agent_registry.register(installation.id, state)

    async def _disconnect_mid_call():
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        tally_agent_registry.unregister(installation.id)  # simulates the WS endpoint's own cleanup on an unclean drop

    asyncio.create_task(_disconnect_mid_call())
    with pytest.raises(ConnectorError):
        await tally_agent_dispatch.submit_command(installation.id, "req-1", "list_companies", {}, timeout_seconds=5)


async def test_cancel_command_is_best_effort_and_never_raises():
    await tally_agent_dispatch.cancel_command(999, "nonexistent-request")  # no registered agent at all

    state, ws = _make_state(installation_id=1)
    await tally_agent_registry.register(1, state)
    state.pending_requests["req-1"] = asyncio.get_running_loop().create_future()
    await tally_agent_dispatch.cancel_command(1, "req-1")
    assert "req-1" not in state.pending_requests
    assert len(ws.sent) == 1


# ── AgentAwareTallyConnector validation (no network needed) ───────────

def test_agent_connector_requires_installation_id():
    with pytest.raises(ConnectorValidationError, match="no Agent selected"):
        AgentAwareTallyConnector({}, {})


def test_agent_connector_rejects_non_int_installation_id():
    with pytest.raises(ConnectorValidationError, match="Invalid agent_installation_id"):
        AgentAwareTallyConnector({"agent_installation_id": "not-a-number"}, {})


def test_agent_connector_accepts_valid_installation_id():
    connector = AgentAwareTallyConnector({"agent_installation_id": "42"}, {})
    assert connector._installation_id == 42
