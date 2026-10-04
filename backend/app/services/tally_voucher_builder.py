"""
tally_voucher_builder.py — TALLY-D. Pure functions: ApInvoice + a current
TallyAccountMapping + a resolved party-ledger name IN, a voucher
structure (and its Tally "Import Data" XML serialization) OUT. No
database access, no network access, no side effects — mirrors
invoice_erp_mapping_service.build_erp_payload()'s own shape (a pure
mapping-application function, kept separate from the orchestration
service that resolves lookups and persists results).

build_voucher_xml() only SERIALIZES a voucher to the exact XML shape
TallyPrime's real "Import Data" API expects. TallyPrimeConnector.
post_voucher() (TALLY-I) is what actually transmits it — gated behind
TALLY-H's approval workflow via tally_writeback_service.py, never called
directly from here or from preview-building.

Double-entry construction (see the module's own accounting model,
disclosed rather than assumed obvious): the expense/purchase ledger and
each present GST ledger are booked Dr (positive amount, ISDEEMEDPOSITIVE
"Yes"); the party ledger is booked Cr for (total - tds_amount), and — a
real, disclosed simplification — any TDS is booked directly as a second
Cr line against a TDS Payable ledger within this SAME voucher (common
practice for many TallyPrime users; the textbook alternative is a
separate TDS journal entry, not modeled here). When
ApInvoice.total == subtotal - discount + freight + (cgst+sgst+igst+cess)
this construction sums to exactly zero; a real amount mismatch shows up
as the BALANCE check's real signal (see
tally_voucher_validation_service.py), not a manufactured rounding line.
"""
from __future__ import annotations

from app.connectors.application.tallyprime import _to_tally_date, _xml_escape
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceLine
from app.models.tally_mapping import TallyAccountMapping
from app.services.tally_mapping_service import apply_transform


class TallyVoucherBuildError(Exception):
    """A mapping-required slot has no resolvable value — a pre-build
    configuration failure, distinct from a validation WARNING/FAIL on an
    otherwise-successfully-built voucher."""


def _round2(value) -> float:
    return round(float(value or 0.0), 2)


def build_voucher(
    invoice: ApInvoice, lines: list[ApInvoiceLine], mapping_rows: list[TallyAccountMapping], *, party_ledger_name: str,
) -> dict:
    if not party_ledger_name:
        raise TallyVoucherBuildError("party_ledger_name is required to build a voucher (resolved from ApInvoice.vendor_name — the invoice has none).")

    header_map = {m.tally_field: m for m in mapping_rows if not m.is_line_mapping}
    invoice_fields = {c.name: getattr(invoice, c.name) for c in invoice.__table__.columns}

    def resolve(tally_field: str, default=None):
        m = header_map.get(tally_field)
        if m is None:
            return default
        raw = invoice_fields.get(m.canonical_field)
        value = apply_transform(raw, m.transform_config, m.default_value)
        if value is None and (m.transform_config or {}).get("required"):
            raise TallyVoucherBuildError(f"Required slot '{tally_field}' (from '{m.canonical_field}') has no value.")
        return value if value is not None else default

    voucher_type_name = resolve("voucher_type_name", "Purchase")
    voucher_date = resolve("voucher_date")
    narration = resolve("narration")
    purchase_ledger_name = resolve("purchase_ledger_name", "Purchase Accounts")
    cgst_ledger_name = resolve("cgst_ledger_name", "CGST")
    sgst_ledger_name = resolve("sgst_ledger_name", "SGST")
    igst_ledger_name = resolve("igst_ledger_name", "IGST")
    cess_ledger_name = resolve("cess_ledger_name", "Cess")
    tds_ledger_name = resolve("tds_ledger_name", "TDS Payable")

    subtotal = _round2(invoice.subtotal)
    discount = _round2(invoice.discount)
    freight = _round2(invoice.freight)
    total = _round2(invoice.total)
    tds_amount = _round2(invoice.tds_amount)
    cgst, sgst, igst, cess = _round2(invoice.cgst_amount), _round2(invoice.sgst_amount), _round2(invoice.igst_amount), _round2(invoice.cess_amount)

    expense_amount = round(subtotal - discount + freight, 2)

    entries = [{"ledger_name": purchase_ledger_name, "amount": expense_amount, "is_deemed_positive": "Yes"}]
    for name, amt in ((cgst_ledger_name, cgst), (sgst_ledger_name, sgst), (igst_ledger_name, igst), (cess_ledger_name, cess)):
        if amt:
            entries.append({"ledger_name": name, "amount": amt, "is_deemed_positive": "Yes"})

    party_net = round(total - tds_amount, 2)
    entries.append({"ledger_name": party_ledger_name, "amount": -party_net, "is_deemed_positive": "No"})
    if tds_amount:
        entries.append({"ledger_name": tds_ledger_name, "amount": -tds_amount, "is_deemed_positive": "No"})

    return {
        "voucher_type": voucher_type_name,
        "voucher_date": voucher_date,
        "party_ledger_name": party_ledger_name,
        "narration": narration,
        "reference": invoice.invoice_number,
        "ledger_entries": entries,
    }


def build_voucher_xml(voucher: dict, *, company: str) -> str:
    """Serializes to TallyPrime's real Voucher-Import XML shape
    (<TALLYREQUEST>Import Data</TALLYREQUEST> / <IMPORTDATA>/
    <REQUESTDATA>/<TALLYMESSAGE><VOUCHER>...). Preview/audit only — see
    module docstring."""
    entries_xml = "".join(
        f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{_xml_escape(e['ledger_name'])}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{e['is_deemed_positive']}</ISDEEMEDPOSITIVE>"
        f"<AMOUNT>{e['amount']:.2f}</AMOUNT></ALLLEDGERENTRIES.LIST>"
        for e in voucher["ledger_entries"]
    )
    narration_xml = f"<NARRATION>{_xml_escape(voucher['narration'])}</NARRATION>" if voucher.get("narration") else ""
    reference_xml = f"<REFERENCE>{_xml_escape(voucher['reference'])}</REFERENCE>" if voucher.get("reference") else ""
    tally_date = _to_tally_date(voucher.get("voucher_date")) or ""
    return (
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>"
        "<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>"
        f"<STATICVARIABLES><SVCURRENTCOMPANY>{_xml_escape(company)}</SVCURRENTCOMPANY></STATICVARIABLES>"
        "</REQUESTDESC><REQUESTDATA>"
        '<TALLYMESSAGE xmlns:UDF="TallyUDF">'
        f'<VOUCHER VCHTYPE="{_xml_escape(voucher["voucher_type"])}" ACTION="Create">'
        f"<DATE>{tally_date}</DATE>"
        f"<VOUCHERTYPENAME>{_xml_escape(voucher['voucher_type'])}</VOUCHERTYPENAME>"
        f"<PARTYLEDGERNAME>{_xml_escape(voucher['party_ledger_name'])}</PARTYLEDGERNAME>"
        f"{narration_xml}{reference_xml}{entries_xml}"
        "</VOUCHER></TALLYMESSAGE>"
        "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )
