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

Run: python3 scripts/mock_tally_gateway.py [--port 9501]
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import xmltodict

STATE = {
    "companies": ["Verify Tally B"],
    "vouchers": [],  # list of dicts already in Tally's own VOUCHER shape
    "last_vch_id": 100,
    "last_master_id": 200,
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
        f"<LEDGERNAME>{le['LEDGERNAME']}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{le['ISDEEMEDPOSITIVE']}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{le['AMOUNT']}</AMOUNT>"
        "</ALLLEDGERENTRIES.LIST>"
        for le in v["ledger_entries"]
    )
    return (
        "<VOUCHER>"
        f"<DATE>{v['DATE']}</DATE>"
        f"<VOUCHERNUMBER>{v['VOUCHERNUMBER']}</VOUCHERNUMBER>"
        f"<VOUCHERTYPENAME>{v['VOUCHERTYPENAME']}</VOUCHERTYPENAME>"
        f"<PARTYLEDGERNAME>{v['PARTYLEDGERNAME']}</PARTYLEDGERNAME>"
        f"<NARRATION>{v['NARRATION']}</NARRATION>"
        f"<REFERENCE>{v['REFERENCE']}</REFERENCE>"
        f"<GUID>{v['GUID']}</GUID>"
        f"<ALTERID>{v['ALTERID']}</ALTERID>"
        f"{entries}"
        "</VOUCHER>"
    )


def _vouchers_xml() -> str:
    items = "".join(_voucher_xml(v) for v in STATE["vouchers"])
    return _envelope(f'<COLLECTION NAME="Voucher Collection">{items}</COLLECTION>')


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
    parser.add_argument("--company", default="Verify Tally B")
    args = parser.parse_args()
    STATE["companies"] = [args.company]

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[mock-tally] listening on 127.0.0.1:{args.port}, company={args.company!r}")
    print("[mock-tally] control: GET /control/state, GET /control/reject-next-n?n=N")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
