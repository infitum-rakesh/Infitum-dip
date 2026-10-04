"""
tally_voucher_preview_service.py — TALLY-D orchestration. Loads an
ApInvoice + its lines, resolves the real party ledger against
TallyCanonicalParty (falling back to the raw extracted vendor_name if no
match has been extracted yet — disclosed via the LEDGER_EXISTS check,
never silently substituted), builds the voucher + its Import XML,
deterministically validates it, and upserts one TallyVoucherPreview (+
replaces its TallyVoucherValidationResult rows).

Never sends anything to Tally — see tally_voucher_builder.py's module
docstring. This is the read side of "what would this invoice become";
TALLY-I is where an actual write, gated by TALLY-H's approval, happens.
"""
from __future__ import annotations

import re

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceLine
from app.models.tally_canonical import TallyCanonicalLedger, TallyCanonicalParty, TallyCanonicalVoucher
from app.models.tally_mapping import TallyVoucherPreview, TallyVoucherPreviewStatus, TallyVoucherValidationResult
from app.services import tally_mapping_service
from app.services.tally_voucher_builder import TallyVoucherBuildError, build_voucher, build_voucher_xml
from app.services.tally_voucher_validation_service import overall_status, validate_voucher


class TallyVoucherPreviewError(Exception):
    """Invoice not found / not this org's / has no data to build from —
    a caller error, distinct from a validation FAIL on a successfully
    built voucher."""


def _normalize_name(name: str | None) -> str:
    """Same normalization as tally_canonical_service._normalize_name /
    invoice_processing_service._normalize_name — kept in sync so a
    canonical party and an ApInvoice.vendor_name match on the same key."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


async def resolve_party_ledger_name(db: AsyncSession, connection_id: int, vendor_name: str | None) -> str:
    """Public — also reused by tally_reconciliation_service.py so both the
    preview builder and the reconciler key vouchers/invoices to the same
    resolved party ledger name."""
    normalized = _normalize_name(vendor_name)
    if normalized:
        result = await db.execute(
            select(TallyCanonicalParty.name).where(
                TallyCanonicalParty.connection_id == connection_id, TallyCanonicalParty.normalized_name == normalized,
            )
        )
        matched = result.scalar_one_or_none()
        if matched:
            return matched
    return vendor_name or ""


async def build_preview(
    db: AsyncSession, connection: Connection, *, invoice_id: int, company: str, actor_user_id: int | None = None,
) -> TallyVoucherPreview:
    invoice = await db.get(ApInvoice, invoice_id)
    if invoice is None or invoice.organization_id != connection.organization_id:
        raise TallyVoucherPreviewError(f"Invoice {invoice_id} not found for this organization.")

    lines = list((await db.execute(select(ApInvoiceLine).where(ApInvoiceLine.invoice_id == invoice.id))).scalars().all())

    mapping_rows = await tally_mapping_service.get_current_mapping(db, connection.organization_id, connection.id)
    if not mapping_rows:
        mapping_rows = await tally_mapping_service.seed_default_mapping(db, connection.organization_id, connection.id)

    party_ledger_name = await resolve_party_ledger_name(db, connection.id, invoice.vendor_name)

    try:
        voucher = build_voucher(invoice, lines, mapping_rows, party_ledger_name=party_ledger_name)
    except TallyVoucherBuildError as exc:
        raise TallyVoucherPreviewError(str(exc)) from exc
    voucher_xml = build_voucher_xml(voucher, company=company)

    known_parties = set(
        (await db.execute(select(TallyCanonicalParty.name).where(TallyCanonicalParty.connection_id == connection.id))).scalars().all()
    )
    known_ledgers = known_parties | set(
        (await db.execute(select(TallyCanonicalLedger.name).where(TallyCanonicalLedger.connection_id == connection.id))).scalars().all()
    )

    possible_duplicate = False
    if invoice.total is not None:
        dup = await db.execute(
            select(TallyCanonicalVoucher.id).where(
                TallyCanonicalVoucher.connection_id == connection.id,
                TallyCanonicalVoucher.party_ledger_name == party_ledger_name,
                TallyCanonicalVoucher.total_amount.is_not(None),
                func.abs(TallyCanonicalVoucher.total_amount - invoice.total) < 0.01,
            ).limit(1)
        )
        possible_duplicate = dup.scalar_one_or_none() is not None

    results = validate_voucher(
        voucher, invoice, known_party_names=known_parties, known_ledger_names=known_ledgers, possible_duplicate=possible_duplicate,
    )
    status = overall_status(results)

    existing = await db.execute(
        select(TallyVoucherPreview).where(TallyVoucherPreview.connection_id == connection.id, TallyVoucherPreview.invoice_id == invoice.id)
    )
    preview = existing.scalar_one_or_none()
    if preview is None:
        preview = TallyVoucherPreview(organization_id=connection.organization_id, connection_id=connection.id, invoice_id=invoice.id)
        db.add(preview)

    preview.company_name = company
    preview.mapping_version = mapping_rows[0].version if mapping_rows else None
    preview.voucher_type = voucher["voucher_type"]
    preview.voucher_date = voucher["voucher_date"].date() if hasattr(voucher["voucher_date"], "date") else voucher["voucher_date"]
    preview.party_ledger_name = voucher["party_ledger_name"]
    preview.narration = voucher["narration"]
    preview.ledger_entries = voucher["ledger_entries"]
    preview.voucher_xml = voucher_xml
    preview.idempotency_key = f"ap-invoice-{invoice.id}-tally-{connection.id}"
    preview.status = TallyVoucherPreviewStatus(status)
    preview.created_by = actor_user_id
    await db.flush()

    await db.execute(delete(TallyVoucherValidationResult).where(TallyVoucherValidationResult.preview_id == preview.id))
    for r in results:
        db.add(TallyVoucherValidationResult(
            organization_id=connection.organization_id, preview_id=preview.id,
            category=r["category"], result=r["result"], message=r["message"], details=r.get("details", {}),
        ))

    await db.commit()
    await db.refresh(preview)
    return preview


async def get_preview(db: AsyncSession, preview_id: int, organization_id: int | None) -> TallyVoucherPreview | None:
    preview = await db.get(TallyVoucherPreview, preview_id)
    if preview is None:
        return None
    if organization_id is not None and preview.organization_id != organization_id:
        return None
    return preview


async def list_validation_results(db: AsyncSession, preview_id: int) -> list[TallyVoucherValidationResult]:
    result = await db.execute(select(TallyVoucherValidationResult).where(TallyVoucherValidationResult.preview_id == preview_id).order_by(TallyVoucherValidationResult.id))
    return list(result.scalars().all())


async def list_previews(
    db: AsyncSession, connection_id: int, organization_id: int | None, *, page: int = 1, page_size: int = 50,
) -> tuple[list[TallyVoucherPreview], int]:
    filters = [TallyVoucherPreview.connection_id == connection_id]
    if organization_id is not None:
        filters.append(TallyVoucherPreview.organization_id == organization_id)
    total = (await db.execute(select(func.count()).select_from(TallyVoucherPreview).where(*filters))).scalar_one()
    stmt = (
        select(TallyVoucherPreview).where(*filters).order_by(TallyVoucherPreview.updated_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total
