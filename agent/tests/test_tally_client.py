import pytest

from tally_agent.tally_client import LocalTallyClient, TallyClientError

from .mock_tally_server import start_mock_tally_server


@pytest.fixture
def tally_server():
    server = start_mock_tally_server(companies=["Acme Traders Pvt Ltd", "Beta Co"])
    yield server
    server.stop()


@pytest.fixture
def client(tally_server):
    return LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)


def test_list_companies(client):
    companies = client.list_companies()
    assert [c["name"] for c in companies] == ["Acme Traders Pvt Ltd", "Beta Co"]
    assert all(c["guid"] is None and c["is_default"] is False for c in companies)


def test_get_company_found(client):
    company = client.get_company("Beta Co")
    assert company["name"] == "Beta Co"


def test_get_company_not_found(client):
    with pytest.raises(TallyClientError):
        client.get_company("Nonexistent Co")


def test_test_connection_success(client):
    result = client.test_connection()
    assert result["success"] is True
    assert "2" in result["message"] or "companies" in result["message"].lower()
    assert result["latency_ms"] > 0


def test_test_connection_zero_companies(tally_server, client):
    tally_server.state["companies"] = []
    result = client.test_connection()
    assert result["success"] is False


def test_get_vouchers_shape(tally_server):
    tally_server.state["mode"] = "vouchers"
    client = LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)
    result = client.get_vouchers(company="Acme Traders Pvt Ltd", voucher_type="Sales")
    assert "vouchers" in result
    v = result["vouchers"][0]
    assert v["voucher_number"] == "S-500"
    assert v["party_ledger_name"] == "Mock Buyer Co"
    assert v["guid"] == "mock-guid-500"
    assert v["ledger_entries"][0]["ledger_name"] == "Sales Account"
    # Confirm the request XML carried the voucher_type filter
    assert "VoucherTypeFilter" in tally_server.state["last_request_body"]
    assert "Sales" in tally_server.state["last_request_body"]


def test_get_sales_vouchers_defaults_voucher_type(tally_server):
    tally_server.state["mode"] = "vouchers"
    client = LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)
    client.get_sales_vouchers(company="Acme Traders Pvt Ltd")
    assert "Sales" in tally_server.state["last_request_body"]


def test_get_purchase_vouchers_defaults_voucher_type(tally_server):
    tally_server.state["mode"] = "vouchers"
    client = LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)
    client.get_purchase_vouchers(company="Acme Traders Pvt Ltd")
    assert "Purchase" in tally_server.state["last_request_body"]


def test_post_voucher_success(tally_server):
    tally_server.state["mode"] = "import_success"
    client = LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)
    result = client.post_voucher("<ENVELOPE>fake request</ENVELOPE>")
    assert result["created"] == 1
    assert result["last_voucher_id"] == "171"


def test_post_voucher_zero_created_raises(tally_server):
    tally_server.state["mode"] = "line_error"
    client = LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)
    with pytest.raises(TallyClientError):
        client.post_voucher("<ENVELOPE>fake request</ENVELOPE>")


def test_http_error_raises(tally_server):
    tally_server.state["mode"] = "http_error"
    client = LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)
    with pytest.raises(TallyClientError):
        client.list_companies()


def test_line_error_raises(tally_server):
    tally_server.state["mode"] = "line_error"
    client = LocalTallyClient(host="127.0.0.1", port=tally_server.port, timeout=5)
    with pytest.raises(TallyClientError, match="LINEERROR|rejected"):
        client.list_companies()


def test_unreachable_host_raises():
    client = LocalTallyClient(host="127.0.0.1", port=1, timeout=2)
    with pytest.raises(TallyClientError, match="Could not reach"):
        client.list_companies()
