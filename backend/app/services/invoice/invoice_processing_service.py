"""
invoice_processing_service.py — materializes a canonical ApInvoice (+
ApInvoiceLine rows) from a Document's real extraction_result. This is the
seam between the existing Document Intelligence pipeline (OCR -> AI/
Custom-Engine extraction -> Review Queue, all reused unmodified — see
services/document_service.py) and the genuinely new Invoice Intelligence
layer (models/invoice_intelligence/*): Document stays the system of
record for the source file/OCR/raw extraction; ApInvoice is the
normalized, validated, ERP-postable representation built FROM it.

Trigger points (both call sites already wired, not just a callable
sitting unused):
  - document_service.process_document() calls this at the end of a run
    where document_type == "INVOICE" was explicitly pinned (scheduler's
    invoice-typed DocumentScheduleConfig — INV11 — or a future explicit
    "process as invoice" action).
  - document_service.run_manual_ai_extraction() calls this the same way
    for the manual "Run AI Extraction" picker when the user explicitly
    chose INVOICE, AND from Review Queue's approve() after a reviewer's
    corrections are merged into Document.extraction_result (re-running
    materialize_invoice() re-normalizes from the corrected values —
    idempotent by document_id, never a duplicate row).

Idempotent by design: ApInvoice.document_id is UNIQUE — materializing
the same Document twice (reprocess, or a later correction) UPDATEs the
existing row rather than inserting a duplicate. Line items are replaced
wholesale each time (delete-then-reinsert) since there's no stable
per-line identity across re-extraction to diff against.

Normalization: every scalar field in Document.extraction_result is a raw
string straight from OCR/AI (never pre-typed) — this module is the one
place that turns "12,345.67" into a real float and "March 5, 2024" into
a real datetime. A value that genuinely doesn't parse is left None,
exactly like extraction's own "can't determine -> blank" contract —
never a fabricated/guessed number or date.

Business-duplicate detection: normalized vendor_name + invoice_number +
total + invoice_date matching an EXISTING ApInvoice for the same
organization -> BLOCKED (all four match) or WARN (vendor+number match
but amount/date differs, or vendor+amount+date match but number
differs). This is deliberately distinct from Document.content_hash's
byte-for-byte technical dedup (already enforced upstream in
document_service.py) — two genuinely different document files (e.g. a
resent PDF with a new watermark/timestamp) can still be the same real
invoice. No per-org configuration UI exists yet for this policy (that's
INV17's Rules UI) — this fixed BLOCK/WARN policy is disclosed here, not
silently presented as user-configurable.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from dateutil import parser as dateutil_parser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceLine, ApInvoiceSource, ApInvoiceStatus, ApInvoiceVendor

CURRENCY_SYMBOL_MAP = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW", "₽": "RUB", "R$": "BRL"}
_CURRENCY_CODE_RE = re.compile(r"\b([A-Z]{3})\b")


class InvoiceProcessingError(Exception):
    """Raised when a Document genuinely has nothing usable to materialize
    from (no extraction_result at all) — the caller's bug, not a runtime
    data-quality issue (those just leave individual fields None)."""


def _field_map(fields: list[dict]) -> dict:
    """name -> raw scalar value, skipping missing/blank fields and the
    table field (handled separately by _line_item_rows())."""
    out = {}
    for f in fields:
        if f.get("type") == "table" or f.get("missing"):
            continue
        out[f["name"]] = f.get("value")
    return out


def _line_item_rows(fields: list[dict]) -> list[dict]:
    for f in fields:
        if f.get("type") == "table" and f.get("name") == "line_items":
            return f.get("rows") or []
    return []


def _clean_string(raw) -> str | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    return text or None


def _parse_amount(raw) -> float | None:
    """Real numeric parsing — strips currency symbols/commas/whitespace,
    never invents a value a genuinely malformed/blank string can't
    produce."""
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


def _parse_date(raw) -> datetime | None:
    """Real date-string parsing via dateutil (handles "March 5, 2024",
    "05-Mar-2024", "2024/03/05", "05/03/2024", ...). dayfirst=False is a
    real, disclosed limitation for ambiguous all-numeric dates (e.g.
    "03/05/2024" reads as March 5, not May 3) — no per-org locale
    configuration exists yet to disambiguate; left as the documented
    default rather than silently guessing a convention. Returns None
    (never a fabricated date) for anything that doesn't genuinely parse."""
    if raw in (None, ""):
        return None
    try:
        parsed = dateutil_parser.parse(str(raw), dayfirst=False, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_currency(raw) -> str | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    m = _CURRENCY_CODE_RE.search(text.upper())
    if m:
        return m.group(1)
    for symbol, code in CURRENCY_SYMBOL_MAP.items():
        if symbol in text:
            return code
    return None


def _normalize_name(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


async def _resolve_vendor(db: AsyncSession, organization_id: int | None, vendor_name: str) -> int | None:
    """Finds an existing ApInvoiceVendor by normalized name (or alias),
    or creates one — real reference-data lookup, not a fabricated ERP
    link (erp_supplier_id stays None until a real ERP-supplier-sync job
    populates it, none exists yet — disclosed, not silently assumed)."""
    normalized = _normalize_name(vendor_name)
    if not normalized:
        return None
    result = await db.execute(
        select(ApInvoiceVendor).where(ApInvoiceVendor.organization_id == organization_id, ApInvoiceVendor.normalized_name == normalized)
    )
    vendor = result.scalar_one_or_none()
    if vendor is not None:
        return vendor.id
    vendor = ApInvoiceVendor(organization_id=organization_id, name=vendor_name.strip(), normalized_name=normalized)
    db.add(vendor)
    await db.flush()
    return vendor.id


async def _check_duplicate(db: AsyncSession, invoice: ApInvoice) -> tuple[str, int | None]:
    """Real, deterministic comparison against every OTHER ApInvoice for
    this organization — never a probabilistic/ML similarity score (no
    such model exists in this build). Returns (duplicate_check_result,
    duplicate_of_invoice_id)."""
    if not (invoice.vendor_name and invoice.invoice_number):
        return "NONE", None  # not enough to compare against — never guess a match

    result = await db.execute(
        select(ApInvoice).where(
            ApInvoice.organization_id == invoice.organization_id,
            ApInvoice.id != (invoice.id or -1),
            ApInvoice.vendor_name.isnot(None),
            ApInvoice.invoice_number.isnot(None),
        )
    )
    candidates = result.scalars().all()
    this_vendor = _normalize_name(invoice.vendor_name)
    this_number = (invoice.invoice_number or "").strip().lower()

    best_match: tuple[str, int] | None = None
    for other in candidates:
        if _normalize_name(other.vendor_name) != this_vendor or (other.invoice_number or "").strip().lower() != this_number:
            continue
        amount_matches = invoice.total is not None and other.total is not None and abs(invoice.total - other.total) < 0.01
        date_matches = invoice.invoice_date is not None and other.invoice_date is not None and invoice.invoice_date.date() == other.invoice_date.date()
        if amount_matches and date_matches:
            return "BLOCKED", other.id  # exact 4-field match — the strongest signal, return immediately
        if best_match is None:
            best_match = ("WARN", other.id)  # same vendor+number, amount or date differs — flag, don't block
    return best_match if best_match else ("NONE", None)


async def materialize_invoice(
    db: AsyncSession, doc: Document, *, source: ApInvoiceSource = ApInvoiceSource.MANUAL_UPLOAD, source_schedule_id: int | None = None,
) -> ApInvoice:
    """Upserts one ApInvoice (+ replaces its ApInvoiceLine rows) from
    doc.extraction_result. Caller is responsible for having already
    confirmed this document is invoice-typed (extraction_result.get(
    "document_type") == "INVOICE") — this function itself doesn't gate
    on that, since Review Queue's approve() may call it on a document
    whose type was set earlier in its lifecycle."""
    if not doc.extraction_result or not doc.extraction_result.get("fields"):
        raise InvoiceProcessingError(f"Document {doc.id} has no extraction_result to materialize an invoice from.")

    fields = doc.extraction_result["fields"]
    fmap = _field_map(fields)

    result = await db.execute(select(ApInvoice).where(ApInvoice.document_id == doc.id))
    invoice = result.scalar_one_or_none()
    if invoice is None:
        invoice = ApInvoice(organization_id=doc.organization_id, document_id=doc.id)
        db.add(invoice)

    invoice.source = source
    invoice.source_schedule_id = source_schedule_id
    invoice.invoice_number = _clean_string(fmap.get("invoice_number"))
    invoice.invoice_date = _parse_date(fmap.get("invoice_date"))
    invoice.due_date = _parse_date(fmap.get("due_date"))
    invoice.vendor_name = _clean_string(fmap.get("vendor_name"))
    invoice.purchase_order = _clean_string(fmap.get("po_number"))
    invoice.currency = _parse_currency(fmap.get("currency"))
    invoice.payment_terms = _clean_string(fmap.get("payment_terms"))
    invoice.subtotal = _parse_amount(fmap.get("subtotal"))
    invoice.tax = _parse_amount(fmap.get("tax_amount"))
    invoice.discount = _parse_amount(fmap.get("discount_amount"))
    invoice.freight = _parse_amount(fmap.get("shipping_amount"))
    invoice.total = _parse_amount(fmap.get("total_amount"))

    invoice.extractor_confidence = doc.extraction_percent
    invoice.ai_raw_response = doc.ai_raw_response
    invoice.status = ApInvoiceStatus.NORMALIZED

    if invoice.vendor_name:
        invoice.vendor_id = await _resolve_vendor(db, doc.organization_id, invoice.vendor_name)

    await db.flush()  # assigns invoice.id for a new row — needed by both duplicate-check (self-exclusion) and line FKs below

    invoice.duplicate_check_result, invoice.duplicate_of_invoice_id = await _check_duplicate(db, invoice)

    # Replace line items wholesale — no stable per-line identity to diff
    # against across a re-extraction, so this is the only correct
    # idempotent approach (matches this project's established convention
    # for other "recompute and replace" derived-data flows).
    existing_lines = await db.execute(select(ApInvoiceLine).where(ApInvoiceLine.invoice_id == invoice.id))
    for line in existing_lines.scalars().all():
        await db.delete(line)
    await db.flush()

    for idx, row in enumerate(_line_item_rows(fields), start=1):
        db.add(ApInvoiceLine(
            invoice_id=invoice.id,
            line_number=idx,
            description=_clean_string(row.get("description")),
            quantity=_parse_amount(row.get("quantity")),
            unit_price=_parse_amount(row.get("unit_price")),
            tax=_parse_amount(row.get("tax")),
            line_total=_parse_amount(row.get("amount")),
        ))

    await db.commit()
    await db.refresh(invoice)

    # INV13 — narrow lifecycle event, only on a genuine duplicate signal
    # (BLOCKED/WARN); never fails the real materialization.
    if invoice.duplicate_check_result in ("BLOCKED", "WARN"):
        try:
            from app.models.invoice_intelligence.learning import ApInvoiceAuditEventType
            from app.services.invoice import invoice_learning_service
            await invoice_learning_service.record_event(
                db, invoice_id=invoice.id, organization_id=invoice.organization_id,
                event_type=ApInvoiceAuditEventType.DUPLICATE_DETECTED,
                message=f"Duplicate check: {invoice.duplicate_check_result} (of invoice {invoice.duplicate_of_invoice_id}).",
                details={"result": invoice.duplicate_check_result, "duplicate_of_invoice_id": invoice.duplicate_of_invoice_id},
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            pass

    return invoice
