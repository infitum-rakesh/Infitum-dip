"""
invoice_inbox_service.py — Invoice Inbox query/orchestration layer for
INV8's /api/invoices/* routes.

This module deliberately owns only what's genuinely new: listing/
filtering/stats over ApInvoice, and a "corrections wrapper that
re-validates." It does NOT reimplement upload, OCR, AI extraction,
review-item storage, or field-correction merging — those stay exactly
where they already live (document_service.py, review_queue_service.py)
per this build's reuse-infrastructure mandate. Corrections in particular
route THROUGH review_queue_service's existing update_field()/approve()
so the same correction ends up recorded in the same ReviewAuditLog other
document types already use — not a second, invoice-only audit trail.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.models.document import Document
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceDecision, ApInvoiceLine, ApInvoiceStatus, ApInvoiceVendor
from app.models.invoice_intelligence.rules import ApInvoiceValidationResult
from app.models.review_queue import ReviewAuditLog, ReviewQueueItem, ReviewStatus
from app.models.user import User


class InvoiceInboxError(Exception):
    """Raised for a genuine caller mistake (correcting a field that
    doesn't exist, etc) — never for "the invoice isn't confident enough,"
    which is invoice_decision_service's job, not this layer's."""


# ── Lookup ───────────────────────────────────────────────────────

async def get_invoice(db: AsyncSession, invoice_id: int, organization_id: int | None) -> ApInvoice | None:
    stmt = select(ApInvoice).where(ApInvoice.id == invoice_id)
    if organization_id is not None:
        stmt = stmt.where(ApInvoice.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_invoice_by_document(db: AsyncSession, document_id: int, organization_id: int | None) -> ApInvoice | None:
    stmt = select(ApInvoice).where(ApInvoice.document_id == document_id)
    if organization_id is not None:
        stmt = stmt.where(ApInvoice.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_document(db: AsyncSession, invoice: ApInvoice) -> Document | None:
    return await db.get(Document, invoice.document_id)


# ── List / stats ─────────────────────────────────────────────────

async def list_invoices(
    db: AsyncSession, organization_id: int | None, *,
    q: str = "", status_filter: str | None = None, decision_filter: str | None = None,
    vendor: str | None = None, date_from: str | None = None, date_to: str | None = None,
    page: int = 1, page_size: int = 20, sort_by: str = "created_at", sort_dir: str = "desc",
) -> tuple[list[ApInvoice], int]:
    stmt = select(ApInvoice)
    if organization_id is not None:
        stmt = stmt.where(ApInvoice.organization_id == organization_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(ApInvoice.invoice_number.ilike(like), ApInvoice.vendor_name.ilike(like), ApInvoice.purchase_order.ilike(like)))
    if status_filter:
        try:
            stmt = stmt.where(ApInvoice.status == ApInvoiceStatus(status_filter))
        except ValueError:
            raise InvoiceInboxError(f"Unknown status '{status_filter}'.")
    if decision_filter:
        try:
            stmt = stmt.where(ApInvoice.final_decision == ApInvoiceDecision(decision_filter))
        except ValueError:
            raise InvoiceInboxError(f"Unknown decision '{decision_filter}'.")
    if vendor:
        stmt = stmt.where(ApInvoice.vendor_name.ilike(f"%{vendor}%"))
    if date_from:
        stmt = stmt.where(ApInvoice.invoice_date >= datetime.fromisoformat(date_from))
    if date_to:
        stmt = stmt.where(ApInvoice.invoice_date <= datetime.fromisoformat(date_to))

    count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = count_result.scalar_one()

    sort_col = {
        "created_at": ApInvoice.created_at, "invoice_date": ApInvoice.invoice_date, "due_date": ApInvoice.due_date,
        "total": ApInvoice.total, "vendor_name": ApInvoice.vendor_name, "status": ApInvoice.status,
    }.get(sort_by, ApInvoice.created_at)
    stmt = stmt.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(stmt)
    return list(result.scalars().all()), total


async def get_stats(db: AsyncSession, organization_id: int | None) -> dict:
    """Inbox KPI cards — real counts/sums, grouped by the same
    status/decision enums the detail view uses, not a separate
    ad-hoc bucketing."""
    org_filter = [ApInvoice.organization_id == organization_id] if organization_id is not None else []

    total_result = await db.execute(select(func.count()).select_from(ApInvoice).where(*org_filter))
    total = total_result.scalar_one()

    by_status_result = await db.execute(select(ApInvoice.status, func.count()).where(*org_filter).group_by(ApInvoice.status))
    by_status = {row[0].value: row[1] for row in by_status_result.all()}

    by_decision_result = await db.execute(select(ApInvoice.final_decision, func.count()).where(*org_filter).group_by(ApInvoice.final_decision))
    by_decision = {(row[0].value if row[0] else "PENDING"): row[1] for row in by_decision_result.all()}

    total_amount_result = await db.execute(select(func.coalesce(func.sum(ApInvoice.total), 0.0)).where(*org_filter))
    total_amount = total_amount_result.scalar_one() or 0.0

    duplicate_result = await db.execute(select(func.count()).select_from(ApInvoice).where(*org_filter, ApInvoice.duplicate_check_result == "BLOCKED"))
    duplicate_count = duplicate_result.scalar_one()

    return {
        "total": total,
        "by_status": by_status,
        "by_decision": by_decision,
        "needs_review": by_decision.get(ApInvoiceDecision.REVIEW_REQUIRED.value, 0),
        "straight_through": by_decision.get(ApInvoiceDecision.STRAIGHT_THROUGH.value, 0),
        "rejected": by_decision.get(ApInvoiceDecision.REJECT.value, 0),
        "duplicate_blocked": duplicate_count,
        "total_amount": round(total_amount, 2),
    }


# ── Detail sub-resources ─────────────────────────────────────────

async def get_lines(db: AsyncSession, invoice_id: int) -> list[ApInvoiceLine]:
    result = await db.execute(select(ApInvoiceLine).where(ApInvoiceLine.invoice_id == invoice_id).order_by(ApInvoiceLine.line_number.asc()))
    return list(result.scalars().all())


async def get_validation_results(db: AsyncSession, invoice_id: int) -> list[ApInvoiceValidationResult]:
    result = await db.execute(
        select(ApInvoiceValidationResult).where(ApInvoiceValidationResult.invoice_id == invoice_id).order_by(ApInvoiceValidationResult.created_at.desc())
    )
    return list(result.scalars().all())


async def get_evidence(db: AsyncSession, invoice: ApInvoice) -> dict:
    """Real per-field evidence straight from Document.extraction_result —
    never recomputed/guessed here. bbox is whatever evidence_service
    (INV4) actually attached at extraction time; None means genuinely no
    matching OCR region was found, not that this endpoint failed to look."""
    doc = await _get_document(db, invoice)
    if doc is None or not doc.extraction_result:
        return {"document_id": invoice.document_id, "fields": []}
    fields = doc.extraction_result.get("fields", [])
    scalar_fields = [
        {"name": f.get("name"), "value": f.get("value"), "confidence": f.get("confidence"),
         "page": f.get("source_page") or f.get("page"), "bbox": f.get("bbox"), "missing": f.get("missing", False)}
        for f in fields if f.get("type") != "table"
    ]
    return {"document_id": invoice.document_id, "fields": scalar_fields}


async def get_audit_trail(db: AsyncSession, invoice: ApInvoice) -> list[dict]:
    """Merges ReviewAuditLog across EVERY review item ever opened for this
    invoice's document — reused unmodified (see models/review_queue.py) —
    rather than a second, invoice-only audit table (this build's explicit
    "don't duplicate what Review Queue already tracks" instruction)."""
    item_ids_result = await db.execute(select(ReviewQueueItem.id).where(ReviewQueueItem.document_id == invoice.document_id))
    item_ids = [row[0] for row in item_ids_result.all()]
    if not item_ids:
        return []
    logs_result = await db.execute(select(ReviewAuditLog).where(ReviewAuditLog.review_item_id.in_(item_ids)).order_by(ReviewAuditLog.created_at.asc()))
    logs = logs_result.scalars().all()

    user_ids = {log.user_id for log in logs if log.user_id}
    name_map: dict[int, str] = {}
    if user_ids:
        async with AsyncSessionLocal() as platform_db:  # User is PlatformBase — never the tenant-routed `db`
            names_result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(user_ids)))
            name_map = {row.id: row.full_name for row in names_result.all()}

    return [
        {
            "id": log.id, "action": log.action, "old_value": log.old_value, "new_value": log.new_value,
            "reason": log.reason, "user_id": log.user_id, "user_name": name_map.get(log.user_id) if log.user_id else "System",
            "created_at": log.created_at,
        }
        for log in logs
    ]


async def get_history(db: AsyncSession, invoice: ApInvoice) -> dict:
    """Document.timeline (real per-stage OCR/extraction history, already
    computed by document_service.py) + this invoice's own decision
    reasons — not a separate invoice_processing_runs table (deliberately
    not duplicated, see the Invoice Intelligence plan)."""
    doc = await _get_document(db, invoice)
    return {
        "document_id": invoice.document_id,
        "timeline": (doc.timeline or []) if doc else [],
        "decision_reasons": invoice.decision_reasons or [],
        "final_decision": invoice.final_decision.value if invoice.final_decision else None,
        "status": invoice.status.value,
    }


# ── Re-validate (no re-OCR/re-extraction) ─────────────────────────

async def revalidate(db: AsyncSession, invoice: ApInvoice) -> ApInvoice:
    """Re-runs materialize -> validate -> decide from the document's
    CURRENT extraction_result — no OCR/AI re-run (see api/invoices.py's
    /reprocess route for that, which goes through document_service
    instead). Useful right after a manual data fix, or to pick up a rule
    change without paying for a fresh extraction."""
    doc = await _get_document(db, invoice)
    if doc is None or not doc.extraction_result or not doc.extraction_result.get("fields"):
        raise InvoiceInboxError("This invoice's document has no extraction data to re-validate against.")

    from app.services.invoice import invoice_decision_service, invoice_processing_service, invoice_validation_service
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, invoice.organization_id)
    validation_results = await invoice_validation_service.evaluate_invoice(db, invoice)
    await invoice_decision_service.decide(db, invoice, validation_results)
    return invoice


# ── Corrections (reuses Review Queue, does not reimplement it) ────

async def apply_corrections(db: AsyncSession, invoice: ApInvoice, corrections: list[dict], actor: User) -> ApInvoice:
    """`corrections`: [{"field_name": str, "corrected_value": str, "reason": str|None}].
    Routes entirely through review_queue_service's existing update_field()
    + approve() — the same mechanism every other document type's
    corrections already go through — rather than a second field-merge
    implementation. approve() is what actually re-triggers materialize/
    validate/decide (already wired in INV5/INV7); this function doesn't
    duplicate that call."""
    if not corrections:
        raise InvoiceInboxError("No corrections supplied.")

    doc = await _get_document(db, invoice)
    if doc is None:
        raise InvoiceInboxError("This invoice's source document no longer exists.")

    from app.services import review_queue_service as review_svc

    # create_manual() already returns an existing OPEN item for this
    # document if one exists (see its own "existing = ..." check) rather
    # than opening a duplicate — exactly the "find or open" behavior this
    # needs, with zero private-function reach-in.
    item = await review_svc.create_manual(db, doc, requested_by=actor, reviewer_id=None, note="Opened for invoice field correction")

    known_fields = {f["name"] for f in (doc.extraction_result or {}).get("fields", []) if f.get("type") != "table"}
    for correction in corrections:
        field_name = correction.get("field_name")
        if field_name not in known_fields:
            raise InvoiceInboxError(f"'{field_name}' is not a field on this invoice.")
        item = await review_svc.update_field(db, item, field_name, correction.get("corrected_value"), correction.get("reason"), actor)

        # INV13 — capture the correction for evaluation-metrics use.
        # update_field() just wrote a real "Field Updated" ReviewAuditLog
        # row for this exact item+field; read it back (same session,
        # sequential, single request — safe to take "most recent") rather
        # than recomputing old_value a second time.
        from app.models.review_queue import ReviewAuditLog
        from app.services.invoice import invoice_learning_service
        log_row = (await db.execute(
            select(ReviewAuditLog).where(ReviewAuditLog.review_item_id == item.id, ReviewAuditLog.action == "Field Updated")
            .order_by(ReviewAuditLog.id.desc()).limit(1)
        )).scalar_one_or_none()
        try:
            await invoice_learning_service.capture_correction(
                db, invoice, field_name=field_name, review_audit_log_id=log_row.id if log_row else None,
            )
        except Exception:  # noqa: BLE001 - learning capture must never block a real correction from applying
            pass

    item = await review_svc.approve(db, item, actor)  # merges corrections into doc.extraction_result AND re-runs materialize/validate/decide (INV5/INV7 wiring)

    refreshed = await get_invoice(db, invoice.id, invoice.organization_id)
    return refreshed or invoice
