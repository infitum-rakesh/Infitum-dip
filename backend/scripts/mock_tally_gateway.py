"""
Standalone mock TallyPrime HTTP gateway — TALLY-I7 live verification only.

No licensed TallyPrime instance exists in this sandbox (same disclosed
limitation as test_tallyprime_connector.py's in-test mock). This script is
that same mock promoted to a long-running process so the REAL
TallyPrimeConnector, the REAL tally_writeback_service retry/circuit-breaker
loop, and the REAL browser-driven frontend can all be exercised against a
REAL local HTTP server speaking Tally's documented XML-over-HTTP shape —
only the actual Tally application is replaced.

Behavior, unlike the test file's per-request `mode` flag, is *stateful*
and reference-driven so post-write verification is a genuine round trip:
  - an Import Data request is parsed for its VOUCHER fields and appended
    to an in-memory ledger, exactly as Tally itself would persist it
  - a subsequent Voucher Collection export returns that same voucher back,
    with a real-looking GUID/ALTERID/VOUCHERNUMBER — nothing is faked at
    the read side once a write has actually happened
  - GET /control/reject-next-n?n=2 makes the NEXT n Import requests fail
    with a LINEERROR, to demonstrate the retry loop against a real HTTP
    round trip (not just the unit-test mock) before it starts succeeding

Demo seed data (client walkthrough): STATE starts pre-populated with a
realistic ~36-voucher Indian SME company ("Rakesh Test Co" by default,
override with --company) spanning Purchase/Sales/Payment/Receipt/Journal
vouchers with real GST (CGST/SGST/IGST) and TDS math, plus Party Master
ledgers (Sundry Creditors/Debtors with GSTIN/state/opening/closing
balances) — deliberately including a handful of real discrepancies
(GST mismatch, a voucher never posted in Tally, a cash purchase never
invoiced, a duplicate posting, a stale ledger balance, a vendor never
paid) so the REAL tally_reconciliation_service surfaces every exception
type it supports against this data, not a hand-picked happy path.

Party Master (TALLY-B's get_party_master) was never implemented by this
mock before — only vouchers were — so ledger-balance reconciliation and
GSTIN-aware party resolution had no data to extract. Added here as a
straightforward completion of the same simulated gateway, not new scope.

Run: python3 scripts/mock_tally_gateway.py [--port 9501] [--company "..."]
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape as _xesc

import xmltodict


def _le(name: str, amount: float, positive: bool) -> dict:
    return {"LEDGERNAME": name, "ISDEEMEDPOSITIVE": "Yes" if positive else "No", "AMOUNT": f"{amount:.2f}"}


def _voucher(date: str, vch_no: str, vtype: str, party: str, narration: str, reference: str,
             guid: str, alter_id: str, entries: list[dict]) -> dict:
    return {
        "DATE": date, "VOUCHERNUMBER": vch_no, "VOUCHERTYPENAME": vtype, "PARTYLEDGERNAME": party,
        "NARRATION": narration, "REFERENCE": reference, "GUID": guid, "ALTERID": alter_id,
        "ledger_entries": entries,
    }


def _purchase_entries(base: float, cgst: float = 0, sgst: float = 0, igst: float = 0,
                       tds: float = 0, party: str = "", ledger: str = "Purchase Accounts") -> list[dict]:
    """Standard Tally double-entry for a purchase: expense/GST legs on the
    positive (debit) side, party + TDS Payable (a liability credit) on
    the negative (credit) side — the positive-side sum is the voucher's
    gross total_amount (see TallyCanonicalVoucher.total_amount docstring),
    matching how ApInvoice.total is also the gross (pre-TDS) figure."""
    entries = [_le(ledger, base, True)]
    if cgst:
        entries.append(_le("CGST", cgst, True))
    if sgst:
        entries.append(_le("SGST", sgst, True))
    if igst:
        entries.append(_le("IGST", igst, True))
    gross = base + cgst + sgst + igst
    net_payable = gross - tds
    entries.append(_le(party, net_payable, False))
    if tds:
        entries.append(_le("TDS Payable", tds, False))
    return entries


def _sales_entries(base: float, cgst: float = 0, sgst: float = 0, igst: float = 0, party: str = "") -> list[dict]:
    gross = base + cgst + sgst + igst
    entries = [_le(party, gross, True)]
    entries.append(_le("Sales Accounts", base, False))
    if cgst:
        entries.append(_le("CGST", cgst, False))
    if sgst:
        entries.append(_le("SGST", sgst, False))
    if igst:
        entries.append(_le("IGST", igst, False))
    return entries


def _money_entries(amount: float, party: str, bank_ledger: str, is_payment: bool) -> list[dict]:
    """Payment: bank/cash credited, party debited (clearing their payable).
    Receipt: bank/cash debited, party credited (clearing their receivable).
    Only the sign differs; both are a simple two-leg voucher."""
    if is_payment:
        return [_le(party, amount, True), _le(bank_ledger, amount, False)]
    return [_le(bank_ledger, amount, True), _le(party, amount, False)]


def _default_vouchers() -> list[dict]:
    v = []

    # ── Purchase vouchers (Shree Ganesh Traders — intrastate, CGST+SGST) ──
    v.append(_voucher("20260908", "P-1001", "Purchase", "Shree Ganesh Traders",
                       "Purchase of raw materials", "INV-SGT-1001", "tally-sgt-1001", "1001",
                       _purchase_entries(50000, cgst=4500, sgst=4500, party="Shree Ganesh Traders")))
    v.append(_voucher("20260901", "P-1002", "Purchase", "Shree Ganesh Traders",
                       "Purchase of packaging material", "INV-SGT-1002", "tally-sgt-1002", "1002",
                       _purchase_entries(30000, cgst=2700, sgst=2700, party="Shree Ganesh Traders")))
    # MISMATCH: Tally has the correct CGST (1800); the matching ApInvoice
    # will be seeded with a wrong AI-extracted CGST of 1300.
    v.append(_voucher("20260911", "P-1003", "Purchase", "Shree Ganesh Traders",
                       "Purchase of hardware fittings", "INV-SGT-1003", "tally-sgt-1003", "1003",
                       _purchase_entries(20000, cgst=1800, sgst=1800, party="Shree Ganesh Traders")))
    # NOTE: no P-1004 in Tally at all — the matching ApInvoice (INV-SGT-1004)
    # is a real MISSING-voucher scenario (invoice received, not yet booked).

    # ── Purchase vouchers (Patel Electricals — interstate, IGST) ──
    v.append(_voucher("20260906", "P-1010", "Purchase", "Patel Electricals Pvt Ltd",
                       "Purchase of electrical panels", "INV-PE-2001", "tally-pe-2001", "1010",
                       _purchase_entries(80000, igst=14400, party="Patel Electricals Pvt Ltd")))
    # MISMATCH: Tally IGST correct (7200); ApInvoice will carry wrong IGST (6400).
    v.append(_voucher("20260910", "P-1011", "Purchase", "Patel Electricals Pvt Ltd",
                       "Purchase of cabling", "INV-PE-2002", "tally-pe-2002", "1011",
                       _purchase_entries(40000, igst=7200, party="Patel Electricals Pvt Ltd")))

    # ── Purchase voucher (Om Steel Industries — old, no Payment ever posted) ──
    v.append(_voucher("20260907", "P-1020", "Purchase", "Om Steel Industries",
                       "Purchase of MS angles and sheets", "INV-OS-3001", "tally-os-3001", "1020",
                       _purchase_entries(60000, cgst=5400, sgst=5400, party="Om Steel Industries")))

    # ── Purchase voucher (Krishna Packaging — also used for ledger-balance mismatch) ──
    v.append(_voucher("20261004", "P-1030", "Purchase", "Krishna Packaging Co",
                       "Purchase of corrugated boxes", "INV-KP-4001", "tally-kp-4001", "1030",
                       _purchase_entries(25000, cgst=2250, sgst=2250, party="Krishna Packaging Co")))
    # DUPLICATE: same vendor + same gross total posted twice in Tally by mistake.
    v.append(_voucher("20261009", "P-1031A", "Purchase", "Krishna Packaging Co",
                       "Purchase of carton sheets", "INV-KP-4002", "tally-kp-4002a", "1031",
                       _purchase_entries(15000, cgst=1350, sgst=1350, party="Krishna Packaging Co")))
    v.append(_voucher("20261009", "P-1031B", "Purchase", "Krishna Packaging Co",
                       "Purchase of carton sheets (dup entry)", "INV-KP-4002", "tally-kp-4002b", "1032",
                       _purchase_entries(15000, cgst=1350, sgst=1350, party="Krishna Packaging Co")))

    # ── Purchase vouchers (Bharat Office Supplies) ──
    v.append(_voucher("20261007", "P-1040", "Purchase", "Bharat Office Supplies",
                       "Purchase of stationery and consumables", "INV-BOS-5001", "tally-bos-5001", "1040",
                       _purchase_entries(12000, cgst=1080, sgst=1080, party="Bharat Office Supplies")))
    # EXTRA: a real Tally voucher (cash purchase) with no matching ApInvoice.
    v.append(_voucher("20261012", "P-1041", "Purchase", "Bharat Office Supplies",
                       "Cash purchase of printer cartridges", "CASH-BOS-5002", "tally-bos-5002", "1041",
                       _purchase_entries(8000, cgst=720, sgst=720, party="Bharat Office Supplies")))

    # ── Purchase voucher with TDS (Sai Logistics Services, 194C @ 2%) ──
    v.append(_voucher("20261005", "P-1050", "Purchase", "Sai Logistics Services",
                       "Freight and logistics services for September", "INV-SLS-6001", "tally-sls-6001", "1050",
                       _purchase_entries(20000, cgst=1800, sgst=1800, tds=400, party="Sai Logistics Services",
                                          ledger="Freight & Forwarding")))

    # ── Sales vouchers (Nexus Retail Pvt Ltd — intrastate) ──
    v.append(_voucher("20261002", "S-2001", "Sales", "Nexus Retail Pvt Ltd",
                       "Sale of finished goods - batch 1", "SO-NRP-001", "tally-nrp-s1", "2001",
                       _sales_entries(70000, cgst=6300, sgst=6300, party="Nexus Retail Pvt Ltd")))
    v.append(_voucher("20261008", "S-2002", "Sales", "Nexus Retail Pvt Ltd",
                       "Sale of finished goods - batch 2", "SO-NRP-002", "tally-nrp-s2", "2002",
                       _sales_entries(45000, cgst=4050, sgst=4050, party="Nexus Retail Pvt Ltd")))
    v.append(_voucher("20261013", "S-2003", "Sales", "Nexus Retail Pvt Ltd",
                       "Sale of finished goods - batch 3", "SO-NRP-003", "tally-nrp-s3", "2003",
                       _sales_entries(32000, cgst=2880, sgst=2880, party="Nexus Retail Pvt Ltd")))

    # ── Sales vouchers (Metro Mart Enterprises — interstate, Karnataka) ──
    v.append(_voucher("20261001", "S-2010", "Sales", "Metro Mart Enterprises",
                       "Sale of finished goods - September dispatch", "SO-MME-001", "tally-mme-s1", "2010",
                       _sales_entries(90000, igst=16200, party="Metro Mart Enterprises")))
    v.append(_voucher("20261010", "S-2011", "Sales", "Metro Mart Enterprises",
                       "Sale of finished goods - October dispatch", "SO-MME-002", "tally-mme-s2", "2011",
                       _sales_entries(55000, igst=9900, party="Metro Mart Enterprises")))

    # ── Sales vouchers (Urban Bazaar Stores — intrastate) ──
    v.append(_voucher("20261003", "S-2020", "Sales", "Urban Bazaar Stores",
                       "Sale of finished goods", "SO-UBS-001", "tally-ubs-s1", "2020",
                       _sales_entries(28000, cgst=2520, sgst=2520, party="Urban Bazaar Stores")))
    v.append(_voucher("20261014", "S-2021", "Sales", "Urban Bazaar Stores",
                       "Sale of finished goods - repeat order", "SO-UBS-002", "tally-ubs-s2", "2021",
                       _sales_entries(19000, cgst=1710, sgst=1710, party="Urban Bazaar Stores")))

    # ── Payment vouchers (vendor payments — deliberately none for Om Steel Industries) ──
    v.append(_voucher("20260915", "PMT-3001", "Payment", "Shree Ganesh Traders",
                       "Payment against INV-SGT-1001", "PMT-SGT-1001", "tally-pmt-sgt1", "3001",
                       _money_entries(59000, "Shree Ganesh Traders", "HDFC Bank Current A/c", True)))
    v.append(_voucher("20260905", "PMT-3002", "Payment", "Shree Ganesh Traders",
                       "Payment against INV-SGT-1002", "PMT-SGT-1002", "tally-pmt-sgt2", "3002",
                       _money_entries(35400, "Shree Ganesh Traders", "HDFC Bank Current A/c", True)))
    v.append(_voucher("20260912", "PMT-3010", "Payment", "Patel Electricals Pvt Ltd",
                       "Payment against INV-PE-2001", "PMT-PE-2001", "tally-pmt-pe1", "3010",
                       _money_entries(94400, "Patel Electricals Pvt Ltd", "HDFC Bank Current A/c", True)))
    v.append(_voucher("20261006", "PMT-3030", "Payment", "Krishna Packaging Co",
                       "Part payment against INV-KP-4001", "PMT-KP-4001", "tally-pmt-kp1", "3030",
                       _money_entries(20000, "Krishna Packaging Co", "HDFC Bank Current A/c", True)))
    v.append(_voucher("20261009", "PMT-3040", "Payment", "Bharat Office Supplies",
                       "Payment against INV-BOS-5001", "PMT-BOS-5001", "tally-pmt-bos1", "3040",
                       _money_entries(14160, "Bharat Office Supplies", "Cash", True)))
    v.append(_voucher("20261006", "PMT-3050", "Payment", "Sai Logistics Services",
                       "Payment (net of TDS) against INV-SLS-6001", "PMT-SLS-6001", "tally-pmt-sls1", "3050",
                       _money_entries(23200, "Sai Logistics Services", "HDFC Bank Current A/c", True)))

    # ── Receipt vouchers (customer receipts) ──
    v.append(_voucher("20261006", "RCT-4001", "Receipt", "Nexus Retail Pvt Ltd",
                       "Receipt against SO-NRP-001", "RCT-NRP-001", "tally-rct-nrp1", "4001",
                       _money_entries(82600, "Nexus Retail Pvt Ltd", "HDFC Bank Current A/c", False)))
    v.append(_voucher("20261011", "RCT-4002", "Receipt", "Nexus Retail Pvt Ltd",
                       "Receipt against SO-NRP-002", "RCT-NRP-002", "tally-rct-nrp2", "4002",
                       _money_entries(53100, "Nexus Retail Pvt Ltd", "HDFC Bank Current A/c", False)))
    v.append(_voucher("20261005", "RCT-4010", "Receipt", "Metro Mart Enterprises",
                       "Receipt against SO-MME-001", "RCT-MME-001", "tally-rct-mme1", "4010",
                       _money_entries(106200, "Metro Mart Enterprises", "HDFC Bank Current A/c", False)))
    v.append(_voucher("20261015", "RCT-4011", "Receipt", "Metro Mart Enterprises",
                       "Receipt against SO-MME-002", "RCT-MME-002", "tally-rct-mme2", "4011",
                       _money_entries(64900, "Metro Mart Enterprises", "HDFC Bank Current A/c", False)))
    v.append(_voucher("20261007", "RCT-4020", "Receipt", "Urban Bazaar Stores",
                       "Receipt against SO-UBS-001", "RCT-UBS-001", "tally-rct-ubs1", "4020",
                       _money_entries(33040, "Urban Bazaar Stores", "Cash", False)))

    # ── Journal vouchers ──
    v.append(_voucher("20260930", "JV-5001", "Journal", "",
                       "Depreciation on plant and machinery for the month", "JV-DEP-SEP", "tally-jv-dep", "5001",
                       [_le("Depreciation", 7500, True), _le("Plant and Machinery", 7500, False)]))
    v.append(_voucher("20261014", "JV-5002", "Journal", "",
                       "Provision for electricity expense", "JV-PROV-OCT", "tally-jv-prov", "5002",
                       [_le("Office Expenses", 4200, True), _le("Outstanding Expenses", 4200, False)]))

    return v


def _default_parties() -> list[dict]:
    def p(name, parent, gstin, state, opening, closing):
        return {
            "NAME": name, "PARENT": parent, "GSTIN": gstin, "LEDSTATENAME": state,
            "OPENINGBALANCE": f"{opening:.2f} Dr" if parent == "Sundry Creditors" else f"{opening:.2f} Cr",
            "CLOSINGBALANCE": f"{closing:.2f} Dr" if parent == "Sundry Creditors" else f"{closing:.2f} Cr",
            "EMAIL": f"accounts@{name.lower().split()[0]}.example.com",
            "LEDGERMOBILE": "9800000000",
            "GUID": f"tally-party-{name.lower().replace(' ', '-')}", "ALTERID": "9001",
        }

    return [
        # closing_balance set equal to this vendor's total seeded ApInvoice
        # amount (seed_tally_demo.py) so LEDGER_BALANCE_MISMATCH fires ONLY
        # for the one vendor deliberately designed to trigger it below.
        p("Shree Ganesh Traders", "Sundry Creditors", "27AAACS1234F1Z5", "Maharashtra", 0, 159300.0),
        p("Om Steel Industries", "Sundry Creditors", "27AAACO5678G1Z2", "Maharashtra", 0, 70800.0),
        p("Patel Electricals Pvt Ltd", "Sundry Creditors", "24AAACP4321H1Z8", "Gujarat", 0, 141600.0),
        # LEDGER_BALANCE_MISMATCH: real invoiced total for this vendor is
        # 29500 (INV-KP-4001) + 17700 (INV-KP-4002) = 47200, but Tally's own
        # closing balance disagrees by well over the ~2.0 tolerance — a
        # realistic "the books haven't been reconciled in a while" scenario.
        p("Krishna Packaging Co", "Sundry Creditors", "27AAACK8765J1Z1", "Maharashtra", 0, 56000.0),
        p("Sai Logistics Services", "Sundry Creditors", "27AAACS2468L1Z4", "Maharashtra", 0, 23200.0),
        p("Bharat Office Supplies", "Sundry Creditors", "27AAACB1357M1Z9", "Maharashtra", 0, 14160.0),
        p("Nexus Retail Pvt Ltd", "Sundry Debtors", "27AAACN1122P1Z3", "Maharashtra", 0, 0.0),
        p("Metro Mart Enterprises", "Sundry Debtors", "29AAACM3344Q1Z6", "Karnataka", 0, 0.0),
        p("Urban Bazaar Stores", "Sundry Debtors", "27AAACU5566R1Z9", "Maharashtra", 0, 0.0),
    ]


STATE = {
    "companies": ["Rakesh Test Co"],
    "vouchers": _default_vouchers(),
    "parties": _default_parties(),
    "last_vch_id": 9000,
    "last_master_id": 9900,
    "reject_next_n": 0,
}
LOCK = threading.Lock()


def _envelope(inner_xml: str) -> str:
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER>"
        f"<BODY><DATA>{inner_xml}</DATA></BODY></ENVELOPE>"
    )


def _companies_xml() -> str:
    companies = "".join(
        f'<COMPANY NAME="{n}"><NAME>{n}</NAME></COMPANY>' for n in STATE["companies"]
    )
    return _envelope(f'<COLLECTION NAME="List of Companies">{companies}</COLLECTION>')


def _voucher_xml(v: dict) -> str:
    entries = "".join(
        "<ALLLEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{_xesc(le['LEDGERNAME'])}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{_xesc(le['ISDEEMEDPOSITIVE'])}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{_xesc(le['AMOUNT'])}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
        for le in v["ledger_entries"]
    )
    return (
        "<VOUCHER>"
        f"<DATE>{_xesc(v['DATE'])}</DATE>"
        f"<VOUCHERNUMBER>{_xesc(v['VOUCHERNUMBER'])}</VOUCHERNUMBER>"
        f"<VOUCHERTYPENAME>{_xesc(v['VOUCHERTYPENAME'])}</VOUCHERTYPENAME>"
        f"<PARTYLEDGERNAME>{_xesc(v['PARTYLEDGERNAME'])}</PARTYLEDGERNAME>"
        f"<NARRATION>{_xesc(v['NARRATION'])}</NARRATION>"
        f"<REFERENCE>{_xesc(v['REFERENCE'])}</REFERENCE>"
        f"<GUID>{_xesc(v['GUID'])}</GUID>"
        f"<ALTERID>{_xesc(v['ALTERID'])}</ALTERID>"
        f"{entries}"
        "</VOUCHER>"
    )


def _vouchers_xml() -> str:
    items = "".join(_voucher_xml(v) for v in STATE["vouchers"])
    return _envelope(f'<COLLECTION NAME="Voucher Collection">{items}</COLLECTION>')


def _party_xml(p: dict) -> str:
    return (
        "<LEDGER>"
        f"<NAME>{_xesc(p['NAME'])}</NAME>"
        f"<PARENT>{_xesc(p['PARENT'])}</PARENT>"
        f"<GSTIN>{_xesc(p['GSTIN'])}</GSTIN>"
        f"<LEDSTATENAME>{_xesc(p['LEDSTATENAME'])}</LEDSTATENAME>"
        f"<OPENINGBALANCE>{_xesc(str(p['OPENINGBALANCE']))}</OPENINGBALANCE>"
        f"<CLOSINGBALANCE>{_xesc(str(p['CLOSINGBALANCE']))}</CLOSINGBALANCE>"
        f"<EMAIL>{_xesc(p['EMAIL'])}</EMAIL>"
        f"<LEDGERMOBILE>{_xesc(p['LEDGERMOBILE'])}</LEDGERMOBILE>"
        f"<GUID>{_xesc(p['GUID'])}</GUID>"
        f"<ALTERID>{_xesc(str(p['ALTERID']))}</ALTERID>"
        "</LEDGER>"
    )


def _parties_xml() -> str:
    items = "".join(_party_xml(p) for p in STATE["parties"])
    return _envelope(f'<COLLECTION NAME="Party Master">{items}</COLLECTION>')


def _line_error_xml(message: str) -> str:
    return _envelope(f"<LINEERROR>{message}</LINEERROR>")


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _handle_import(parsed: dict) -> str:
    with LOCK:
        if STATE["reject_next_n"] > 0:
            STATE["reject_next_n"] -= 1
            return _line_error_xml("Mock gateway: simulated rejection (reject-next-n)")

        try:
            msg = parsed["ENVELOPE"]["BODY"]["IMPORTDATA"]["REQUESTDATA"]["TALLYMESSAGE"]
            voucher = msg["VOUCHER"]
        except (KeyError, TypeError):
            return _line_error_xml("Mock gateway: could not locate VOUCHER in Import Data request")

        entries = []
        for le in _as_list(voucher.get("ALLLEDGERENTRIES.LIST")):
            entries.append({
                "LEDGERNAME": le.get("LEDGERNAME", ""),
                "ISDEEMEDPOSITIVE": le.get("ISDEEMEDPOSITIVE", "No"),
                "AMOUNT": le.get("AMOUNT", "0.00"),
            })

        STATE["last_vch_id"] += 1
        STATE["last_master_id"] += 1
        vch_id, master_id = STATE["last_vch_id"], STATE["last_master_id"]
        new_voucher = {
            "DATE": voucher.get("DATE", ""),
            "VOUCHERNUMBER": str(vch_id),
            "VOUCHERTYPENAME": voucher.get("VOUCHERTYPENAME", ""),
            "PARTYLEDGERNAME": voucher.get("PARTYLEDGERNAME", ""),
            "NARRATION": voucher.get("NARRATION", ""),
            "REFERENCE": voucher.get("REFERENCE", ""),
            "GUID": f"mock-guid-{master_id}",
            "ALTERID": str(master_id),
            "ledger_entries": entries,
        }
        STATE["vouchers"].append(new_voucher)

        return _envelope(
            "<RESPONSE>"
            "<CREATED>1</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS>"
            f"<LASTVCHID>{vch_id}</LASTVCHID><LASTMID>{master_id}</LASTMID>"
            "<LINEERROR></LINEERROR>"
            "</RESPONSE>"
        )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[mock-tally]", fmt % args)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/control/reject-next-n":
            n = int(parse_qs(parsed.query).get("n", ["0"])[0])
            with LOCK:
                STATE["reject_next_n"] = n
            self._send(200, json.dumps({"reject_next_n": n}).encode(), "application/json")
            return
        if parsed.path == "/control/state":
            with LOCK:
                body = json.dumps({
                    "companies": STATE["companies"],
                    "voucher_count": len(STATE["vouchers"]),
                    "party_count": len(STATE["parties"]),
                    "reject_next_n": STATE["reject_next_n"],
                }).encode()
            self._send(200, body, "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            parsed = xmltodict.parse(body)
        except Exception as exc:  # noqa: BLE001
            self._send(400, f"bad xml: {exc}".encode(), "text/plain")
            return

        envelope = parsed.get("ENVELOPE", {})
        header = envelope.get("HEADER", {})
        request_type = header.get("TALLYREQUEST", "")
        req_id = header.get("ID", "")

        if request_type == "Import Data":
            payload = _handle_import(parsed).encode("utf-8")
        elif req_id == "List of Companies":
            payload = _companies_xml().encode("utf-8")
        elif req_id == "Voucher Collection":
            payload = _vouchers_xml().encode("utf-8")
        elif req_id == "Party Master":
            payload = _parties_xml().encode("utf-8")
        else:
            payload = _envelope("<COLLECTION></COLLECTION>").encode("utf-8")

        self._send(200, payload, "text/xml; charset=utf-8")

    def _send(self, code: int, payload: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9501)
    parser.add_argument("--company", default="Rakesh Test Co")
    args = parser.parse_args()
    STATE["companies"] = [args.company]

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[mock-tally] listening on 127.0.0.1:{args.port}, company={args.company!r}")
    print(f"[mock-tally] seeded {len(STATE['vouchers'])} vouchers, {len(STATE['parties'])} parties")
    print("[mock-tally] control: GET /control/state, GET /control/reject-next-n?n=N")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
