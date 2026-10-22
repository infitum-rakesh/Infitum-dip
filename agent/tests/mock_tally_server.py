"""
mock_tally_server.py — a local HTTP server standing in for TallyPrime's
own XML/HTTP gateway, for testing tally_client.LocalTallyClient. Same
disclosed convention and envelope shape as backend/tests/
test_tallyprime_connector.py's own mock (ENVELOPE/BODY/DATA/COLLECTION/
...[], ENVELOPE/BODY/DATA/LINEERROR for errors) — intentionally kept in
sync with that file since both exercise the identical TDL/XML contract.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def envelope(inner_xml: str) -> str:
    return f"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DATA>{inner_xml}</DATA></BODY></ENVELOPE>"


def companies_xml(names: list[str]) -> str:
    companies = "".join(f'<COMPANY NAME="{n}"><NAME>{n}</NAME></COMPANY>' for n in names)
    return envelope(f'<COLLECTION NAME="List of Companies">{companies}</COLLECTION>')


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        state["last_request_body"] = body.decode("utf-8", errors="replace")
        state["request_count"] = state.get("request_count", 0) + 1

        mode = state.get("mode", "companies")
        if mode == "http_error":
            self.send_response(500)
            self.end_headers()
            return
        if mode == "line_error":
            payload = envelope("<LINEERROR>Unknown Request, Cannot Accept Data</LINEERROR>").encode("utf-8")
        elif mode == "import_success":
            payload = envelope(
                "<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS>"
                "<LASTVCHID>171</LASTVCHID><LASTMID>172</LASTMID></RESPONSE>"
            ).encode("utf-8")
        elif mode == "vouchers":
            payload = envelope(
                '<COLLECTION NAME="Voucher Collection">'
                '<VOUCHER><DATE>20260405</DATE><VOUCHERNUMBER>S-500</VOUCHERNUMBER><VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>'
                '<PARTYLEDGERNAME>Mock Buyer Co</PARTYLEDGERNAME><REFERENCE>INV-500</REFERENCE>'
                '<GUID>mock-guid-500</GUID><ALTERID>5</ALTERID>'
                '<ALLLEDGERENTRIES.LIST><LEDGERNAME>Sales Account</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-1000.00</AMOUNT></ALLLEDGERENTRIES.LIST>'
                '</VOUCHER>'
                '</COLLECTION>'
            ).encode("utf-8")
        else:
            payload = companies_xml(state.get("companies", [])).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockTallyServer:
    def __init__(self, companies: list[str] | None = None, mode: str = "companies"):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.state = {"mode": mode, "companies": companies or ["Mock Co Pvt Ltd"]}
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def state(self) -> dict:
        return self._server.state

    def start(self) -> "MockTallyServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()


def start_mock_tally_server(companies: list[str] | None = None, mode: str = "companies") -> MockTallyServer:
    return MockTallyServer(companies=companies, mode=mode).start()
