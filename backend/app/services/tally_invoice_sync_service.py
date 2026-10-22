"""
tally_invoice_sync_service.py — automatic Invoice Intelligence -> Tally
bridge.

Before this module, nothing connected the two: Invoice Intelligence
(invoice_decision_service.py) never referenced Tally at all, and the only
way a TallyVoucherPreview ever got built was a human manually opening the
Voucher Preview page, picking a connection, typing a company name, and
clicking through Preview -> Submit for Approval -> Write Back to Tally
(see tally_voucher_preview_service.py / tally_approval_service.py /
tally_writeback_service.py's own docstrings — none of them resolve a
connection/company on their own, all three require the caller to already
know which one to use).

This module supplies the missing piece — "which Tally company does THIS
invoice belong to" (TallyVendorMapping, resolve_connection_for_invoice())
— and the trigger (sync_invoice_to_tally(), called from document_service.
py right after invoice_decision_service.decide() resolves to
STRAIGHT_THROUGH — the exact same gate INV11 already uses to auto-post an
invoice to a generic ERP connection, not a new pattern). Once resolved,
it runs the identical build_preview() -> submit_for_approval() steps the
manual UI flow already runs; if this org has never configured a
TALLY_VOUCHER-scoped ApprovalRule, submit_for_approval() auto-approves
exactly as it already does for a manual submission (see that module's
own docstring), and this service then also fires post_voucher() — the
one step the manual flow still requires a separate explicit button click
for, automated here because an unattended pipeline has no one to click it.

Two independent, both-default-OFF gates make this fully inert until an
org opts in to both:
  1. OrganizationSettings.document_intelligence.tally_auto_sync_enabled
  2. At least one active TallyVendorMapping row for this invoice's vendor
     (or an org-wide default row, vendor_name IS NULL) — no row configured
     is always a silent no-op, regardless of the flag.

Every failure mode — a bug here, Tally unreachable, a missing/blocked
preview — resolves to a returned outcome dict, never an exception leaking
to the caller; see sync_invoice_to_tally()'s own try/except and the
identical belt-and-suspenders wrapper document_service.py already puts
around its own call site (same "a Tally problem must never fail the
invoice's own processing" contract INV11's ERP auto-post already honors).
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection, ConnectionType
from app.models.invoice_intelligence.core import ApInvoice
from app.models.tally_mapping import TallyVendorMapping, TallyVoucherApprovalStatus, TallyVoucherPreviewStatus
from app.services import settings_service as settings_svc
from app.services import tally_approval_service, tally_voucher_preview_service
from app.services.tally_voucher_preview_service import TallyVoucherPreviewError


class TallyVendorMappingError(Exception):
    """Caller error — bad connection id / wrong org / wrong connection type."""


def _normalize_vendor(name: str | None) -> str:
    """Same normalization as tally_voucher_preview_service._normalize_name
    — kept in sync so a vendor mapping matches ApInvoice.vendor_name the
    same way a canonical Tally party already does."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


# ── Vendor -> Tally company mapping CRUD ────────────────────────────────

async def list_vendor_mappings(db: AsyncSession, organization_id: int) -> list[TallyVendorMapping]:
    result = await db.execute(
        select(TallyVendorMapping)
        .where(TallyVendorMapping.organization_id == organization_id)
        .order_by(TallyVendorMapping.vendor_name.is_(None).desc(), TallyVendorMapping.vendor_name)
    )
    return list(result.scalars().all())


