"""
Standalone mock India-verification gateway — IV2 live verification only.

No real KYC/government API credentials exist in this sandbox (UIDAI
AUA/KUA licensing, a paid GSTN/MCA data-provider contract, or a KYC
aggregator account like Karza/Signzy/IDfy are all real-world business
relationships this environment doesn't have — see
connectors/verification/mock_provider.py's own docstring). This script is
a small, honest JSON/HTTP server speaking a contract this codebase itself
defines (there is no "real" wire format to mimic the way Tally's TDL XML
protocol exists independently of this project) so the REAL
MockVerificationProvider connector, the REAL verification orchestrator,
and the REAL browser-driven frontend can all be exercised against a REAL
local HTTP server end-to-end — only the actual government/KYC vendor is
replaced. Every response is clearly a dev/test fixture, never presented
as a live verification result (every normalized outcome this project
stores is tagged with the connection it came from, and MOCK_VERIFICATION_
PROVIDER is never selectable as a production connection type's label
implies otherwise).

Seeded PAN records (IV2 — see services/compliance/identity_verification_
service.py):
  ABCPD1234E -> VERIFIED,  holder "Rakesh Test Enterprises",  Individual
  AAACI1681G -> VERIFIED,  holder "Infitum Technologies Pvt Ltd",  Company
  AAACI1681H -> INACTIVE_OR_INVALID (provider confirms PAN exists but is inactive)
  Any other FORMAT-VALID PAN not listed above -> NOT_VERIFIED.

Seeded GSTIN records (IV7 — see tax_verification_service.py; every value
below is a REAL, checksum-valid GSTIN, independently verified against
the published mod-36 algorithm before being hardcoded here — see that
module's own docstring):
  27AAACI1681G1ZP -> VERIFIED, "Infitum Technologies Pvt Ltd", Active
  27AAACI1681H1ZN -> VERIFIED (registration found) but status "Cancelled"
  07ABCPD1234E1ZG -> VERIFIED, "Rakesh Test Enterprises", Active
  Any other checksum-valid GSTIN not listed -> NOT_VERIFIED.

Seeded TAN records (IV8):
  DELI12345A -> VERIFIED, deductor "Infitum Technologies Pvt Ltd"
  MUMB54321B -> INACTIVE_OR_INVALID

Seeded MCA companies (IV9 — CIN-exact lookup always unambiguous;
legal-name search returns every case-insensitive substring match, which
is deliberately ambiguous for "Shree Enterprises"):
  U72200MH2015PTC268234 -> "Infitum Technologies Private Limited", Active
  U15122MH2008PTC188888 -> "Dormant Traders Private Limited", Strike Off
  U51909GJ2012PTC071234 -> "Shree Enterprises Private Limited", Active
  U51909MH2015PTC071235 -> "Shree Enterprises India Private Limited", Active
  (legal_name search "Shree Enterprises" -> both of the last two, AMBIGUOUS_MATCH)

Seeded Udyam records (IV10):
  UDYAM-MH-01-0001234 -> VERIFIED, "Infitum Technologies Private Limited", Small
  UDYAM-DL-02-0005678 -> VERIFIED, "Rakesh Test Enterprises", Micro

Name-mismatch detection (PAN/GSTIN/TAN/Udyam): if `submitted_name` is
posted and doesn't case/whitespace-insensitively match the seeded
holder/legal/deductor/enterprise name, status becomes NAME_MISMATCH
instead of VERIFIED.

Run: python3 scripts/mock_verification_gateway.py [--port 9601]
Control: GET /control/fail-next?n=1  — the next N requests return a real
HTTP 503 (simulates a genuine provider outage, for UNAVAILABLE-path tests)
"""
import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAN_RECORDS = {
    "ABCPD1234E": {"status": "VERIFIED", "holder_name": "Rakesh Test Enterprises", "entity_type": "Individual"},
    "AAACI1681G": {"status": "VERIFIED", "holder_name": "Infitum Technologies Pvt Ltd", "entity_type": "Company"},
    "AAACI1681H": {"status": "INACTIVE_OR_INVALID", "holder_name": "Dormant Traders Pvt Ltd", "entity_type": "Company"},
}
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

GSTIN_RECORDS = {
    "27AAACI1681G1ZP": {
        "status": "VERIFIED", "legal_name": "Infitum Technologies Pvt Ltd", "trade_name": "Infitum Tech",
        "registration_status": "Active", "registration_date": "2018-07-01", "principal_place_of_business": "Mumbai, Maharashtra",
    },
    "27AAACI1681H1ZN": {
        "status": "VERIFIED", "legal_name": "Dormant Traders Pvt Ltd", "trade_name": "Dormant Traders",
        "registration_status": "Cancelled", "registration_date": "2016-03-15", "principal_place_of_business": "Pune, Maharashtra",
    },
    "07ABCPD1234E1ZG": {
        "status": "VERIFIED", "legal_name": "Rakesh Test Enterprises", "trade_name": "Rakesh Test Enterprises",
        "registration_status": "Active", "registration_date": "2020-01-10", "principal_place_of_business": "New Delhi",
    },
}

