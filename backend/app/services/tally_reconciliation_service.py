"""
tally_reconciliation_service.py — TALLY-E orchestration.

Loads real ApInvoice rows (voucher_type=PURCHASE) for this org and real
TallyCanonicalVoucher rows (voucher_type='Purchase') for this connection,
keys both sides by (resolved party ledger name, rounded total) and hands
them to app.reconciliation.engine.diff_keys_and_fields() — the SAME
row-diffing primitive every other reconciliation in this codebase uses —
via two ListRowSource wrappers, with GST/TDS amounts compared through its
tolerance_columns option. This is the deliberate alternative to what
invoice_reconciliation_service.py (INV10) did: that service reimplemented
its own tolerance comparison, its own normalization, and its own
result/exception shape instead of reusing the engine that already
existed (see models/tally_reconciliation.py's own docstring). TALLY-E
only adds the two things the engine's two-source-diff abstraction
genuinely has no concept of: a ledger running-balance check and a
Payment-voucher-existence check.

Two disclosed simplifications, not silently assumed correct:
  - The ledger-balance check compares the sum of ALL of a vendor's
    ApInvoice purchase totals against Tally's real closing_balance for
    that party. ApInvoice has no paid/outstanding flag, so this is not a
    real open-items subledger reconciliation — it will show a "mismatch"
    for a vendor whose invoices have genuinely already been paid. Real
    per-invoice outstanding tracking is not built anywhere in this
    codebase yet; comparing absolute values (not signed) also sidesteps
    TallyCanonicalParty.closing_balance's Dr/Cr sign convention, which is
    not recorded anywhere in that model.
  - The payment-voucher-existence check only knows "does ANY Payment
    voucher exist for this party in Tally at all" — it cannot tell which
    specific invoice a specific payment voucher actually settled (no
    payment-to-invoice allocation exists in the canonical model), so it
    is a coarse existence signal, not a real settlement match.

TALLY-F — Review Queue escalation (_escalate_exceptions() below): the
master directive forbids a polymorphic/nullable ReviewQueueItem.document_id
and forbids fake Document rows, so only exception types that already carry
a real invoice_id (MISSING/MISMATCH/PAYMENT_VOUCHER_MISSING — see
ESCALATE_TYPES) ever get escalated, since only those already have a real
anchor available: the SAME real Document the flagged ApInvoice was itself
materialized from. EXTRA/DUPLICATE_TARGET_MATCH (voucher-only, no invoice
at all) and LEDGER_BALANCE_MISMATCH (party-level, no single invoice) have
no such anchor and are never escalated — a disclosed limitation, not an
oversight; see review_queue_service.create_from_tally_exception()'s own
docstring for the reuse mechanics.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.document import Document
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceVoucherType
from app.models.tally_canonical import TallyCanonicalParty, TallyCanonicalVoucher, TallyVoucherDirection
from app.models.tally_reconciliation import (
    TallyExceptionType, TallyReconciliationException, TallyReconciliationRun, TallyReconciliationRunStatus,
)
from app.reconciliation.engine import ListRowSource, diff_keys_and_fields
from app.services import review_queue_service, tally_mapping_service
from app.services.tally_voucher_preview_service import resolve_party_ledger_name

DEFAULT_TOLERANCE = 0.02
GST_TDS_COMPARE_COLUMNS = ["cgst_amount", "sgst_amount", "igst_amount", "tds_amount"]
# How many days an invoice must age before a missing Payment voucher is
# worth flagging — a grace period, not a real due-date/terms calculation
# (ApInvoice.due_date exists but a real "is this actually overdue" check
# is out of TALLY-E's scope; this is a coarse, disclosed proxy).
PAYMENT_GRACE_DAYS = 30

# TALLY-F — only these exception types carry a real invoice_id (hence a
# real anchor Document to reuse) — see module docstring.
ESCALATE_TYPES = {TallyExceptionType.MISSING, TallyExceptionType.MISMATCH, TallyExceptionType.PAYMENT_VOUCHER_MISSING}


class TallyReconciliationError(Exception):
    """Bad request — connection/run not found, or nothing to compare."""


def _normalize_name(name: str | None) -> str:
    """Same normalization as tally_canonical_service/tally_voucher_preview_service
    — kept in sync so a resolved party ledger name keys identically on
    both sides of the diff."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _round2(v) -> float | None:
    return None if v is None else round(float(v), 2)


