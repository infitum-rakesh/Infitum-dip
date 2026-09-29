"""
Automated test for app/connectors/api/rest.py — locks in the real bug
found in Phase 11's static review of oracle_erp.py and fixed here in
rest.py too (the same exception-handling gap, flagged as a follow-up at
the time): test_connection() only caught ConnectorValidationError around
_auth_headers(), but the OAuth2 auth path's _oauth2_token() makes a real
network call and can raise ConnectorError.

A real local HTTP server stands in for both the REST API host and the
OAuth2 token endpoint — same convention as test_oracle_erp_connector.py.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.connectors.api.rest import RestApiConnector


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
        if self.path == "/":
            return self._send_json(state.get("base_status", 200), {"ok": True})
        return self._send_json(404, {"detail": "not found"})

    def do_POST(self):
        state = self.server.state
        if self.path == "/oauth2/token":
            if state.get("force_oauth_error"):
                return self._send_json(400, {"error": "invalid_client"})
            return self._send_json(200, {"access_token": "fake_token", "expires_in": 3600})
        return self._send_json(404, {"detail": "not found"})


@pytest.fixture
def rest_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {"base_status": 200, "force_oauth_error": False}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _oauth2_connector(port: int) -> RestApiConnector:
    return RestApiConnector(
        config={
            "base_url": f"http://127.0.0.1:{port}", "auth_type": "OAuth2",
            "token_url": f"http://127.0.0.1:{port}/oauth2/token", "client_id": "cid",
        },
        secrets={"client_secret": "secret"},
    )


def test_test_connection_oauth2_token_failure_returns_graceful_result_not_raise(rest_server):
    """Real bug fix: an OAuth2 token endpoint that rejects the client
    credentials used to raise ConnectorError uncaught out of
    test_connection() — the caller (connection_service.test_connection(),
    which does not wrap the connector call in its own try/except) would
    see an unhandled exception instead of a clean
    ConnectionTestResult(success=False)."""
    port = rest_server.server_address[1]
    rest_server.state["force_oauth_error"] = True
    connector = _oauth2_connector(port)

    result = connector.test_connection()  # must not raise

    assert result.success is False
    assert "OAuth2 token request failed" in result.message


def test_test_connection_oauth2_success(rest_server):
    port = rest_server.server_address[1]
    connector = _oauth2_connector(port)
    result = connector.test_connection()
    assert result.success is True


def test_test_connection_no_auth_success(rest_server):
    port = rest_server.server_address[1]
    connector = RestApiConnector(config={"base_url": f"http://127.0.0.1:{port}"}, secrets={})
    result = connector.test_connection()
    assert result.success is True
