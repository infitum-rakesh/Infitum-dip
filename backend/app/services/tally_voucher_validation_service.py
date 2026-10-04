"""
tally_voucher_validation_service.py — TALLY-D deterministic pre-write
checks. A fixed Python check set, NOT a user-editable rule table like
ApInvoiceValidationRule (models/invoice_intelligence/rules.py) — these
are voucher-structural invariants (does this double-entry actually
balance, does the party ledger exist, is CGST+SGST/IGST mutually
exclusive), not org-configurable business policy. Disclosed scope choice,
not a silent gap: TALLY-E's reconciliation engine is where genuinely
configurable tolerance/policy checks belong.

Every check produces a TallyVoucherCheckResult row (PASS/WARNING/FAIL);
any FAIL means the voucher must not proceed past preview regardless of
anything a later phase does (see TallyVoucherPreviewStatus.BLOCKED).
"""
from __future__ import annotations

from app.models.invoice_intelligence.core import ApInvoice
from app.models.tally_mapping import TallyVoucherCheckCategory, TallyVoucherCheckResult

_BALANCE_TOLERANCE = 0.02


def _result(category, result, message, **details) -> dict:
    return {"category": category, "result": result, "message": message, "details": details}


def validate_voucher(
    voucher: dict, invoice: ApInvoice, *, known_party_names: set[str], known_ledger_names: set[str], possible_duplicate: bool,
) -> list[dict]:
    results: list[dict] = []
    C, R = TallyVoucherCheckCategory, TallyVoucherCheckResult

    # REQUIRED_FIELDS
    missing = [
        name for name, value in (
            ("invoice_number", invoice.invoice_number), ("voucher_date", voucher.get("voucher_date")),
            ("party_ledger_name", voucher.get("party_ledger_name")), ("total", invoice.total),
        ) if not value
    ]
    if missing:
        results.append(_result(C.REQUIRED_FIELDS, R.FAIL, f"Missing required field(s): {', '.join(missing)}.", missing=missing))
    else:
        results.append(_result(C.REQUIRED_FIELDS, R.PASS, "All required fields present."))

    # BALANCE — real double-entry check on the built voucher, not the raw invoice fields
    entries = voucher.get("ledger_entries") or []
    entry_sum = round(sum(e["amount"] for e in entries), 2)
    if abs(entry_sum) > _BALANCE_TOLERANCE:
        results.append(_result(
            C.BALANCE, R.FAIL,
            f"Voucher ledger entries do not balance (sum={entry_sum:.2f}, expected 0.00) — check subtotal/discount/freight/GST/TDS mapping.",
            sum=entry_sum,
        ))
    else:
        results.append(_result(C.BALANCE, R.PASS, "Ledger entries balance to zero.", sum=entry_sum))

    # LEDGER_EXISTS — party
    party_name = voucher.get("party_ledger_name")
    if not known_party_names:
        results.append(_result(
            C.LEDGER_EXISTS, R.WARNING,
            "No PARTY_MASTER extraction has been run yet for this connection — party ledger existence could not be checked.",
        ))
    elif party_name not in known_party_names:
        results.append(_result(
            C.LEDGER_EXISTS, R.FAIL,
            f"Party ledger '{party_name}' was not found among ledgers already extracted from Tally for this connection.",
            party_ledger_name=party_name,
        ))
    else:
        results.append(_result(C.LEDGER_EXISTS, R.PASS, f"Party ledger '{party_name}' found.", party_ledger_name=party_name))

    # LEDGER_EXISTS — other ledgers referenced by non-zero entries (soft:
    # LEDGER extraction is per-name, so absence here usually just means
    # this specific ledger hasn't been extracted yet, not that it's missing in Tally)
    other_names = {e["ledger_name"] for e in entries if e["ledger_name"] != party_name}
    unknown_others = sorted(n for n in other_names if n not in known_ledger_names)
    if unknown_others:
        results.append(_result(
            C.LEDGER_EXISTS, R.WARNING,
            f"Ledger(s) not yet confirmed against Tally extraction data: {', '.join(unknown_others)}.",
            unconfirmed_ledgers=unknown_others,
        ))

    # GST — CGST/SGST (intra-state) and IGST (inter-state) are mutually exclusive
    igst = float(invoice.igst_amount or 0)
    intra = float(invoice.cgst_amount or 0) + float(invoice.sgst_amount or 0)
    if igst and intra:
        results.append(_result(
            C.GST, R.WARNING,
            "Both CGST/SGST and IGST are present — a voucher should reflect either an intra-state (CGST+SGST) or inter-state (IGST) supply, not both.",
            cgst_sgst=intra, igst=igst,
        ))
    else:
        results.append(_result(C.GST, R.PASS, "GST split is consistent (not both intra- and inter-state)."))

    # DUPLICATE — lightweight pre-check; TALLY-E owns the real two-sided reconciliation
    if possible_duplicate:
        results.append(_result(
            C.DUPLICATE, R.WARNING,
            "A Tally voucher already extracted for this connection has the same party and a matching amount — possible duplicate.",
        ))
    else:
        results.append(_result(C.DUPLICATE, R.PASS, "No matching voucher found among extracted Tally data."))

    return results


def overall_status(results: list[dict]) -> str:
    return "BLOCKED" if any(r["result"] == TallyVoucherCheckResult.FAIL for r in results) else "READY"
