"""
Automated tests for app/connectors/application/tallyprime.py — TALLY-A.

No licensed TallyPrime instance exists in this environment, so a real
local HTTP server stands in for Tally's own XML-over-HTTP gateway (same
disclosed convention as test_oracle_erp_connector.py) — the real httpx
client, XML request construction, and xmltodict response parsing all run
unmodified; only the network destination changes. The mock speaks Tally's
actual documented response envelope shape (ENVELOPE/BODY/DATA/COLLECTION/
COMPANY[], and ENVELOPE/BODY/DATA/LINEERROR for request-level errors) —
the exact bug this test suite caught and fixed in the connector before
these tests were written (the first draft assumed a flatter
ENVELOPE/COLLECTION shape).
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.connectors.application.tallyprime import TallyPrimeConnector
from app.connectors.base import ConnectorError


def _envelope(inner_xml: str) -> str:
    return f"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA>{inner_xml}</DATA></BODY></ENVELOPE>"


def _companies_xml(names: list[str]) -> str:
    companies = "".join(f'<COMPANY NAME="{n}"><NAME>{n}</NAME></COMPANY>' for n in names)
    return _envelope(f'<COLLECTION NAME="List of Companies">{companies}</COLLECTION>')


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        state["last_request_body"] = body.decode("utf-8", errors="replace")
        state["last_auth_header"] = self.headers.get("Authorization")

        mode = state.get("mode", "companies")
        if mode == "http_error":
            self.send_response(500)
            self.end_headers()
            return
        if mode == "empty":
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "not_xml":
            payload = b"this is not xml"
        elif mode == "line_error":
            payload = _envelope("<LINEERROR>Unknown Request, Cannot Accept Data</LINEERROR>").encode("utf-8")
        elif mode == "import_success":
            payload = _envelope("<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS><LASTVCHID>171</LASTVCHID><LASTMID>172</LASTMID><LINEERROR></LINEERROR></RESPONSE>").encode("utf-8")
        elif mode == "import_zero_created":
            payload = _envelope("<RESPONSE><CREATED>0</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS><LINEERROR></LINEERROR></RESPONSE>").encode("utf-8")
        elif mode == "vouchers":
            payload = _envelope(
                '<COLLECTION NAME="Voucher Collection">'
                '<VOUCHER><DATE>20260405</DATE><VOUCHERNUMBER>12</VOUCHERNUMBER><VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>'
                '<PARTYLEDGERNAME>Beta Supplies</PARTYLEDGERNAME><REFERENCE>INV-TALLY-I-1</REFERENCE>'
                '<GUID>mock-guid-1</GUID><ALTERID>99</ALTERID>'
                '<ALLLEDGERENTRIES.LIST><LEDGERNAME>Direct Expenses</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>1180.00</AMOUNT></ALLLEDGERENTRIES.LIST>'
                '</VOUCHER>'
                '</COLLECTION>'
            ).encode("utf-8")
        else:
            payload = _companies_xml(state.get("companies", [])).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def tally_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {"mode": "companies", "companies": ["Acme Traders Pvt Ltd"]}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _connector(tally_server, **config) -> TallyPrimeConnector:
    port = tally_server.server_address[1]
    return TallyPrimeConnector(config={"host": "127.0.0.1", "port": port, **config}, secrets={})


def test_list_companies_single_company_shape(tally_server):
    """xmltodict collapses a single-child COLLECTION/COMPANY into a bare
    dict, not a one-item list — must still come back as a one-item list."""
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd"]
    connector = _connector(tally_server)
    companies = connector.list_companies()
    assert [c["name"] for c in companies] == ["Acme Traders Pvt Ltd"]


def test_list_companies_multiple(tally_server):
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd", "Beta Exports Ltd"]
    connector = _connector(tally_server)
    companies = connector.list_companies()
    assert [c["name"] for c in companies] == ["Acme Traders Pvt Ltd", "Beta Exports Ltd"]


def test_list_companies_marks_configured_company_as_default(tally_server):
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd", "Beta Exports Ltd"]
    connector = _connector(tally_server, company_name="Beta Exports Ltd")
    companies = connector.list_companies()
    defaults = {c["name"]: c["is_default"] for c in companies}
    assert defaults == {"Acme Traders Pvt Ltd": False, "Beta Exports Ltd": True}


def test_get_company_found(tally_server):
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd"]
    connector = _connector(tally_server)
    assert connector.get_company("Acme Traders Pvt Ltd")["name"] == "Acme Traders Pvt Ltd"


def test_get_company_not_found_raises(tally_server):
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd"]
    connector = _connector(tally_server)
    with pytest.raises(ConnectorError):
        connector.get_company("Nonexistent Co")


def test_test_connection_success_no_company_configured(tally_server):
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd"]
    connector = _connector(tally_server)
    result = connector.test_connection()
    assert result.success is True
    assert "1 company available" in result.message
    assert result.server_version is None  # honestly DATA_NOT_AVAILABLE — see class docstring


def test_test_connection_configured_company_matches(tally_server):
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd"]
    connector = _connector(tally_server, company_name="Acme Traders Pvt Ltd")
    result = connector.test_connection()
    assert result.success is True
    assert "targeting 'Acme Traders Pvt Ltd'" in result.message


def test_test_connection_configured_company_not_found_still_succeeds_with_warning(tally_server):
    """Company mismatch is a warning, not a hard failure — the instance is
    genuinely reachable, the configured company is just stale/wrong."""
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd"]
    connector = _connector(tally_server, company_name="Nonexistent Co")
    result = connector.test_connection()
    assert result.success is True
    assert "not among them" in result.message


def test_test_connection_zero_companies_fails(tally_server):
    tally_server.state["companies"] = []
    connector = _connector(tally_server)
    result = connector.test_connection()
    assert result.success is False
    assert "zero companies" in result.message


def test_test_connection_line_error_fails_gracefully(tally_server):
    """Tally returns HTTP 200 even for a rejected request — the real error
    is embedded as BODY/DATA/LINEERROR and must not be treated as success."""
    tally_server.state["mode"] = "line_error"
    connector = _connector(tally_server)
    result = connector.test_connection()  # must not raise
    assert result.success is False
    assert "Unknown Request" in result.message


def test_test_connection_http_error_fails_gracefully(tally_server):
    tally_server.state["mode"] = "http_error"
    connector = _connector(tally_server)
    result = connector.test_connection()
    assert result.success is False


def test_test_connection_empty_response_fails_gracefully(tally_server):
    tally_server.state["mode"] = "empty"
    connector = _connector(tally_server)
    result = connector.test_connection()
    assert result.success is False


def test_test_connection_missing_host_is_validation_error_not_raise(tally_server):
    connector = TallyPrimeConnector(config={}, secrets={})
    result = connector.test_connection()
    assert result.success is False
    assert "Host" in result.message


def test_list_companies_not_xml_raises_connector_error(tally_server):
    tally_server.state["mode"] = "not_xml"
    connector = _connector(tally_server)
    with pytest.raises(ConnectorError):
        connector.list_companies()


def test_auth_token_sent_as_bearer_header(tally_server):
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd"]
    port = tally_server.server_address[1]
    connector = TallyPrimeConnector(config={"host": "127.0.0.1", "port": port}, secrets={"auth_token": "secret-token"})
    connector.list_companies()
    assert tally_server.state["last_auth_header"] == "Bearer secret-token"


def test_list_companies_request_body_is_the_documented_tdl_envelope(tally_server):
    tally_server.state["companies"] = ["Acme Traders Pvt Ltd"]
    connector = _connector(tally_server)
    connector.list_companies()
    sent = tally_server.state["last_request_body"]
    assert "<ID>List of Companies</ID>" in sent
    assert "<TYPE>Collection</TYPE>" in sent


# ── Controlled write-back (TALLY-I) ─────────────────────────────────

def test_post_voucher_success_returns_import_result(tally_server):
    tally_server.state["mode"] = "import_success"
    connector = _connector(tally_server)
    result = connector.post_voucher("<ENVELOPE>mock import xml</ENVELOPE>")
    assert result["created"] == 1
    assert result["last_voucher_id"] == "171"
    assert result["last_master_id"] == "172"


def test_post_voucher_zero_created_raises(tally_server):
    """Tally accepting the envelope but genuinely creating nothing must
    never look like a silent success."""
    tally_server.state["mode"] = "import_zero_created"
    connector = _connector(tally_server)
    with pytest.raises(ConnectorError, match="CREATED=0"):
        connector.post_voucher("<ENVELOPE>mock import xml</ENVELOPE>")


def test_post_voucher_line_error_raises(tally_server):
    tally_server.state["mode"] = "line_error"
    connector = _connector(tally_server)
    with pytest.raises(ConnectorError):
        connector.post_voucher("<ENVELOPE>mock import xml</ENVELOPE>")


def test_get_vouchers_includes_reference_field(tally_server):
    """REFERENCE was added specifically so TALLY-I's post-write
    verification can match a written voucher back by invoice_number."""
    tally_server.state["mode"] = "vouchers"
    connector = _connector(tally_server)
    result = connector.get_vouchers(company="Verify Tally B")
    assert len(result["vouchers"]) == 1
    voucher = result["vouchers"][0]
    assert voucher["reference"] == "INV-TALLY-I-1"
    assert voucher["party_ledger_name"] == "Beta Supplies"
    assert voucher["guid"] == "mock-guid-1"
