"""
Automated tests for TALLY-B: TallyPrimeConnector's real extraction methods
(get_vouchers/get_purchase_vouchers/get_sales_vouchers/get_party_master/
get_ledger/get_payments/get_bill_allocations/get_outstanding) and
tally_extraction_service.py's orchestration (date-range chunking,
INCREMENTAL checkpointing, partial-chunk-failure isolation).

Same disclosed convention as test_tallyprime_connector.py: no licensed
TallyPrime instance exists in this environment, so a real local HTTP
server stands in for Tally's gateway, dispatching by the real request's
<ID> element so each Collection-export shape (vouchers, ledger detail,
party master, bill allocations) gets a realistic, protocol-accurate
response — not a single generic stub.
"""
import re
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.connectors.application.tallyprime import TallyPrimeConnector
from app.connectors.base import ConnectorError
from app.models.connection import Connection, ConnectionCategory, ConnectionType
from app.models.tally_canonical import TallyCanonicalVoucher
from app.models.tally_extraction import TallyExtractionMode, TallyExtractionRecord, TallyExtractionRun, TallyExtractionStatus, TallyExtractionType
from app.services import tally_extraction_service as svc


def _envelope(inner: str) -> str:
    return f"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA>{inner}</DATA></BODY></ENVELOPE>"


def _voucher_xml(vouchers: list[dict]) -> str:
    rows = "".join(
        f"<VOUCHER><DATE>{v['date']}</DATE><VOUCHERNUMBER>{v['number']}</VOUCHERNUMBER>"
        f"<VOUCHERTYPENAME>{v['type']}</VOUCHERTYPENAME><PARTYLEDGERNAME>{v.get('party', '')}</PARTYLEDGERNAME>"
        f"<NARRATION></NARRATION><GUID>{v['number']}-guid</GUID><ALTERID>1</ALTERID>"
        f'<ALLLEDGERENTRIES.LIST><LEDGERNAME>{v.get("party", "")}</LEDGERNAME><AMOUNT>{v.get("amount", "1000.00")}</AMOUNT>'
        f"<ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE></ALLLEDGERENTRIES.LIST></VOUCHER>"
        for v in vouchers
    )
    return _envelope(f'<COLLECTION NAME="Voucher Collection">{rows}</COLLECTION>')


def _party_master_xml(parties: list[dict]) -> str:
    rows = "".join(
        f'<LEDGER><NAME>{p["name"]}</NAME><PARENT>{p["parent"]}</PARENT><GSTIN>{p.get("gstin", "")}</GSTIN>'
        f"<GUID>{p['name']}-guid</GUID><ALTERID>1</ALTERID></LEDGER>"
        for p in parties
    )
    return _envelope(f'<COLLECTION NAME="Party Master">{rows}</COLLECTION>')


def _ledger_detail_xml(name: str, opening: str, closing: str) -> str:
    row = f"<LEDGER><NAME>{name}</NAME><PARENT>Sundry Debtors</PARENT><OPENINGBALANCE>{opening}</OPENINGBALANCE><CLOSINGBALANCE>{closing}</CLOSINGBALANCE><GUID>{name}-guid</GUID><ALTERID>1</ALTERID></LEDGER>"
    return _envelope(f'<COLLECTION NAME="Ledger Detail">{row}</COLLECTION>')


def _bill_allocations_xml(entries: list[dict]) -> str:
    rows = "".join(
        f'<LEDGER><NAME>{e["party"]}</NAME><PARENT>Sundry Debtors</PARENT>'
        f'<BILLALLOCATIONS.LIST><NAME>{e["bill"]}</NAME><BILLDATE>{e["date"]}</BILLDATE>'
        f'<BILLTYPE>New Ref</BILLTYPE><OPENINGBALANCE>{e["closing"]}</OPENINGBALANCE><CLOSINGBALANCE>{e["closing"]}</CLOSINGBALANCE>'
        f"</BILLALLOCATIONS.LIST></LEDGER>"
        for e in entries
    )
    return _envelope(f'<COLLECTION NAME="Bill Allocations">{rows}</COLLECTION>')


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        state["calls"] = state.get("calls", 0) + 1
        state.setdefault("request_bodies", []).append(body)

        if state.get("fail_after_n_calls") is not None and state["calls"] > state["fail_after_n_calls"]:
            self.send_response(200)
            payload = _envelope("<LINEERROR>Simulated failure</LINEERROR>").encode("utf-8")
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        id_match = re.search(r"<ID>(.*?)</ID>", body)
        request_id = id_match.group(1) if id_match else ""

        if request_id == "Voucher Collection":
            payload = _voucher_xml(state.get("vouchers", [])).encode("utf-8")
        elif request_id == "Party Master":
            payload = _party_master_xml(state.get("parties", [])).encode("utf-8")
        elif request_id == "Ledger Detail":
            payload = _ledger_detail_xml(state.get("ledger_name", "Acme"), "0.00", "5000.00 Dr").encode("utf-8")
        elif request_id == "Bill Allocations":
            payload = _bill_allocations_xml(state.get("bill_allocations", [])).encode("utf-8")
        else:
            payload = _envelope("<COLLECTION></COLLECTION>").encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def tally_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _connector(tally_server) -> TallyPrimeConnector:
    port = tally_server.server_address[1]
    return TallyPrimeConnector(config={"host": "127.0.0.1", "port": port}, secrets={})