async def set_vendor_mapping(
    db: AsyncSession, *, organization_id: int, vendor_name: str | None, connection_id: int,
    company_name: str, actor_user_id: int | None = None,
) -> TallyVendorMapping:
    """Upsert — one row per (organization, vendor_name). vendor_name=None
    (or blank) sets/replaces this org's fallback/default routing, used
    when no vendor-specific row matches an invoice."""
    connection = await db.get(Connection, connection_id)
    if connection is None or connection.organization_id != organization_id:
        raise TallyVendorMappingError(f"Connection {connection_id} was not found in this organization.")
    if connection.connection_type != ConnectionType.TALLY_PRIME:
        raise TallyVendorMappingError(f"Connection {connection_id} is not a TallyPrime connection.")
    if not company_name or not company_name.strip():
        raise TallyVendorMappingError("A company name is required.")

    normalized_key = vendor_name.strip() if vendor_name and vendor_name.strip() else None

    existing = await db.execute(
        select(TallyVendorMapping).where(
            TallyVendorMapping.organization_id == organization_id,
            TallyVendorMapping.vendor_name == normalized_key,
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = TallyVendorMapping(organization_id=organization_id, vendor_name=normalized_key, created_by=actor_user_id)
        db.add(row)
    row.connection_id = connection_id
    row.company_name = company_name.strip()
    row.is_active = True
    await db.commit()
    await db.refresh(row)
    return row


async def clear_vendor_mapping(db: AsyncSession, *, organization_id: int, mapping_id: int) -> bool:
    row = await db.get(TallyVendorMapping, mapping_id)
    if row is None or row.organization_id != organization_id:
        return False
    await db.delete(row)
    await db.commit()
    return True


# ── Resolution ───────────────────────────────────────────────────────────

async def resolve_connection_for_invoice(db: AsyncSession, invoice: ApInvoice) -> tuple[Connection, str] | None:
    """Vendor-specific row wins; falls back to this org's default
    (vendor_name IS NULL) row; returns None if neither exists. Never
    raises — the caller's contract is to treat None as a clean no-op."""
    if invoice.organization_id is None:
        return None
    normalized = _normalize_vendor(invoice.vendor_name)

    mapping = None
    if normalized:
        rows = (await db.execute(
            select(TallyVendorMapping).where(
                TallyVendorMapping.organization_id == invoice.organization_id,
                TallyVendorMapping.is_active.is_(True),
                TallyVendorMapping.vendor_name.is_not(None),
            )
        )).scalars().all()
        for row in rows:
            if _normalize_vendor(row.vendor_name) == normalized:
                mapping = row
                break

    if mapping is None:
        result = await db.execute(
            select(TallyVendorMapping).where(
                TallyVendorMapping.organization_id == invoice.organization_id,
                TallyVendorMapping.vendor_name.is_(None),
                TallyVendorMapping.is_active.is_(True),
            )
        )
        mapping = result.scalar_one_or_none()

    if mapping is None:
        return None

    connection = await db.get(Connection, mapping.connection_id)
    if connection is None or connection.organization_id != invoice.organization_id:
        return None
    return connection, mapping.company_name


# ── The automatic trigger ────────────────────────────────────────────────

async def sync_invoice_to_tally(db: AsyncSession, invoice: ApInvoice) -> dict:
    """Builds a voucher preview, submits it for approval, and — only once
    actually APPROVED (whether by a real human or by this org's own
    AUTO_APPROVED-when-unconfigured default, identical in spirit to how
    Payment Approval already treats an org with no rules) — writes it
    back to Tally. Returns an outcome dict for logging/counting; never
    raises, so a bug or an unreachable Tally gateway here can never fail
    the invoice's own decisioning."""
    try:
        if invoice.organization_id is None:
            return {"status": "skipped", "reason": "no_organization"}

        org_settings = await settings_svc.get_or_create(db, invoice.organization_id)
        di_settings = settings_svc.effective_document_intelligence_settings(org_settings)
        if not di_settings.get("tally_auto_sync_enabled"):
            return {"status": "skipped", "reason": "flag_off"}

        resolved = await resolve_connection_for_invoice(db, invoice)
        if resolved is None:
            return {"status": "skipped", "reason": "no_mapping"}
        connection, company_name = resolved

        try:
            preview = await tally_voucher_preview_service.build_preview(
                db, connection, invoice_id=invoice.id, company=company_name,
            )
        except TallyVoucherPreviewError as exc:
            return {"status": "error", "stage": "build_preview", "error": str(exc)}

        if preview.status != TallyVoucherPreviewStatus.READY:
            return {"status": "blocked", "preview_id": preview.id}

        # Already approved from an earlier run (build_preview() upserts
        # in place, so a reprocessed/corrected invoice can land here
        # again) — skip straight to write-back without resubmitting.
        if preview.approval_status != TallyVoucherApprovalStatus.APPROVED:
            try:
                preview, _workflow = await tally_approval_service.submit_for_approval(
                    db, preview_id=preview.id, organization_id=invoice.organization_id, actor_user_id=None,
                )
            except tally_approval_service.TallyApprovalError as exc:
                return {"status": "error", "stage": "submit_for_approval", "error": str(exc)}

        if preview.approval_status != TallyVoucherApprovalStatus.APPROVED:
            return {"status": "pending_approval", "preview_id": preview.id}

        from app.services import tally_writeback_service  # lazy — mirrors this file's other lazy service imports, avoids a module-load-time cycle
        try:
            txn = await tally_writeback_service.post_voucher(db, preview, actor_user_id=None)
            return {"status": "posted", "preview_id": preview.id, "transaction_id": txn.id}
        except tally_writeback_service.TallyWriteBackError as exc:
            # Already written / already in flight both land here — a
            # harmless, expected outcome on a re-sync, not a real failure.
            return {"status": "write_back_skipped", "preview_id": preview.id, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - must never fail invoice/document processing; see module docstring
        return {"status": "error", "stage": "unexpected", "error": str(exc)[:500]}