TAN_RECORDS = {
    "DELI12345A": {"status": "VERIFIED", "deductor_name": "Infitum Technologies Pvt Ltd", "entity_type": "Company"},
    "MUMB54321B": {"status": "INACTIVE_OR_INVALID", "deductor_name": "Dormant Traders Pvt Ltd", "entity_type": "Company"},
}
_TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")

MCA_COMPANIES = [
    {"cin": "U72200MH2015PTC268234", "legal_name": "Infitum Technologies Private Limited", "entity_type": "Private Limited Company",
     "status": "Active", "incorporation_date": "2015-06-12", "registered_office": "Bandra Kurla Complex, Mumbai, Maharashtra",
     "roc": "RoC-Mumbai"},
    {"cin": "U15122MH2008PTC188888", "legal_name": "Dormant Traders Private Limited", "entity_type": "Private Limited Company",
     "status": "Strike Off", "incorporation_date": "2008-11-03", "registered_office": "Shivaji Nagar, Pune, Maharashtra",
     "roc": "RoC-Pune"},
    {"cin": "U51909GJ2012PTC071234", "legal_name": "Shree Enterprises Private Limited", "entity_type": "Private Limited Company",
     "status": "Active", "incorporation_date": "2012-04-18", "registered_office": "Navrangpura, Ahmedabad, Gujarat",
     "roc": "RoC-Ahmedabad"},
    {"cin": "U51909MH2015PTC071235", "legal_name": "Shree Enterprises India Private Limited", "entity_type": "Private Limited Company",
     "status": "Active", "incorporation_date": "2015-09-22", "registered_office": "Andheri East, Mumbai, Maharashtra",
     "roc": "RoC-Mumbai"},
]
_CIN_RE = re.compile(r"^[UL][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")

UDYAM_RECORDS = {
    "UDYAM-MH-01-0001234": {"status": "VERIFIED", "enterprise_name": "Infitum Technologies Private Limited", "classification": "Small", "registration_date": "2021-02-14"},
    "UDYAM-DL-02-0005678": {"status": "VERIFIED", "enterprise_name": "Rakesh Test Enterprises", "classification": "Micro", "registration_date": "2022-08-09"},
}
_UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")