# ── Connector-level tests ────────────────────────────────────────────

def test_get_vouchers_returns_normalized_rows(tally_server):
    tally_server.state["vouchers"] = [
        {"date": "20240405", "number": "PB/001", "type": "Purchase", "party": "Acme Traders", "amount": "1180.00"},
        {"date": "20240410", "number": "PB/002", "type": "Purchase", "party": "Beta Exports", "amount": "590.00"},
    ]
    connector = _connector(tally_server)
    result = connector.get_vouchers(company="Demo Co", from_date=date(2024, 4, 1), to_date=date(2024, 4, 30))
    assert [v["voucher_number"] for v in result["vouchers"]] == ["PB/001", "PB/002"]
    assert result["vouchers"][0]["ledger_entries"][0]["ledger_name"] == "Acme Traders"


def test_get_purchase_vouchers_defaults_type_to_purchase(tally_server):
    tally_server.state["vouchers"] = [{"date": "20240405", "number": "PB/001", "type": "Purchase"}]
    connector = _connector(tally_server)
    connector.get_purchase_vouchers(company="Demo Co", from_date=date(2024, 4, 1), to_date=date(2024, 4, 30))
    sent = tally_server.state["request_bodies"][-1]
    assert '$VoucherTypeName = "Purchase"' in sent


def test_get_sales_vouchers_defaults_type_to_sales(tally_server):
    tally_server.state["vouchers"] = []
    connector = _connector(tally_server)
    connector.get_sales_vouchers(company="Demo Co", from_date=date(2024, 4, 1), to_date=date(2024, 4, 30))
    sent = tally_server.state["request_bodies"][-1]
    assert '$VoucherTypeName = "Sales"' in sent


def test_get_party_master_infers_party_type_from_parent(tally_server):
    tally_server.state["parties"] = [
        {"name": "Acme Traders", "parent": "Sundry Debtors", "gstin": "27AAAAA0000A1Z5"},
        {"name": "Beta Supplies", "parent": "Sundry Creditors"},
    ]
    connector = _connector(tally_server)
    result = connector.get_party_master(company="Demo Co")
    by_name = {p["name"]: p for p in result["parties"]}
    assert by_name["Acme Traders"]["party_type"] == "CUSTOMER"
    assert by_name["Beta Supplies"]["party_type"] == "SUPPLIER"
    assert by_name["Acme Traders"]["gstin"] == "27AAAAA0000A1Z5"


def test_get_party_master_customer_filter_rejects_unknown_type(tally_server):
    connector = _connector(tally_server)
    from app.connectors.base import ConnectorValidationError
    with pytest.raises(ConnectorValidationError):
        connector.get_party_master(company="Demo Co", party_type="VENDOR")


def test_get_ledger_returns_ledger_and_vouchers(tally_server):
    tally_server.state["ledger_name"] = "Acme Traders"
    tally_server.state["vouchers"] = [{"date": "20240405", "number": "PB/001", "type": "Purchase", "party": "Acme Traders"}]
    connector = _connector(tally_server)
    result = connector.get_ledger(company="Demo Co", party="Acme Traders", from_date=date(2024, 4, 1), to_date=date(2024, 4, 30))
    assert result["ledger"]["name"] == "Acme Traders"
    assert result["ledger"]["closing_balance"] == "5000.00 Dr"
    assert len(result["vouchers"]) == 1


