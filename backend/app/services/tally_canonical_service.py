"""
tally_canonical_service.py — TALLY-C materialization.

Turns one TallyExtractionRun's raw TallyExtractionRecord rows
(TALLY-B's pre-canonical snapshot) into real TallyCanonicalVoucher/
Party/Ledger rows, upserted by (connection_id, guid) — Tally's own
GUID is the real identity; ALTERID is Tally's own change-counter, so a
record whose ALTERID hasn't moved since the last sync is a cheap no-op
rather than a rewrite.

Called automatically at the end of tally_extraction_service.
start_extraction() (best-effort — a canonicalization bug must never
break the raw extraction, which remains authoritative regardless), and
exposed standalone via re_materialize_run() for backfilling runs that
predate this service or for reprocessing after a bug fix here.

Records with no GUID (BILL_ALLOCATIONS/OUTSTANDING — see
models/tally_canonical.py's module docstring for why) are counted as
skipped, never silently dropped without being accounted for.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tally_canonical import TallyCanonicalLedger, TallyCanonicalParty, TallyCanonicalVoucher, TallyVoucherDirection
from app.models.tally_extraction import TallyExtractionRecord, TallyExtractionRun, TallyExtractionType

_VOUCHER_EXTRACTION_TYPES = {
    TallyExtractionType.VOUCHERS,
    TallyExtractionType.PURCHASE_VOUCHERS,
    TallyExtractionType.SALES_VOUCHERS,
    TallyExtractionType.PAYMENTS,
}


def _normalize_name(name: str | None) -> str:
    """Mirrors invoice_intelligence's ApInvoiceVendor.normalized_name
    convention exactly (see invoice_processing_service._normalize_name)
    so TALLY-D/E can fuzzy-match a canonical party against an
    ApInvoice.vendor_name using the same normalization on both sides."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _parse_amount(raw) -> float | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = re.sub(r"[^0-9.\-]", "", str(raw).replace(",", ""))
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _parse_date_str(raw):
    from datetime import date
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if len(s) == 8 and s.isdigit():
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return date.fromisoformat(s)
    except ValueError:
        return None
    return None


def _voucher_total(item: dict) -> float | None:
    """The transaction's real value from the AP/AR side: the ledger
    entry matching the voucher's own party_ledger_name, absolute value.
    None (never guessed) if that entry isn't present in the raw data."""
    party = item.get("party_ledger_name")
    for entry in item.get("ledger_entries") or []:
        if isinstance(entry, dict) and entry.get("ledger_name") == party:
            amt = _parse_amount(entry.get("amount"))
            return abs(amt) if amt is not None else None
    return None


async def _upsert_voucher(db: AsyncSession, run: TallyExtractionRun, record: TallyExtractionRecord, item: dict) -> bool:
    guid = item.get("guid")
    if not guid:
        return False
    result = await db.execute(
        select(TallyCanonicalVoucher).where(TallyCanonicalVoucher.connection_id == run.connection_id, TallyCanonicalVoucher.guid == guid)
    )
    voucher = result.scalar_one_or_none()
    alter_id = _parse_int(item.get("alter_id"))
    if voucher is not None and voucher.alter_id is not None and alter_id is not None and voucher.alter_id == alter_id:
        return False  # Tally's own change-counter hasn't moved — cheap no-op, not a rewrite

    if voucher is None:
        voucher = TallyCanonicalVoucher(
            organization_id=run.organization_id, connection_id=run.connection_id,
            company_name=run.company_name, guid=guid,
        )
        db.add(voucher)

    voucher.source_record_id = record.id
    voucher.alter_id = alter_id
    voucher.voucher_number = item.get("voucher_number")
    voucher.voucher_type = item.get("voucher_type")
    voucher.voucher_date = _parse_date_str(item.get("date"))
    voucher.party_ledger_name = item.get("party_ledger_name")
    voucher.narration = item.get("narration")
    direction = item.get("direction")
    voucher.direction = TallyVoucherDirection(direction) if direction in ("PAYMENT", "RECEIPT") else None
    voucher.total_amount = _voucher_total(item)
    voucher.ledger_entries = item.get("ledger_entries") or []
    return True


async def _upsert_party(db: AsyncSession, run: TallyExtractionRun, record: TallyExtractionRecord, item: dict) -> bool:
    guid = item.get("guid")
    if not guid:
        return False
    result = await db.execute(
        select(TallyCanonicalParty).where(TallyCanonicalParty.connection_id == run.connection_id, TallyCanonicalParty.guid == guid)
    )
    party = result.scalar_one_or_none()
    alter_id = _parse_int(item.get("alter_id"))
    if party is not None and party.alter_id is not None and alter_id is not None and party.alter_id == alter_id:
        return False

    if party is None:
        party = TallyCanonicalParty(
            organization_id=run.organization_id, connection_id=run.connection_id,
            company_name=run.company_name, guid=guid,
        )
        db.add(party)

    party.source_record_id = record.id
    party.alter_id = alter_id
    party.name = item.get("name") or party.name or guid
    party.normalized_name = _normalize_name(party.name)
    party.parent = item.get("parent")
    party.party_type = item.get("party_type")
    party.gstin = item.get("gstin")
    party.state = item.get("state")
    party.address_lines = item.get("address_lines") or []
    party.email = item.get("email")
    party.mobile = item.get("mobile")
    party.opening_balance = _parse_amount(item.get("opening_balance"))
    party.closing_balance = _parse_amount(item.get("closing_balance"))
    return True