STATE = {"fail_next": 0}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[mock-verification] {self.address_string()} - {fmt % args}")

    def _json(self, code: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if self.path.startswith("/control/fail-next"):
            n = 1
            if "?" in self.path:
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                n = int(qs.get("n", ["1"])[0])
            STATE["fail_next"] = n
            self._json(200, {"fail_next": n})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self):
        if STATE["fail_next"] > 0:
            STATE["fail_next"] -= 1
            self._json(503, {"error": "simulated_provider_outage"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid_json"})
            return

        if self.path == "/verify/pan":
            self._verify_pan(body)
            return
        if self.path == "/verify/gstin":
            self._verify_gstin(body)
            return
        if self.path == "/verify/tan":
            self._verify_tan(body)
            return
        if self.path == "/search/mca":
            self._search_mca(body)
            return
        if self.path == "/verify/udyam":
            self._verify_udyam(body)
            return

        self._json(404, {"error": "not_found", "message": f"No handler for {self.path}"})

    def _verify_pan(self, body: dict):
        pan = (body.get("pan") or "").strip().upper()
        submitted_name = body.get("submitted_name")

        if not _PAN_RE.match(pan):
            self._json(200, {
                "status": "ERROR", "holder_name": None, "entity_type": None,
                "reference_id": f"MOCK-PAN-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
                "error_message": "PAN does not match the expected format — this gateway only verifies format-valid PANs.",
            })
            return

        record = PAN_RECORDS.get(pan)
        if record is None:
            self._json(200, {
                "status": "NOT_VERIFIED", "holder_name": None, "entity_type": None,
                "reference_id": f"MOCK-PAN-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
            })
            return

        status = record["status"]
        if status == "VERIFIED" and submitted_name and _normalize_name(submitted_name) != _normalize_name(record["holder_name"]):
            status = "NAME_MISMATCH"

        self._json(200, {
            "status": status, "holder_name": record["holder_name"], "entity_type": record["entity_type"],
            "reference_id": f"MOCK-PAN-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
        })

    def _verify_gstin(self, body: dict):
        gstin = (body.get("gstin") or "").strip().upper()
        submitted_name = body.get("submitted_name")

        if len(gstin) != 15:
            self._json(200, {
                "status": "ERROR", "legal_name": None, "trade_name": None, "registration_status": None,
                "reference_id": f"MOCK-GSTIN-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
                "error_message": "GSTIN does not match the expected 15-character format.",
            })
            return

        record = GSTIN_RECORDS.get(gstin)
        if record is None:
            self._json(200, {
                "status": "NOT_VERIFIED", "legal_name": None, "trade_name": None, "registration_status": None,
                "reference_id": f"MOCK-GSTIN-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
            })
            return

        status = record["status"]
        if status == "VERIFIED" and submitted_name and _normalize_name(submitted_name) != _normalize_name(record["legal_name"]):
            status = "NAME_MISMATCH"

        self._json(200, {
            "status": status, "legal_name": record["legal_name"], "trade_name": record["trade_name"],
            "registration_status": record["registration_status"], "registration_date": record["registration_date"],
            "principal_place_of_business": record["principal_place_of_business"],
            "reference_id": f"MOCK-GSTIN-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
        })

    def _verify_tan(self, body: dict):
        tan = (body.get("tan") or "").strip().upper()
        submitted_name = body.get("submitted_name")

        if not _TAN_RE.match(tan):
            self._json(200, {
                "status": "ERROR", "deductor_name": None, "entity_type": None,
                "reference_id": f"MOCK-TAN-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
                "error_message": "TAN does not match the expected format.",
            })
            return

        record = TAN_RECORDS.get(tan)
        if record is None:
            self._json(200, {
                "status": "NOT_VERIFIED", "deductor_name": None, "entity_type": None,
                "reference_id": f"MOCK-TAN-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
            })
            return

        status = record["status"]
        if status == "VERIFIED" and submitted_name and _normalize_name(submitted_name) != _normalize_name(record["deductor_name"]):
            status = "NAME_MISMATCH"

        self._json(200, {
            "status": status, "deductor_name": record["deductor_name"], "entity_type": record["entity_type"],
            "reference_id": f"MOCK-TAN-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
        })

    def _search_mca(self, body: dict):
        cin = (body.get("cin") or "").strip().upper()
        legal_name = body.get("legal_name")

        if cin:
            if not _CIN_RE.match(cin):
                self._json(200, {"status": "ERROR", "matches": [], "error_message": "CIN does not match the expected format."})
                return
            company = next((c for c in MCA_COMPANIES if c["cin"] == cin), None)
            if company is None:
                self._json(200, {"status": "NOT_FOUND", "matches": []})
                return
            self._json(200, {"status": "FOUND", "matches": [self._mca_match(company)]})
            return

        if legal_name:
            needle = _normalize_name(legal_name)
            matches = [self._mca_match(c) for c in MCA_COMPANIES if needle in _normalize_name(c["legal_name"])]
            if not matches:
                self._json(200, {"status": "NOT_FOUND", "matches": []})
            elif len(matches) == 1:
                self._json(200, {"status": "FOUND", "matches": matches})
            else:
                self._json(200, {"status": "AMBIGUOUS", "matches": matches})
            return

        self._json(400, {"error": "missing_identifier", "message": "Provide either cin or legal_name."})

    @staticmethod
    def _mca_match(company: dict) -> dict:
        return {
            "cin": company["cin"], "legal_name": company["legal_name"], "entity_type": company["entity_type"],
            "status": company["status"], "incorporation_date": company["incorporation_date"],
            "registered_office": company["registered_office"], "roc": company["roc"],
            "reference_id": f"MOCK-MCA-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
        }

    def _verify_udyam(self, body: dict):
        udyam_number = (body.get("udyam_number") or "").strip().upper()
        submitted_name = body.get("submitted_name")

        if not _UDYAM_RE.match(udyam_number):
            self._json(200, {
                "status": "ERROR", "enterprise_name": None, "classification": None,
                "reference_id": f"MOCK-UDYAM-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
                "error_message": "Udyam Registration Number does not match the expected format.",
            })
            return

        record = UDYAM_RECORDS.get(udyam_number)
        if record is None:
            self._json(200, {
                "status": "NOT_VERIFIED", "enterprise_name": None, "classification": None,
                "reference_id": f"MOCK-UDYAM-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
            })
            return

        status = record["status"]
        if status == "VERIFIED" and submitted_name and _normalize_name(submitted_name) != _normalize_name(record["enterprise_name"]):
            status = "NAME_MISMATCH"

        self._json(200, {
            "status": status, "enterprise_name": record["enterprise_name"], "classification": record["classification"],
            "registration_date": record["registration_date"],
            "reference_id": f"MOCK-UDYAM-{uuid.uuid4().hex[:12]}", "verified_at": _now_iso(),
        })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9601)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[mock-verification] listening on 127.0.0.1:{args.port}")
    print(f"[mock-verification] seeded PAN records: {', '.join(PAN_RECORDS.keys())}")
    print("[mock-verification] control: GET /control/fail-next?n=N")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
