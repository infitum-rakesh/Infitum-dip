"""
Automated tests for app/connectors/application/oracle_erp.py — written in
Phase 11 to lock in the real bug found and fixed by Phase 11's static
review of the disclosed-unverified connectors (no live Oracle Fusion ERP
Cloud tenant exists in this environment).

Real local HTTP servers stand in for the Oracle ERP host and the OAuth2
token endpoint (this connector has no Connection/connector seam to
substitute a double at, same convention as the payment-gateway provider
tests) — the real httpx client, request construction, and response
handling all run unmodified; only the network destination changes.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.connectors.application.oracle_erp import OracleErpConnector


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state = self.server.state
        path = self.path.split("?")[0]
        if "/fscmRestApi/resources/" in path and path.endswith("/invoices"):
            if state.get("invoices_status", 200) >= 400:
                return self._send_json(state["invoices_status"], {"detail": "simulated Oracle error"})
            return self._send_json(200, {"items": [], "count": 0, "hasMore": False})
        return self._send_json(404, {"detail": "not found"})

    def do_POST(self):
        state = self.server.state
        if self.path == "/oauth2/token":
            if state.get("force_oauth_error"):
                return self._send_json(400, {"error": "invalid_client"})
            return self._send_json(200, {"access_token": "fake_token", "expires_in": 3600})
        return self._send_json(404, {"detail": "not found"})


@pytest.fixture
def erp_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {"invoices_status": 200, "force_oauth_error": False}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _oauth2_connector(erp_server, port: int) -> OracleErpConnector:
    return OracleErpConnector(
        config={
            "host": f"http://127.0.0.1:{port}", "auth_type": "OAuth2",
            "token_url": f"http://127.0.0.1:{port}/oauth2/token", "client_id": "cid",
        },
        secrets={"client_secret": "secret"},
    )


def test_test_connection_oauth2_token_failure_returns_graceful_result_not_raise(erp_server):
    """Real bug fix: an OAuth2 token endpoint that rejects the client
    credentials used to raise ConnectorError uncaught out of
    test_connection() (only ConnectorValidationError was caught) — the
    caller (connection_service.test_connection(), which does not wrap the
    connector call in its own try/except) would see an unhandled
    exception instead of a clean ConnectionTestResult(success=False)."""
    port = erp_server.server_address[1]
    erp_server.state["force_oauth_error"] = True
    connector = _oauth2_connector(erp_server, port)

    result = connector.test_connection()  # must not raise

    assert result.success is False
    assert "OAuth2 token request failed" in result.message


def test_test_connection_oauth2_success(erp_server):
    port = erp_server.server_address[1]
    connector = _oauth2_connector(erp_server, port)
    result = connector.test_connection()
    assert result.success is True


def test_test_connection_basic_auth_success(erp_server):
    port = erp_server.server_address[1]
    connector = OracleErpConnector(
        config={"host": f"http://127.0.0.1:{port}", "auth_type": "Basic Auth", "username": "u"},
        secrets={"password": "p"},
    )
    result = connector.test_connection()
    assert result.success is True


def test_test_connection_basic_auth_rejected_credentials(erp_server):
    port = erp_server.server_address[1]
    erp_server.state["invoices_status"] = 401
    connector = OracleErpConnector(
        config={"host": f"http://127.0.0.1:{port}", "auth_type": "Basic Auth", "username": "u"},
        secrets={"password": "wrong"},
    )
    result = connector.test_connection()
    assert result.success is False
    assert "rejected these credentials" in result.message


def test_test_connection_missing_basic_auth_credentials_is_validation_error(erp_server):
    port = erp_server.server_address[1]
    connector = OracleErpConnector(config={"host": f"http://127.0.0.1:{port}", "auth_type": "Basic Auth"}, secrets={})
    result = connector.test_connection()
    assert result.success is False
    assert "username/password" in result.message