def test_get_payments_tags_direction_both_ways(tally_server):
    tally_server.state["vouchers"] = [{"date": "20240405", "number": "PY/001", "type": "Payment"}]
    connector = _connector(tally_server)
    result = connector.get_payments(company="Demo Co", from_date=date(2024, 4, 1), to_date=date(2024, 4, 30))
    directions = {v["direction"] for v in result["payments"]}
    assert directions == {"PAYMENT", "RECEIPT"}
    assert len(result["payments"]) == 2  # same mock data returned for both Payment and Receipt calls


def test_get_bill_allocations_flattens_nested_bills(tally_server):
    tally_server.state["bill_allocations"] = [
        {"party": "Acme Traders", "bill": "PB/001", "date": "20240405", "closing": "1180.00 Dr"},
    ]
    connector = _connector(tally_server)
    result = connector.get_bill_allocations(company="Demo Co")
    assert result["bill_allocations"][0]["party"] == "Acme Traders"
    assert result["bill_allocations"][0]["bill_name"] == "PB/001"


def test_get_outstanding_filters_zero_balance_bills(tally_server):
    tally_server.state["bill_allocations"] = [
        {"party": "Acme Traders", "bill": "PB/001", "date": "20240405", "closing": "1180.00 Dr"},
        {"party": "Acme Traders", "bill": "PB/002", "date": "20240406", "closing": "0.00"},
    ]
    connector = _connector(tally_server)
    result = connector.get_outstanding(company="Demo Co")
    assert [b["bill_name"] for b in result["outstanding"]] == ["PB/001"]


def test_get_vouchers_line_error_raises_connector_error(tally_server):
    tally_server.state["fail_after_n_calls"] = 0
    connector = _connector(tally_server)
    with pytest.raises(ConnectorError):
        connector.get_vouchers(company="Demo Co", from_date=date(2024, 4, 1), to_date=date(2024, 4, 30))


# ── Service-level orchestration tests ────────────────────────────────