async def _upsert_ledger(db: AsyncSession, run: TallyExtractionRun, record: TallyExtractionRecord, item: dict) -> bool:
    guid = item.get("guid")
    if not guid:
        return False
    result = await db.execute(
        select(TallyCanonicalLedger).where(TallyCanonicalLedger.connection_id == run.connection_id, TallyCanonicalLedger.guid == guid)
    )
    ledger = result.scalar_one_or_none()
    alter_id = _parse_int(item.get("alter_id"))
    if ledger is not None and ledger.alter_id is not None and alter_id is not None and ledger.alter_id == alter_id:
        return False

    if ledger is None:
        ledger = TallyCanonicalLedger(
            organization_id=run.organization_id, connection_id=run.connection_id,
            company_name=run.company_name, guid=guid,
        )
        db.add(ledger)

    ledger.source_record_id = record.id
    ledger.alter_id = alter_id
    ledger.name = item.get("name") or ledger.name or guid
    ledger.parent = item.get("parent")
    ledger.opening_balance = _parse_amount(item.get("opening_balance"))
    ledger.closing_balance = _parse_amount(item.get("closing_balance"))
    return True


async def materialize_from_run(db: AsyncSession, run: TallyExtractionRun) -> dict:
    """Reads every TallyExtractionRecord for this run and upserts the
    corresponding canonical row(s). Returns counts — never silently
    drops a record without being reflected in `skipped_no_identity`."""
    counts = {"vouchers": 0, "parties": 0, "ledgers": 0, "skipped_no_identity": 0}
    records = (await db.execute(
        select(TallyExtractionRecord).where(TallyExtractionRecord.run_id == run.id)
    )).scalars().all()

    for record in records:
        item = record.raw_data or {}
        if run.extraction_type in _VOUCHER_EXTRACTION_TYPES:
            if await _upsert_voucher(db, run, record, item):
                counts["vouchers"] += 1
            elif not item.get("guid"):
                counts["skipped_no_identity"] += 1
        elif run.extraction_type == TallyExtractionType.PARTY_MASTER:
            if await _upsert_party(db, run, record, item):
                counts["parties"] += 1
            elif not item.get("guid"):
                counts["skipped_no_identity"] += 1
        elif run.extraction_type == TallyExtractionType.LEDGER:
            if item.get("kind") == "ledger":
                if await _upsert_ledger(db, run, record, item):
                    counts["ledgers"] += 1
                elif not item.get("guid"):
                    counts["skipped_no_identity"] += 1
            else:
                if await _upsert_voucher(db, run, record, item):
                    counts["vouchers"] += 1
                elif not item.get("guid"):
                    counts["skipped_no_identity"] += 1
        else:
            # BILL_ALLOCATIONS / OUTSTANDING — no GUID in Tally's own TDL
            # export for a bill allocation; see models/tally_canonical.py.
            counts["skipped_no_identity"] += 1

    await db.commit()
    return counts


async def re_materialize_run(db: AsyncSession, run_id: int, organization_id: int | None) -> dict | None:
    """Standalone entry point for backfilling a run materialized before
    this service existed, or reprocessing after a bug fix here."""
    run = await db.get(TallyExtractionRun, run_id)
    if run is None:
        return None
    if organization_id is not None and run.organization_id != organization_id:
        return None
    return await materialize_from_run(db, run)


async def list_vouchers(
    db: AsyncSession, connection_id: int, organization_id: int | None, *, page: int = 1, page_size: int = 50,
) -> tuple[list[TallyCanonicalVoucher], int]:
    filters = [TallyCanonicalVoucher.connection_id == connection_id]
    if organization_id is not None:
        filters.append(TallyCanonicalVoucher.organization_id == organization_id)
    total = (await db.execute(select(func.count()).select_from(TallyCanonicalVoucher).where(*filters))).scalar_one()
    stmt = (
        select(TallyCanonicalVoucher).where(*filters).order_by(TallyCanonicalVoucher.voucher_date.desc().nullslast(), TallyCanonicalVoucher.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total


async def list_parties(
    db: AsyncSession, connection_id: int, organization_id: int | None, *, page: int = 1, page_size: int = 50,
) -> tuple[list[TallyCanonicalParty], int]:
    filters = [TallyCanonicalParty.connection_id == connection_id]
    if organization_id is not None:
        filters.append(TallyCanonicalParty.organization_id == organization_id)
    total = (await db.execute(select(func.count()).select_from(TallyCanonicalParty).where(*filters))).scalar_one()
    stmt = (
        select(TallyCanonicalParty).where(*filters).order_by(TallyCanonicalParty.name)
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total


async def list_ledgers(
    db: AsyncSession, connection_id: int, organization_id: int | None, *, page: int = 1, page_size: int = 50,
) -> tuple[list[TallyCanonicalLedger], int]:
    filters = [TallyCanonicalLedger.connection_id == connection_id]
    if organization_id is not None:
        filters.append(TallyCanonicalLedger.organization_id == organization_id)
    total = (await db.execute(select(func.count()).select_from(TallyCanonicalLedger).where(*filters))).scalar_one()
    stmt = (
        select(TallyCanonicalLedger).where(*filters).order_by(TallyCanonicalLedger.name)
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total
