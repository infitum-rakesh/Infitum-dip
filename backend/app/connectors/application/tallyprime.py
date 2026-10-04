"""
TallyPrimeConnector — real XML-over-HTTP connectivity to a TallyPrime
instance's own local gateway (Gateway of Tally -> F1 Help -> Settings ->
Connectivity -> "Client/Server configuration" -> "Enable ODBC/HTTP
Server", default port 9000). Tally has no SQL dialect and no WSDL/SOAP —
its entire integration surface is one HTTP POST endpoint that accepts a
request XML envelope (built using TDL — Tally Definition Language) and
returns a response XML envelope. This is Tally's own long-documented XML
API, not a proprietary or undocumented mechanism.

Real, but NOT live-verified against an actual TallyPrime instance: no
licensed Tally install exists in this environment. Live-verified instead
against a local mock Tally XML/HTTP gateway purpose-built for this
integration's own tests (see backend/tests/test_tallyprime_connector.py),
which speaks the exact request/response envelope shape documented below —
same disclosed-but-real precedent as connectors/application/oracle_erp.py
and salesforce.py, one step further verified since a protocol-accurate
mock server exists here (unlike those, which have no local emulator at
all) — the same category of precedent as connectors/api/soap.py's "local
mock WSDL service" verification.

Company discovery uses the standard, Tally-documented TDL Collection
export requesting every Company object's NAME — Tally's own canonical
"list every company on this instance" request shape (this exact XML is
Tally's long-published TDL reference example, not invented here):

    <ENVELOPE>
     <HEADER>
      <VERSION>1</VERSION>
      <TALLYREQUEST>Export</TALLYREQUEST>
      <TYPE>Collection</TYPE>
      <ID>List of Companies</ID>
     </HEADER>
     <BODY>
      <DESC>
       <TDL>
        <TDLMESSAGE>
         <COLLECTION NAME="List of Companies" ISMODIFY="No">
          <TYPE>Company</TYPE>
          <FETCH>NAME</FETCH>
         </COLLECTION>
        </TDLMESSAGE>
       </TDL>
      </DESC>
     </BODY>
    </ENVELOPE>

Tally's HTTP gateway returns HTTP 200 even when the *request itself* is
rejected (malformed XML, unknown report/collection) — the real error is
embedded in the response body as a <LINEERROR> element, not surfaced via
HTTP status. Every method here checks for that explicitly rather than
trusting a 200 status code as success, per this integration's own
no-fake-success discipline (see connectors/base.py's module docstring).

Tally's Collection-export mechanism does not expose a clean "Tally
version" string. test_connection() honestly returns server_version=None
(DATA_NOT_AVAILABLE) rather than inventing one — a real deployment
wanting version detail would need a separate, more advanced TDL request
this integration does not implement, and that gap is disclosed here
rather than silently faked.

Company selection is never assumed: config["company_name"] is only ever
used to annotate list_companies()'s is_default flag and to warn in
test_connection() when it doesn't match a real discovered company — it is
never silently substituted with "the first company" or "the only
company." Every extraction/write method Phase B+ adds takes an explicit
`company` keyword, never a connector-level default.
"""
from __future__ import annotations

import time
import xml.sax.saxutils as saxutils
from datetime import date, datetime

import httpx
import xmltodict

from app.connectors.base import (
    BaseConnector,
    ConnectionTestResult,
    ConnectorError,
    ConnectorValidationError,
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_PORT = 9000

_LIST_COMPANIES_REQUEST = (
    "<ENVELOPE>"
    "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>List of Companies</ID></HEADER>"
    "<BODY><DESC><TDL><TDLMESSAGE>"
    '<COLLECTION NAME="List of Companies" ISMODIFY="No"><TYPE>Company</TYPE><FETCH>NAME</FETCH></COLLECTION>'
    "</TDLMESSAGE></TDL></DESC></BODY>"
    "</ENVELOPE>"
)


def _extract_company_names(envelope: dict) -> list[str]:
    """Tally's real Collection-export response shape nests the collection
    under BODY/DATA, not directly under ENVELOPE — i.e.
    ENVELOPE/BODY/DATA/COLLECTION/COMPANY[] (this is Tally's own
    documented TDL export response envelope, the same BODY/DATA wrapper
    every Collection/Object export uses, not something invented here). A
    bare ENVELOPE/COLLECTION is also accepted as a defensive fallback in
    case a differently-configured report/TDL definition omits the
    BODY/DATA wrapper — never relied on as the primary shape. xmltodict
    collapses a single matching child into a bare dict (not a one-item
    list) — normalized here so callers never special-case the "exactly
    one company" case. A COMPANY's real name comes from the
    <FETCH>NAME</FETCH>-produced child element (c["NAME"]), not the NAME=
    XML attribute Tally also echoes on the tag itself (c["@NAME"] once
    xmltodict parses it) — the child element is preferred since it's the
    actual fetched field value."""
    body = envelope.get("BODY") or {}
    data = body.get("DATA") or {}
    collection = data.get("COLLECTION") or envelope.get("COLLECTION") or {}
    companies = collection.get("COMPANY")
    if companies is None:
        return []
    if isinstance(companies, dict):
        companies = [companies]
    names: list[str] = []
    for c in companies:
        if isinstance(c, dict):
            name = c.get("NAME") or c.get("@NAME")
        else:
            name = c
        if name:
            names.append(str(name).strip())
    return names


def _xml_escape(value) -> str:
    """Escapes &/</> for embedding a value as XML text content (e.g. inside
    a <SVCURRENTCOMPANY> or <SYSTEM TYPE="Formulae"> tag's body). Does NOT
    escape double quotes — a Formulae string literal's quotes are TDL
    syntax living inside that XML text node, not XML attribute delimiters,
    so they must stay literal. A party/voucher-type/company name that
    itself contains a literal `"` would break the generated Formulae
    string and is a disclosed, unhandled edge case in this phase."""
    return saxutils.escape(str(value))


def _to_tally_date(value) -> str | None:
    """Tally's SVFROMDATE/SVTODATE static variables take dates as
    YYYYMMDD. Accepts a date/datetime object, an ISO "YYYY-MM-DD" string
    (the natural shape a caller passes), or a string already in Tally's
    own YYYYMMDD form (passed through unchanged)."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y%m%d")
    s = str(value).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s.replace("-", "")
    return s


def _extract_import_result(envelope: dict) -> dict:
    """Tally's real 'Import Data' response reports what it actually did —
    CREATED/ALTERED counts plus the newly-created voucher's own
    LASTVCHID/LASTMID — nested the same BODY/DATA way every other
    response in this file is (see _extract_company_names' docstring on
    why that wrapper is checked defensively rather than assumed). The
    wrapping tag name itself is the one part of this shape not
    independently confirmable in this environment (no licensed Tally
    instance — see module docstring): both RESPONSE and IMPORTRESULT are
    accepted since public TDL references disagree on which Tally version
    uses which, and a bare ENVELOPE-level tag is accepted too as the same
    defensive fallback every other extractor here already uses."""
    body = envelope.get("BODY") or {}
    data = body.get("DATA") or {}
    result = data.get("RESPONSE") or data.get("IMPORTRESULT") or envelope.get("RESPONSE") or envelope.get("IMPORTRESULT") or {}

    def _int(key: str) -> int:
        v = result.get(key)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    return {
        "created": _int("CREATED"),
        "altered": _int("ALTERED"),
        "errors": _int("ERRORS"),
        "last_voucher_id": result.get("LASTVCHID"),
        "last_master_id": result.get("LASTMID"),
    }


def _collection_items(envelope: dict, item_tag: str) -> list[dict]:
    """Generalizes _extract_company_names' BODY/DATA/COLLECTION navigation
    (with the same defensive bare-ENVELOPE/COLLECTION fallback) to any
    Collection item tag (VOUCHER, LEDGER, ...) — every Collection-export
    response this connector issues shares this one envelope shape."""
    body = envelope.get("BODY") or {}
    data = body.get("DATA") or {}
    collection = data.get("COLLECTION") or envelope.get("COLLECTION") or {}
    items = collection.get(item_tag)
    if items is None:
        return []
    if isinstance(items, dict):
        items = [items]
    return [i for i in items if isinstance(i, dict)]


def _normalize_voucher(v: dict) -> dict:
    ledger_entries = v.get("ALLLEDGERENTRIES.LIST")
    if isinstance(ledger_entries, dict):
        ledger_entries = [ledger_entries]
    elif not isinstance(ledger_entries, list):
        ledger_entries = []
    return {
        "date": v.get("DATE"),
        "voucher_number": v.get("VOUCHERNUMBER"),
        "voucher_type": v.get("VOUCHERTYPENAME"),
        "party_ledger_name": v.get("PARTYLEDGERNAME"),
        "narration": v.get("NARRATION"),
        "reference": v.get("REFERENCE"),
        "guid": v.get("GUID"),
        "alter_id": v.get("ALTERID"),
        "ledger_entries": [
            {
                "ledger_name": le.get("LEDGERNAME"),
                "amount": le.get("AMOUNT"),
                "is_deemed_positive": le.get("ISDEEMEDPOSITIVE"),
            }
            for le in ledger_entries if isinstance(le, dict)
        ],
    }


def _nonzero_amount(value) -> bool:
    """Tally amounts often arrive suffixed " Dr"/" Cr" rather than as a
    bare signed number. An amount this connector can't parse at all is
    treated as non-zero (surfaced, not silently dropped) rather than
    guessed at."""
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    for suffix in (" Dr", " Cr", " DR", " CR", " dr", " cr"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
            break
    try:
        return abs(float(s)) > 1e-9
    except ValueError:
        return True


# Tally has no rigid party-type flag on a Ledger — supplier/customer is
# inferred from which standard parent group the ledger sits under (the
# same convention documented on BaseConnector.get_party_master). A real
# deployment could rename or regroup these, which is a disclosed
# limitation of inferring party_type this way.
_PARTY_TYPE_PARENTS = {"SUPPLIER": "Sundry Creditors", "CUSTOMER": "Sundry Debtors"}


class TallyPrimeConnector(BaseConnector):
    connection_type = "TALLY_PRIME"

    def _base_url(self) -> str:
        host = (self.config.get("host") or "").strip()
        if not host:
            raise ConnectorValidationError("This TallyPrime connection has no Host configured.")
        if host.startswith("http://") or host.startswith("https://"):
            return host.rstrip("/")
        port = self.config.get("port") or DEFAULT_PORT
        # Tally's own HTTP gateway is plain HTTP only (no TLS option) —
        # this is Tally's own protocol choice, not a limitation here; see
        # class docstring.
        return f"http://{host}:{port}"

    def _timeout(self) -> float:
        try:
            return float(self.config.get("timeout_seconds") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT

    def _headers(self) -> dict:
        headers = {"Content-Type": "text/xml; charset=utf-8"}
        token = self.secrets.get("auth_token")
        if token:
            # Tally's own gateway has no built-in auth — this only matters
            # for a deployment that fronts it with a reverse proxy. See
            # FIELD_SCHEMAS["TALLY_PRIME"]'s help text.
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _post(self, request_xml: str) -> dict:
        """POSTs one Tally XML request and returns the parsed response as
        a plain dict (via xmltodict). Raises ConnectorError for real
        transport failures AND for Tally's own embedded error responses
        (a <LINEERROR> in the body) — see class docstring on why the HTTP
        status code alone is never trusted as success here."""
        url = self._base_url()
        try:
            with httpx.Client(timeout=self._timeout()) as client:
                resp = client.post(url, content=request_xml.encode("utf-8"), headers=self._headers())
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach TallyPrime at {url}: {exc}")) from exc
        if resp.status_code >= 400:
            raise ConnectorError(self._scrub(f"TallyPrime's HTTP gateway rejected the request: HTTP {resp.status_code}"))
        text = resp.text or ""
        if not text.strip():
            raise ConnectorError(
                "TallyPrime's HTTP gateway returned an empty response — confirm a company is open in Tally "
                "and the HTTP server is enabled (Gateway of Tally > F1 Help > Settings > Connectivity)."
            )
        try:
            parsed = xmltodict.parse(text)
        except Exception as exc:  # noqa: BLE001 - xmltodict raises assorted expat parse-error subclasses
            raise ConnectorError(f"TallyPrime returned a response that was not valid XML: {exc}") from exc
        envelope = parsed.get("ENVELOPE") or {}
        # Tally nests a request-level error as BODY/DATA/LINEERROR (the
        # same wrapper every export response uses — see
        # _extract_company_names' docstring); a bare ENVELOPE/LINEERROR is
        # also checked defensively in case a given error path omits it.
        body = envelope.get("BODY") or {}
        data = body.get("DATA") or {}
        line_error = data.get("LINEERROR") or envelope.get("LINEERROR")
        if line_error:
            raise ConnectorError(self._scrub(f"TallyPrime rejected the request: {line_error}"))
        return envelope

    def test_connection(self) -> ConnectionTestResult:
        t0 = time.monotonic()
        try:
            envelope = self._post(_LIST_COMPANIES_REQUEST)
        except (ConnectorValidationError, ConnectorError) as exc:
            return ConnectionTestResult(False, str(exc))
        latency_ms = (time.monotonic() - t0) * 1000
        companies = _extract_company_names(envelope)
        configured = (self.config.get("company_name") or "").strip()
        if not companies:
            return ConnectionTestResult(
                False,
                "Reached TallyPrime, but it reported zero companies — open at least one company in Tally and try again.",
                latency_ms,
            )
        plural = "y" if len(companies) == 1 else "ies"
        if configured and configured not in companies:
            return ConnectionTestResult(
                True,
                f"Connected to TallyPrime — found {len(companies)} compan{plural}, but the configured company "
                f"'{configured}' is not among them. Use Discover Companies to pick a real one.",
                latency_ms,
            )
        message = f"Connected to TallyPrime — {len(companies)} compan{plural} available"
        message += f", targeting '{configured}'." if configured else ". No company selected yet — use Discover Companies."
        # server_version deliberately left as DATA_NOT_AVAILABLE (None) —
        # see class docstring on why this request type can't honestly
        # report one.
        return ConnectionTestResult(True, message, latency_ms, server_version=None)

    def list_companies(self) -> list[dict]:
        envelope = self._post(_LIST_COMPANIES_REQUEST)
        names = _extract_company_names(envelope)
        configured = (self.config.get("company_name") or "").strip()
        return [
            {"name": name, "guid": None, "is_default": bool(configured) and name == configured}
            for name in names
        ]

    def get_company(self, name: str) -> dict:
        for company in self.list_companies():
            if company["name"] == name:
                return company
        raise ConnectorError(f"TallyPrime has no company named '{name}' on this instance.")

    # ── Controlled write-back (TALLY-I) ─────────────────────────────────
    # The one write-capable method this connector exposes. Callers (see
    # services/tally_writeback_service.py) are responsible for every
    # precondition this file's own docstrings insist on: TALLY-H write-back
    # approval, TALLY-D structural pre-write validation, and idempotency —
    # this method itself just sends exactly the XML it is given and
    # reports exactly what Tally's own response says happened, neither
    # assuming success from a 200 status nor inventing a GUID Tally didn't
    # actually return.
    def post_voucher(self, voucher_xml: str) -> dict:
        """POSTs a real 'Import Data' envelope (built by
        tally_voucher_builder.build_voucher_xml() — the exact XML a
        preview already showed the user before approval). _post() itself
        already raises ConnectorError for a LINEERROR (Tally rejected the
        request outright); this additionally treats CREATED=0 with no
        LINEERROR as a real failure too — Tally accepting the envelope but
        genuinely creating nothing must never look like a silent success
        to a caller that only checked for an exception."""
        envelope = self._post(voucher_xml)
        result = _extract_import_result(envelope)
        if result["created"] < 1:
            raise ConnectorError(
                self._scrub(f"TallyPrime accepted the request but reported CREATED={result['created']} — no voucher was actually created.")
            )
        return result

    # ── Read-only extraction (TALLY-B) ─────────────────────────────────
    # Every method below issues a real TDL Collection-export request
    # against SVCURRENTCOMPANY, following the same documented pattern as
    # company discovery above: STATICVARIABLES scope the request to one
    # company (and, where relevant, a date range via SVFROMDATE/SVTODATE —
    # Tally's own standard mechanism for date-bounding a report/collection,
    # not invented here); a COLLECTION of a given TYPE with an explicit
    # FETCH field list; an optional FILTER referencing a named SYSTEM
    # Formulae object for server-side filtering. This is the same TDL
    # vocabulary Tally's own bundled reports are built from.
    #
    # No native pagination: a Collection request returns its entire
    # filtered result set in one response (see get_vouchers' docstring on
    # BaseConnector). Bounded-size, checkpointed extraction over large
    # date ranges is services/tally_extraction_service.py's job, not
    # this connector's — it chunks by date range and calls these methods
    # once per chunk.

    def get_vouchers(self, *, company: str, voucher_type: str | None = None, from_date=None,
                      to_date=None, party: str | None = None) -> dict:
        static_vars = [f"<SVCURRENTCOMPANY>{_xml_escape(company)}</SVCURRENTCOMPANY>"]
        tally_from, tally_to = _to_tally_date(from_date), _to_tally_date(to_date)
        if tally_from:
            static_vars.append(f"<SVFROMDATE>{tally_from}</SVFROMDATE>")
        if tally_to:
            static_vars.append(f"<SVTODATE>{tally_to}</SVTODATE>")
        filters, formulae = [], []
        if voucher_type:
            filters.append("VoucherTypeFilter")
            formulae.append(f'<SYSTEM TYPE="Formulae" NAME="VoucherTypeFilter">$VoucherTypeName = "{_xml_escape(voucher_type)}"</SYSTEM>')
        if party:
            filters.append("PartyFilter")
            formulae.append(f'<SYSTEM TYPE="Formulae" NAME="PartyFilter">$PartyLedgerName = "{_xml_escape(party)}"</SYSTEM>')
        filter_tag = f"<FILTER>{','.join(filters)}</FILTER>" if filters else ""
        fetch = "DATE,VOUCHERNUMBER,VOUCHERTYPENAME,PARTYLEDGERNAME,NARRATION,REFERENCE,GUID,ALTERID,ALLLEDGERENTRIES.LIST"
        request_xml = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>Voucher Collection</ID></HEADER>"
            "<BODY><DESC>"
            f"<STATICVARIABLES>{''.join(static_vars)}</STATICVARIABLES>"
            "<TDL><TDLMESSAGE>"
            f'<COLLECTION NAME="Voucher Collection" ISMODIFY="No"><TYPE>Voucher</TYPE><FETCH>{fetch}</FETCH>{filter_tag}</COLLECTION>'
            f"{''.join(formulae)}"
            "</TDLMESSAGE></TDL>"
            "</DESC></BODY></ENVELOPE>"
        )
        envelope = self._post(request_xml)
        return {"vouchers": [_normalize_voucher(v) for v in _collection_items(envelope, "VOUCHER")]}

    def get_purchase_vouchers(self, *, company: str, **kwargs) -> dict:
        """"Purchase" is Tally's standard voucher-type name for this
        category, but a real deployment may have renamed or added
        secondary purchase voucher types — pass voucher_type explicitly
        to override this default."""
        kwargs.setdefault("voucher_type", "Purchase")
        return self.get_vouchers(company=company, **kwargs)

    def get_sales_vouchers(self, *, company: str, **kwargs) -> dict:
        kwargs.setdefault("voucher_type", "Sales")
        return self.get_vouchers(company=company, **kwargs)

    def get_party_master(self, *, company: str, party_type: str | None = None, **kwargs) -> dict:
        if party_type:
            parent = _PARTY_TYPE_PARENTS.get(party_type.upper())
            if not parent:
                raise ConnectorValidationError(
                    f"Unknown party_type '{party_type}' — expected SUPPLIER or CUSTOMER."
                )
            parents = [parent]
        else:
            parents = list(_PARTY_TYPE_PARENTS.values())
        formula = " OR ".join(f'$Parent = "{_xml_escape(p)}"' for p in parents)
        fetch = "NAME,PARENT,GSTIN,LEDSTATENAME,ADDRESS.LIST,EMAIL,LEDGERMOBILE,OPENINGBALANCE,CLOSINGBALANCE,GUID,ALTERID"
        request_xml = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>Party Master</ID></HEADER>"
            "<BODY><DESC>"
            f"<STATICVARIABLES><SVCURRENTCOMPANY>{_xml_escape(company)}</SVCURRENTCOMPANY></STATICVARIABLES>"
            "<TDL><TDLMESSAGE>"
            f'<COLLECTION NAME="Party Master" ISMODIFY="No"><TYPE>Ledger</TYPE><FETCH>{fetch}</FETCH><FILTER>PartyGroupFilter</FILTER></COLLECTION>'
            f'<SYSTEM TYPE="Formulae" NAME="PartyGroupFilter">{formula}</SYSTEM>'
            "</TDLMESSAGE></TDL>"
            "</DESC></BODY></ENVELOPE>"
        )
        envelope = self._post(request_xml)
        parties = []
        for l in _collection_items(envelope, "LEDGER"):
            address = l.get("ADDRESS.LIST")
            address_lines = address.get("ADDRESS") if isinstance(address, dict) else None
            if isinstance(address_lines, str):
                address_lines = [address_lines]
            elif not isinstance(address_lines, list):
                address_lines = []
            parent = l.get("PARENT")
            parties.append({
                "name": l.get("NAME"), "parent": parent, "gstin": l.get("GSTIN"),
                "state": l.get("LEDSTATENAME"), "address_lines": address_lines,
                "email": l.get("EMAIL"), "mobile": l.get("LEDGERMOBILE"),
                "opening_balance": l.get("OPENINGBALANCE"), "closing_balance": l.get("CLOSINGBALANCE"),
                "guid": l.get("GUID"), "alter_id": l.get("ALTERID"),
                "party_type": "SUPPLIER" if parent == "Sundry Creditors" else ("CUSTOMER" if parent == "Sundry Debtors" else None),
            })
        return {"parties": parties}

    def get_ledger(self, *, company: str, party: str, from_date=None, to_date=None) -> dict:
        fetch = "NAME,PARENT,OPENINGBALANCE,CLOSINGBALANCE,GUID,ALTERID"
        static_vars = [f"<SVCURRENTCOMPANY>{_xml_escape(company)}</SVCURRENTCOMPANY>"]
        tally_from, tally_to = _to_tally_date(from_date), _to_tally_date(to_date)
        if tally_from:
            static_vars.append(f"<SVFROMDATE>{tally_from}</SVFROMDATE>")
        if tally_to:
            static_vars.append(f"<SVTODATE>{tally_to}</SVTODATE>")
        request_xml = (
            "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>Ledger Detail</ID></HEADER>"
            "<BODY><DESC>"
            f"<STATICVARIABLES>{''.join(static_vars)}</STATICVARIABLES>"
            "<TDL><TDLMESSAGE>"
            f'<COLLECTION NAME="Ledger Detail" ISMODIFY="No"><TYPE>Ledger</TYPE><FETCH>{fetch}</FETCH><FILTER>NameFilter</FILTER></COLLECTION>'
            f'<SYSTEM TYPE="Formulae" NAME="NameFilter">$Name = "{_xml_escape(party)}"</SYSTEM>'
            "</TDLMESSAGE></TDL>"
            "</DESC></BODY></ENVELOPE>"
        )
        envelope = self._post(request_xml)
        ledgers = _collection_items(envelope, "LEDGER")
        if not ledgers:
            raise ConnectorError(f"TallyPrime has no ledger named '{party}' in company '{company}'.")
        ledger = ledgers[0]
        vouchers = self.get_vouchers(company=company, from_date=from_date, to_date=to_date, party=party)["vouchers"]
        return {
            "ledger": {
                "name": ledger.get("NAME"), "parent": ledger.get("PARENT"),
                "opening_balance": ledger.get("OPENINGBALANCE"), "closing_balance": ledger.get("CLOSINGBALANCE"),
                "guid": ledger.get("GUID"), "alter_id": ledger.get("ALTERID"),
            },
            "vouchers": vouchers,
        }

    def get_payments(self, *, company: str, from_date=None, to_date=None, party: str | None = None) -> dict:
        """Fetches BOTH Payment and Receipt voucher types — AP
        reconciliation needs money going out, AR reconciliation needs
        money coming in, and both are "payments" against an outstanding
        bill depending on which side of the relationship you're on. Each
        returned voucher is tagged with direction: "PAYMENT" | "RECEIPT"
        so a caller can filter to one side."""
        payments = self.get_vouchers(company=company, voucher_type="Payment", from_date=from_date, to_date=to_date, party=party)["vouchers"]
        receipts = self.get_vouchers(company=company, voucher_type="Receipt", from_date=from_date, to_date=to_date, party=party)["vouchers"]
        for v in payments:
            v["direction"] = "PAYMENT"
        for v in receipts:
            v["direction"] = "RECEIPT"
        return {"payments": payments + receipts}

    def _bill_allocations_request(self, company: str, party: str | None) -> str:
        fetch = "NAME,PARENT,BILLALLOCATIONS.LIST"
        if party:
            filter_tag, formula = "<FILTER>NameFilter</FILTER>", f'<SYSTEM TYPE="Formulae" NAME="NameFilter">$Name = "{_xml_escape(party)}"</SYSTEM>'
        else:
            parents_formula = " OR ".join(f'$Parent = "{p}"' for p in _PARTY_TYPE_PARENTS.values())
            filter_tag, formula = "<FILTER>PartyGroupFilter</FILTER>", f'<SYSTEM TYPE="Formulae" NAME="PartyGroupFilter">{parents_formula}</SYSTEM>'
        return (
            "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>Bill Allocations</ID></HEADER>"
            "<BODY><DESC>"
            f"<STATICVARIABLES><SVCURRENTCOMPANY>{_xml_escape(company)}</SVCURRENTCOMPANY></STATICVARIABLES>"
            "<TDL><TDLMESSAGE>"
            f'<COLLECTION NAME="Bill Allocations" ISMODIFY="No"><TYPE>Ledger</TYPE><FETCH>{fetch}</FETCH>{filter_tag}</COLLECTION>'
            f"{formula}"
            "</TDLMESSAGE></TDL>"
            "</DESC></BODY></ENVELOPE>"
        )

    def get_bill_allocations(self, *, company: str, party: str | None = None, **kwargs) -> dict:
        """Real bill-wise detail via Tally's BILLALLOCATIONS.LIST — only
        populated for ledgers with "Maintain bill-wise details" enabled
        (Tally's own per-ledger setting); a ledger without it simply
        contributes no bill rows here, not an error."""
        envelope = self._post(self._bill_allocations_request(company, party))
        allocations = []
        for ledger in _collection_items(envelope, "LEDGER"):
            bills = ledger.get("BILLALLOCATIONS.LIST")
            if isinstance(bills, dict):
                bills = [bills]
            elif not isinstance(bills, list):
                bills = []
            for b in bills:
                if not isinstance(b, dict):
                    continue
                allocations.append({
                    "party": ledger.get("NAME"), "bill_name": b.get("NAME"), "bill_date": b.get("BILLDATE"),
                    "bill_type": b.get("BILLTYPE"), "opening_balance": b.get("OPENINGBALANCE"),
                    "closing_balance": b.get("CLOSINGBALANCE"),
                })
        return {"bill_allocations": allocations}

    def get_outstanding(self, *, company: str, party: str | None = None) -> dict:
        """Real, current outstanding bills — every bill allocation whose
        closing balance is non-zero, from the same live BILLALLOCATIONS.LIST
        data get_bill_allocations() exposes (this is not a separate Tally
        report; Tally has no other TDL-exposed "outstanding" collection
        type). "As of now", not point-in-time — Tally's bill-wise closing
        balance always reflects the current state."""
        allocations = self.get_bill_allocations(company=company, party=party)["bill_allocations"]
        return {"outstanding": [b for b in allocations if _nonzero_amount(b.get("closing_balance"))]}