async def _make_connection(db, org, tally_server) -> Connection:
    port = tally_server.server_address[1]
    conn = Connection(
        organization_id=org.id, name="Verify Tally", category=ConnectionCategory.APPLICATION,
        connection_type=ConnectionType.TALLY_PRIME, config={"host": "127.0.0.1", "port": port},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


@pytest.mark.asyncio
async def test_start_extraction_full_mode_persists_records(db, make_org, tally_server):
    org = await make_org()
    conn = await _make_connection(db, org, tally_server)
    tally_server.state["vouchers"] = [{"date": "20240405", "number": "PB/001", "type": "Purchase", "party": "Acme"}]

    run = await svc.start_extraction(
        db, conn, extraction_type=TallyExtractionType.VOUCHERS, company="Demo Co",
        from_date=date(2024, 4, 1), to_date=date(2024, 4, 30), mode=TallyExtractionMode.FULL,
    )

    assert run.status == TallyExtractionStatus.COMPLETED
    assert run.records_fetched == 1
    assert run.checkpoint_date == date(2024, 4, 30)

    from sqlalchemy import select
    records = (await db.execute(select(TallyExtractionRecord).where(TallyExtractionRecord.run_id == run.id))).scalars().all()
    assert len(records) == 1
    assert records[0].external_ref == "PB/001"
    assert records[0].raw_data["party_ledger_name"] == "Acme"


@pytest.mark.asyncio
async def test_start_extraction_chunks_large_date_range(db, make_org, tally_server, monkeypatch):
    org = await make_org()
    conn = await _make_connection(db, org, tally_server)
    tally_server.state["vouchers"] = []
    monkeypatch.setattr(svc, "CHUNK_DAYS", 10)

    run = await svc.start_extraction(
        db, conn, extraction_type=TallyExtractionType.VOUCHERS, company="Demo Co",
        from_date=date(2024, 1, 1), to_date=date(2024, 1, 25), mode=TallyExtractionMode.FULL,
    )

    # 25-day span chunked into 10-day windows -> 3 chunks -> 3 real calls.
    assert run.chunks_total == 3
    assert tally_server.state["calls"] == 3
    assert run.status == TallyExtractionStatus.COMPLETED


@pytest.mark.asyncio
async def test_start_extraction_incremental_advances_checkpoint_and_skips_when_no_new_range(db, make_org, tally_server):
    org = await make_org()
    conn = await _make_connection(db, org, tally_server)
    tally_server.state["vouchers"] = [{"date": "20240110", "number": "PB/001", "type": "Purchase"}]

    first = await svc.start_extraction(
        db, conn, extraction_type=TallyExtractionType.VOUCHERS, company="Demo Co",
        from_date=date(2024, 1, 1), to_date=date(2024, 1, 15), mode=TallyExtractionMode.INCREMENTAL,
    )
    assert first.checkpoint_date == date(2024, 1, 15)
    calls_after_first = tally_server.state["calls"]

    # Same requested window again, but INCREMENTAL — must start after the
    # checkpoint, and since to_date == checkpoint there's nothing new: a
    # real, honest zero-record COMPLETED run with no Tally call at all.
    second = await svc.start_extraction(
        db, conn, extraction_type=TallyExtractionType.VOUCHERS, company="Demo Co",
        from_date=date(2024, 1, 1), to_date=date(2024, 1, 15), mode=TallyExtractionMode.INCREMENTAL,
    )
    assert second.records_fetched == 0
    assert second.status == TallyExtractionStatus.COMPLETED
    assert tally_server.state["calls"] == calls_after_first  # no new HTTP call made


@pytest.mark.asyncio
async def test_start_extraction_partial_chunk_failure_isolates_error(db, make_org, tally_server, monkeypatch):
    org = await make_org()
    conn = await _make_connection(db, org, tally_server)
    tally_server.state["vouchers"] = [{"date": "20240105", "number": "PB/001", "type": "Purchase"}]
    monkeypatch.setattr(svc, "CHUNK_DAYS", 10)
    tally_server.state["fail_after_n_calls"] = 1  # first chunk succeeds, rest fail

    run = await svc.start_extraction(
        db, conn, extraction_type=TallyExtractionType.VOUCHERS, company="Demo Co",
        from_date=date(2024, 1, 1), to_date=date(2024, 1, 25), mode=TallyExtractionMode.FULL,
    )

    assert run.status == TallyExtractionStatus.COMPLETED_WITH_ERRORS
    assert run.chunks_failed == 2
    assert run.records_fetched == 1  # only the successful chunk's record was saved
    assert run.checkpoint_date is None  # must NOT advance past a partial failure


@pytest.mark.asyncio
async def test_start_extraction_ledger_requires_party(db, make_org, tally_server):
    org = await make_org()
    conn = await _make_connection(db, org, tally_server)
    with pytest.raises(svc.TallyExtractionValidationError):
        await svc.start_extraction(db, conn, extraction_type=TallyExtractionType.LEDGER, company="Demo Co")


@pytest.mark.asyncio
async def test_start_extraction_date_ranged_type_requires_dates(db, make_org, tally_server):
    org = await make_org()
    conn = await _make_connection(db, org, tally_server)
    with pytest.raises(svc.TallyExtractionValidationError):
        await svc.start_extraction(db, conn, extraction_type=TallyExtractionType.VOUCHERS, company="Demo Co")


@pytest.mark.asyncio
async def test_start_extraction_party_master_not_date_ranged(db, make_org, tally_server):
    org = await make_org()
    conn = await _make_connection(db, org, tally_server)
    tally_server.state["parties"] = [{"name": "Acme", "parent": "Sundry Debtors"}]

    run = await svc.start_extraction(db, conn, extraction_type=TallyExtractionType.PARTY_MASTER, company="Demo Co")
    assert run.status == TallyExtractionStatus.COMPLETED
    assert run.records_fetched == 1


@pytest.mark.asyncio
async def test_start_extraction_also_materializes_canonical_voucher(db, make_org, tally_server):
    """TALLY-C wiring: a real extraction run must produce a canonical
    row too, not just the raw TallyExtractionRecord snapshot."""
    org = await make_org()
    conn = await _make_connection(db, org, tally_server)
    tally_server.state["vouchers"] = [{"date": "20240405", "number": "PB/001", "type": "Purchase", "party": "Acme", "amount": "500.00"}]

    run = await svc.start_extraction(
        db, conn, extraction_type=TallyExtractionType.VOUCHERS, company="Demo Co",
        from_date=date(2024, 4, 1), to_date=date(2024, 4, 30), mode=TallyExtractionMode.FULL,
    )
    assert run.status == TallyExtractionStatus.COMPLETED

    from sqlalchemy import select
    voucher = (await db.execute(
        select(TallyCanonicalVoucher).where(TallyCanonicalVoucher.connection_id == conn.id, TallyCanonicalVoucher.voucher_number == "PB/001")
    )).scalar_one()
    assert voucher.party_ledger_name == "Acme"
    assert voucher.total_amount == 500.00
