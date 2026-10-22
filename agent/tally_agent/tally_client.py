"""
tally_client.py — LocalTallyClient: real XML-over-HTTP connectivity to
the TallyPrime instance running on THIS machine (Gateway of Tally -> F1
Help -> Settings -> Connectivity -> "Client/Server configuration" ->
"Enable ODBC/HTTP Server", default port 9000).

This is a deliberate, line-for-line duplicate of the TDL-building and
response-parsing logic in backend/app/connectors/application/
tallyprime.py's TallyPrimeConnector — NOT an import of it. The agent is
a standalone process shipped to a customer's machine; it has no access
to the backend package at runtime (see package __init__.py's own
docstring on why). Every return shape here is kept identical to that
connector's on purpose: AgentAwareTallyConnector (backend/app/
connectors/application/tallyprime_agent.py) relays this data straight
through to the exact same extraction/reconciliation/write-back pipeline
TallyPrimeConnector feeds — that pipeline must never be able to tell
which transport produced a given response.

Real, but like TallyPrimeConnector itself, NOT live-verified against an
actual TallyPrime instance (no licensed Tally install exists anywhere
in this project — see that module's own docstring for the identical
disclosure). Live-verified here instead against the same kind of local
mock Tally HTTP gateway backend/tests/test_tallyprime_connector.py
already established as this integration's verification precedent (see
agent/tests/mock_tally_server.py).
"""
from __future__ import annotations

import time
import xml.sax.saxutils as saxutils
from datetime import date, datetime

import httpx
import xmltodict

DEFAULT_TIMEOUT = 30.0

_LIST_COMPANIES_REQUEST = (
    "<ENVELOPE>"
    "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>List of Companies</ID></HEADER>"
    "<BODY><DESC><TDL><TDLMESSAGE>"
    '<COLLECTION NAME="List of Companies" ISMODIFY="No"><TYPE>Company</TYPE><FETCH>NAME</FETCH></COLLECTION>'
    "</TDLMESSAGE></TDL></DESC></BODY>"
    "</ENVELOPE>"
)

_PARTY_TYPE_PARENTS = {"SUPPLIER": "Sundry Creditors", "CUSTOMER": "Sundry Debtors"}


class TallyClientError(Exception):
    """Raised for any failure talking to the local TallyPrime instance —
    unreachable, non-200, unparseable XML, or Tally's own embedded
    LINEERROR. dispatcher.py catches this and reports it back to the
    server as this command's command_result.error, never an unhandled
    exception."""


def _xml_escape(value) -> str:
    return saxutils.escape(str(value))


def _to_tally_date(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y%m%d")
    s = str(value).strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s.replace("-", "")
    return s


def _extract_company_names(envelope: dict) -> list[str]:
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
        name = (c.get("NAME") or c.get("@NAME")) if isinstance(c, dict) else c
        if name:
            names.append(str(name).strip())
    return names


def _extract_import_result(envelope: dict) -> dict:
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
        "created": _int("CREATED"), "altered": _int("ALTERED"), "errors": _int("ERRORS"),
        "last_voucher_id": result.get("LASTVCHID"), "last_master_id": result.get("LASTMID"),
    }


def _collection_items(envelope: dict, item_tag: str) -> list[dict]:
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
        "date": v.get("DATE"), "voucher_number": v.get("VOUCHERNUMBER"), "voucher_type": v.get("VOUCHERTYPENAME"),
        "party_ledger_name": v.get("PARTYLEDGERNAME"), "narration": v.get("NARRATION"), "reference": v.get("REFERENCE"),
        "guid": v.get("GUID"), "alter_id": v.get("ALTERID"),
        "ledger_entries": [
            {"ledger_name": le.get("LEDGERNAME"), "amount": le.get("AMOUNT"), "is_deemed_positive": le.get("ISDEEMEDPOSITIVE")}
            for le in ledger_entries if isinstance(le, dict)
        ],
    }


def _nonzero_amount(value) -> bool:
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


class LocalTallyClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9000, timeout: float = DEFAULT_TIMEOUT):
        self._host = host
        self._port = port
        self._timeout = timeout

    def _base_url(self) -> str:
        if self._host.startswith("http://") or self._host.startswith("https://"):
            return self._host.rstrip("/")
        return f"http://{self._host}:{self._port}"

    def _post(self, request_xml: str) -> dict:
        url = self._base_url()
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(url, content=request_xml.encode("utf-8"), headers={"Content-Type": "text/xml; charset=utf-8"})
        except httpx.RequestError as exc:
            raise TallyClientError(f"Could not reach TallyPrime at {url}: {exc}") from exc
        if resp.status_code >= 400:
            raise TallyClientError(f"TallyPrime's HTTP gateway rejected the request: HTTP {resp.status_code}")
        text = resp.text or ""
        if not text.strip():
            raise TallyClientError(
                "TallyPrime's HTTP gateway returned an empty response — confirm a company is open in Tally "
                "and the HTTP server is enabled (Gateway of Tally > F1 Help > Settings > Connectivity)."
            )
        try:
            parsed = xmltodict.parse(text)
        except Exception as exc:  # noqa: BLE001 - xmltodict raises assorted expat parse-error subclasses
            raise TallyClientError(f"TallyPrime returned a response that was not valid XML: {exc}") from exc
        envelope = parsed.get("ENVELOPE") or {}
        body = envelope.get("BODY") or {}
        data = body.get("DATA") or {}
        line_error = data.get("LINEERROR") or envelope.get("LINEERROR")
        if line_error:
            raise TallyClientError(f"TallyPrime rejected the request: {line_error}")
        return envelope

    def test_connection(self) -> dict:
        t0 = time.monotonic()
        try:
            envelope = self._post(_LIST_COMPANIES_REQUEST)
        except TallyClientError as exc:
            return {"success": False, "message": str(exc)}
        latency_ms = (time.monotonic() - t0) * 1000
        companies = _extract_company_names(envelope)
        if not companies:
            return {"success": False, "message": "Reached TallyPrime, but it reported zero companies.", "latency_ms": latency_ms}
        return {"success": True, "message": f"Connected to TallyPrime — {len(companies)} company(ies) available.", "latency_ms": latency_ms}

    def list_companies(self) -> list[dict]:
        envelope = self._post(_LIST_COMPANIES_REQUEST)
        return [{"name": name, "guid": None, "is_default": False} for name in _extract_company_names(envelope)]

    def get_company(self, name: str) -> dict:
        for company in self.list_companies():
            if company["name"] == name:
                return company
        raise TallyClientError(f"TallyPrime has no company named '{name}' on this instance.")

    def post_voucher(self, voucher_xml: str) -> dict:
        envelope = self._post(voucher_xml)
        result = _extract_import_result(envelope)
        if result["created"] < 1:
            raise TallyClientError(f"TallyPrime accepted the request but reported CREATED={result['created']} — no voucher was actually created.")
        return result

    def get_vouchers(self, *, company: str, voucher_type: str | None = None, from_date=None, to_date=None, party: str | None = None) -> dict:
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
        kwargs.setdefault("voucher_type", "Purchase")
        return self.get_vouchers(company=company, **kwargs)

    def get_sales_vouchers(self, *, company: str, **kwargs) -> dict:
        kwargs.setdefault("voucher_type", "Sales")
        return self.get_vouchers(company=company, **kwargs)

    def get_party_master(self, *, company: str, party_type: str | None = None, **kwargs) -> dict:
        if party_type:
            parent = _PARTY_TYPE_PARENTS.get(party_type.upper())
            if not parent:
                raise TallyClientError(f"Unknown party_type '{party_type}' — expected SUPPLIER or CUSTOMER.")
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
                "name": l.get("NAME"), "parent": parent, "gstin": l.get("GSTIN"), "state": l.get("LEDSTATENAME"),
                "address_lines": address_lines, "email": l.get("EMAIL"), "mobile": l.get("LEDGERMOBILE"),
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
            raise TallyClientError(f"TallyPrime has no ledger named '{party}' in company '{company}'.")
        ledger = ledgers[0]
        vouchers = self.get_vouchers(company=company, from_date=from_date, to_date=to_date, party=party)["vouchers"]
        return {
            "ledger": {
                "name": ledger.get("NAME"), "parent": ledger.get("PARENT"), "opening_balance": ledger.get("OPENINGBALANCE"),
                "closing_balance": ledger.get("CLOSINGBALANCE"), "guid": ledger.get("GUID"), "alter_id": ledger.get("ALTERID"),
            },
            "vouchers": vouchers,
        }

    def get_payments(self, *, company: str, from_date=None, to_date=None, party: str | None = None) -> dict:
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
                    "bill_type": b.get("BILLTYPE"), "opening_balance": b.get("OPENINGBALANCE"), "closing_balance": b.get("CLOSINGBALANCE"),
                })
        return {"bill_allocations": allocations}

    def get_outstanding(self, *, company: str, party: str | None = None) -> dict:
        allocations = self.get_bill_allocations(company=company, party=party)["bill_allocations"]
        return {"outstanding": [b for b in allocations if _nonzero_amount(b.get("closing_balance"))]}