def _parse_amount(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(dt: datetime | None) -> datetime | None:
    """SQLite round-trips DateTime(timezone=True) values as naive (unlike
    Postgres, which preserves tzinfo) — ApInvoice.invoice_date is always
    written as UTC-aware (see invoice_processing_service._parse_date), so
    a naive value read back is safely assumed UTC rather than the local
    zone. Without this, comparing against the tz-aware `cutoff` below
    raises TypeError on SQLite but not on Postgres."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _tally_gst_tds_from_entries(ledger_entries: list[dict], ledger_name_map: dict[str, str]) -> dict[str, float]:
    """TallyCanonicalVoucher has no dedicated GST/TDS columns — GST/TDS
    booking is just ledger entries in Tally's own model, not a
    first-class concept (see that model's own docstring). Derives them
    for one voucher by matching each entry's ledger_name against the
    SAME account mapping TALLY-D's voucher builder uses, rather than
    hardcoding ledger names a second time."""
    amounts = {"cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0, "tds_amount": 0.0}
    field_by_name = {
        (ledger_name_map.get("cgst") or "").strip().lower(): "cgst_amount",
        (ledger_name_map.get("sgst") or "").strip().lower(): "sgst_amount",
        (ledger_name_map.get("igst") or "").strip().lower(): "igst_amount",
        (ledger_name_map.get("tds") or "").strip().lower(): "tds_amount",
    }
    field_by_name.pop("", None)
    for entry in ledger_entries or []:
        name = (entry.get("ledger_name") or "").strip().lower()
        field = field_by_name.get(name)
        if field:
            amounts[field] += abs(_parse_amount(entry.get("amount")))
    return {k: round(v, 2) for k, v in amounts.items()}


async def _ledger_name_map(db: AsyncSession, organization_id: int | None, connection_id: int) -> dict[str, str]:
    mapping_rows = await tally_mapping_service.get_current_mapping(db, organization_id, connection_id)
    if not mapping_rows:
        mapping_rows = await tally_mapping_service.seed_default_mapping(db, organization_id, connection_id)
    by_field = {m.tally_field: m.default_value for m in mapping_rows}
    return {
        "cgst": by_field.get("cgst_ledger_name") or "CGST",
        "sgst": by_field.get("sgst_ledger_name") or "SGST",
        "igst": by_field.get("igst_ledger_name") or "IGST",
        "tds": by_field.get("tds_ledger_name") or "TDS Payable",
    }


async def _escalate_exceptions(
    db: AsyncSession, exceptions: list[TallyReconciliationException], invoices_by_id: dict[int, ApInvoice],
) -> None:
    """TALLY-F. Only exception types in ESCALATE_TYPES ever reach here (see
    module docstring for why EXTRA/DUPLICATE_TARGET_MATCH/LEDGER_BALANCE_
    MISMATCH cannot: no real invoice, hence no real Document to anchor a
    ReviewQueueItem to). Skips silently — never fails the reconciliation
    run — if the invoice or its Document can no longer be found (a real,
    if rare, edge case: the invoice was deleted, or its Document row was
    removed independently of ApInvoice)."""
    for exc in exceptions:
        if exc.exception_type not in ESCALATE_TYPES or exc.invoice_id is None:
            continue
        invoice = invoices_by_id.get(exc.invoice_id)
        if invoice is None:
            continue
        doc = await db.get(Document, invoice.document_id)
        if doc is None:
            continue
        item = await review_queue_service.create_from_tally_exception(db, doc, issue=exc.message)
        exc.review_item_id = item.id


async def run_reconciliation(
    db: AsyncSession, connection: Connection, *, company: str, tolerance: float = DEFAULT_TOLERANCE, created_by: int | None = None,
    schedule_id: int | None = None, trigger: str = "Manual",
) -> TallyReconciliationRun:
    run = TallyReconciliationRun(
        organization_id=connection.organization_id, connection_id=connection.id, company_name=company,
        tolerance=tolerance, status=TallyReconciliationRunStatus.RUNNING, started_at=_utcnow(), created_by=created_by,
        schedule_id=schedule_id, trigger=trigger,
    )
    db.add(run)
    await db.flush()

    try:
        invoices = list((await db.execute(
            select(ApInvoice).where(
                ApInvoice.organization_id == connection.organization_id,
                ApInvoice.voucher_type == ApInvoiceVoucherType.PURCHASE,
            )
        )).scalars().all())
        vouchers = list((await db.execute(
            select(TallyCanonicalVoucher).where(
                TallyCanonicalVoucher.connection_id == connection.id,
                func.lower(TallyCanonicalVoucher.voucher_type) == "purchase",
            )
        )).scalars().all())
        ledger_name_map = await _ledger_name_map(db, connection.organization_id, connection.id)

        # Engine-facing rows (only the key + compare columns) plus
        # side-lookups back to the real ORM rows for exception linking —
        # diff_keys_and_fields() only ever returns key TUPLES and the
        # projected compare columns, never arbitrary extra fields.
        source_rows: list[dict] = []
        invoice_id_by_key: dict[tuple, int] = {}
        invoice_party_by_key: dict[tuple, str] = {}
        invoices_by_id = {inv.id: inv for inv in invoices}
        party_name_by_invoice_id: dict[int, str] = {}
        for inv in invoices:
            party_name_by_invoice_id[inv.id] = await resolve_party_ledger_name(db, connection.id, inv.vendor_name)
            if inv.total is None:
                continue
            party_key = _normalize_name(party_name_by_invoice_id[inv.id])
            if not party_key:
                continue
            amount_key = f"{_round2(inv.total):.2f}"
            key = (party_key, amount_key)
            source_rows.append({
                "party_key": party_key, "amount_key": amount_key,
                "cgst_amount": _round2(inv.cgst_amount) or 0.0, "sgst_amount": _round2(inv.sgst_amount) or 0.0,
                "igst_amount": _round2(inv.igst_amount) or 0.0, "tds_amount": _round2(inv.tds_amount) or 0.0,
            })
            # First row for a key wins — mirrors _fetch_key_rows' own
            # first-wins dedup discipline in engine.py, so this side-index
            # never disagrees with which row the engine actually compared.
            invoice_id_by_key.setdefault(key, inv.id)
            invoice_party_by_key.setdefault(key, party_name_by_invoice_id[inv.id])

        target_rows: list[dict] = []
        voucher_id_by_key: dict[tuple, int] = {}
        voucher_party_by_key: dict[tuple, str] = {}
        for v in vouchers:
            if v.total_amount is None:
                continue
            party_key = _normalize_name(v.party_ledger_name)
            if not party_key:
                continue
            amount_key = f"{_round2(v.total_amount):.2f}"
            key = (party_key, amount_key)
            gst_tds = _tally_gst_tds_from_entries(v.ledger_entries, ledger_name_map)
            target_rows.append({"party_key": party_key, "amount_key": amount_key, **gst_tds})
            voucher_id_by_key.setdefault(key, v.id)
            voucher_party_by_key.setdefault(key, v.party_ledger_name)

        diff = diff_keys_and_fields(
            source=ListRowSource(source_rows, name="ApInvoice purchase rows"),
            target=ListRowSource(target_rows, name="Tally purchase vouchers"),
            key_columns=["party_key", "amount_key"], compare_columns=GST_TDS_COMPARE_COLUMNS,
            tolerance_columns={c: tolerance for c in GST_TDS_COMPARE_COLUMNS},
        )

        exceptions: list[TallyReconciliationException] = []

        def add_exc(exception_type, *, invoice_id=None, voucher_id=None, party_name=None, column=None,
                    source_value=None, target_value=None, message, details=None):
            exceptions.append(TallyReconciliationException(
                organization_id=connection.organization_id, run_id=run.id, exception_type=exception_type,
                invoice_id=invoice_id, voucher_id=voucher_id, party_name=party_name, column=column,
                source_value=None if source_value is None else str(source_value),
                target_value=None if target_value is None else str(target_value),
                message=message, details=details or {},
            ))

        for key in diff["missing_keys"]:
            party_name = invoice_party_by_key.get(key)
            add_exc(
                TallyExceptionType.MISSING, invoice_id=invoice_id_by_key.get(key), party_name=party_name,
                message=f"No matching Tally purchase voucher found for {party_name or 'this vendor'} at amount {key[1]}.",
            )
        for key in diff["extra_keys"]:
            party_name = voucher_party_by_key.get(key)
            add_exc(
                TallyExceptionType.EXTRA, voucher_id=voucher_id_by_key.get(key), party_name=party_name,
                message=f"Tally purchase voucher for {party_name or 'this vendor'} at amount {key[1]} has no matching ApInvoice.",
            )
        for m in diff["mismatches"]:
            skey, tkey = m["key"], m["target_key"]
            add_exc(
                TallyExceptionType.MISMATCH, invoice_id=invoice_id_by_key.get(skey), voucher_id=voucher_id_by_key.get(tkey),
                party_name=invoice_party_by_key.get(skey), column=m["column"],
                source_value=m["source_value"], target_value=m["target_value"],
                message=f"{m['column']} differs beyond tolerance: invoice={m['source_value']} vs tally={m['target_value']}.",
            )
        for d in diff["duplicate_target_matches"]:
            tkey = d["target_key"]
            party_name = voucher_party_by_key.get(tkey)
            add_exc(
                TallyExceptionType.DUPLICATE_TARGET_MATCH, voucher_id=voucher_id_by_key.get(tkey), party_name=party_name,
                message=(
                    f"More than one Tally purchase voucher shares this vendor+amount for {party_name or 'this vendor'} "
                    "— which one the matching ApInvoice actually corresponds to was not decided; resolve manually."
                ),
            )
        mismatched_source_keys = {m["key"] for m in diff["mismatches"]}
        for sample in diff["matched_sample"]:
            skey, tkey = sample["source_key"], sample["target_key"]
            if skey in mismatched_source_keys:
                continue
            add_exc(
                TallyExceptionType.MATCHED, invoice_id=invoice_id_by_key.get(skey), voucher_id=voucher_id_by_key.get(tkey),
                party_name=invoice_party_by_key.get(skey), message="Matched — no discrepancy beyond tolerance.",
            )

        # Ledger balance check — genuinely not a two-source diff (see
        # module docstring's disclosed simplification).
        ap_totals_by_party: dict[str, float] = {}
        for inv in invoices:
            if inv.total is None:
                continue
            party_name = party_name_by_invoice_id.get(inv.id)
            if not party_name:
                continue
            ap_totals_by_party[party_name] = ap_totals_by_party.get(party_name, 0.0) + float(inv.total)

        parties = list((await db.execute(
            select(TallyCanonicalParty).where(TallyCanonicalParty.connection_id == connection.id)
        )).scalars().all())
        party_balance_by_name = {p.name: p.closing_balance for p in parties if p.closing_balance is not None}

        ledger_tolerance = max(1.0, tolerance * 100)
        ledger_mismatch_count = 0
        for party_name, ap_total in ap_totals_by_party.items():
            tally_balance = party_balance_by_name.get(party_name)
            if tally_balance is None:
                continue
            if abs(round(abs(ap_total), 2) - round(abs(tally_balance), 2)) > ledger_tolerance:
                ledger_mismatch_count += 1
                add_exc(
                    TallyExceptionType.LEDGER_BALANCE_MISMATCH, party_name=party_name,
                    source_value=round(ap_total, 2), target_value=round(tally_balance, 2),
                    message=(
                        f"Sum of {party_name}'s ApInvoice purchase totals ({round(ap_total, 2)}) differs from Tally's "
                        f"closing balance ({round(tally_balance, 2)}) by more than {ledger_tolerance}."
                    ),
                )

        # Payment-voucher-existence check — also not a two-source diff.
        payment_party_names = set((await db.execute(
            select(TallyCanonicalVoucher.party_ledger_name).where(
                TallyCanonicalVoucher.connection_id == connection.id,
                TallyCanonicalVoucher.direction == TallyVoucherDirection.PAYMENT,
            )
        )).scalars().all())
        payment_party_keys = {_normalize_name(p) for p in payment_party_names if p}

        source_keys = {(r["party_key"], r["amount_key"]) for r in source_rows}
        target_keys = {(r["party_key"], r["amount_key"]) for r in target_rows}
        duplicate_keys = {d["target_key"] for d in diff["duplicate_target_matches"]}
        matched_keys = (source_keys & target_keys) - duplicate_keys

        cutoff = _utcnow() - timedelta(days=PAYMENT_GRACE_DAYS)
        payment_missing_count = 0
        for key in matched_keys:
            inv_id = invoice_id_by_key.get(key)
            inv = invoices_by_id.get(inv_id) if inv_id else None
            invoice_date = _as_aware_utc(inv.invoice_date) if inv else None
            if invoice_date is None or invoice_date > cutoff:
                continue
            if key[0] in payment_party_keys:
                continue
            payment_missing_count += 1
            add_exc(
                TallyExceptionType.PAYMENT_VOUCHER_MISSING, invoice_id=inv_id, party_name=invoice_party_by_key.get(key),
                message=(
                    f"Invoice matched a Tally purchase voucher {PAYMENT_GRACE_DAYS}+ days ago but no Payment voucher "
                    f"exists in Tally for {invoice_party_by_key.get(key) or 'this vendor'}."
                ),
            )

        db.add_all(exceptions)
        await _escalate_exceptions(db, exceptions, invoices_by_id)

        run.source_count = diff["source_count"]
        run.target_count = diff["target_count"]
        run.matched_count = diff["matched_count"]
        run.missing_count = len(diff["missing_keys"])
        run.extra_count = len(diff["extra_keys"])
        run.mismatched_count = len(mismatched_source_keys)
        run.ambiguous_count = len(diff["ambiguous"])
        run.duplicate_target_count = len(diff["duplicate_target_matches"])
        run.ledger_balance_mismatch_count = ledger_mismatch_count
        run.payment_voucher_missing_count = payment_missing_count
        run.status = TallyReconciliationRunStatus.SUCCEEDED
        run.completed_at = _utcnow()
        await db.commit()
        await db.refresh(run)
        return run
    except Exception as exc:  # noqa: BLE001 — persist the failure on the run row rather than losing it to a bare 500
        await db.rollback()
        run = await db.get(TallyReconciliationRun, run.id)
        run.status = TallyReconciliationRunStatus.FAILED
        run.error_message = str(exc)
        run.completed_at = _utcnow()
        await db.commit()
        raise


async def get_run(db: AsyncSession, run_id: int, organization_id: int | None) -> TallyReconciliationRun | None:
    run = await db.get(TallyReconciliationRun, run_id)
    if run is None:
        return None
    if organization_id is not None and run.organization_id != organization_id:
        return None
    return run


async def list_runs(
    db: AsyncSession, organization_id: int | None, connection_id: int, *, page: int = 1, page_size: int = 50,
) -> tuple[list[TallyReconciliationRun], int]:
    filters = [TallyReconciliationRun.connection_id == connection_id]
    if organization_id is not None:
        filters.append(TallyReconciliationRun.organization_id == organization_id)
    total = (await db.execute(select(func.count()).select_from(TallyReconciliationRun).where(*filters))).scalar_one()
    stmt = (
        select(TallyReconciliationRun).where(*filters).order_by(TallyReconciliationRun.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total


async def list_exceptions(
    db: AsyncSession, run_id: int, organization_id: int | None, *, exception_type: TallyExceptionType | None = None,
    page: int = 1, page_size: int = 100,
) -> tuple[list[TallyReconciliationException], int]:
    filters = [TallyReconciliationException.run_id == run_id]
    if organization_id is not None:
        filters.append(TallyReconciliationException.organization_id == organization_id)
    if exception_type is not None:
        filters.append(TallyReconciliationException.exception_type == exception_type)
    total = (await db.execute(select(func.count()).select_from(TallyReconciliationException).where(*filters))).scalar_one()
    stmt = (
        select(TallyReconciliationException).where(*filters).order_by(TallyReconciliationException.id.asc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total
