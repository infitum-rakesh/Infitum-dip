"""
invoice_learning_service.py — correction capture + narrow lifecycle-event
logging for Invoice Intelligence (INV13).

DISCLOSED LIMITATION (same as models/invoice_intelligence/learning.py's
own docstring): this codebase has no ML training infrastructure — no GPU
workers, no model registry, no training pipeline. What this module
actually does is honest and real: it captures every reviewer field
correction into ApInvoiceLearningRecord (re-indexed by field/vendor for
evaluation queries ReviewAuditLog's generic shape can't answer
efficiently), and lets a human curator mark a subset "approved for
learning" — Technical Design §7's first five steps (capture -> associate
-> approve -> evaluation set -> [stop here]). Nothing here trains or
updates a model. invoice_evaluation_service.py is what actually consumes
this data, as read-only metrics — never a feedback loop back into
extraction.

Correction capture is called from invoice_inbox_service.apply_corrections()
right after each review_queue_service.update_field() call, reusing the
audit row that already-existing function just wrote (see that call site)
rather than recomputing old_value a second time.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice_intelligence.core import ApInvoice
from app.models.invoice_intelligence.learning import ApInvoiceAuditEvent, ApInvoiceAuditEventType, ApInvoiceLearningRecord
from app.models.review_queue import ReviewAuditLog


class InvoiceLearningError(Exception):
    pass


# ── Correction capture ──────────────────────────────────────────────

async def capture_correction(
    db: AsyncSession, invoice: ApInvoice, *, field_name: str, review_audit_log_id: int | None = None,
) -> ApInvoiceLearningRecord:
    """Re-indexes one field correction from the ReviewAuditLog row
    review_queue_service.update_field() just wrote (old_value/new_value
    already computed there — read back here rather than recomputed) into
    a row keyed by (field_name, vendor_id) for evaluation queries.
    document_layout_hint is honestly disclosed as a freeform vendor-name
    guess — this codebase has no real template/layout classifier."""
    old_value: str | None = None
    new_value: str | None = None
    if review_audit_log_id is not None:
        log_row = await db.get(ReviewAuditLog, review_audit_log_id)
        if log_row is not None:
            old_value, new_value = log_row.old_value, log_row.new_value

    record = ApInvoiceLearningRecord(
        organization_id=invoice.organization_id, invoice_id=invoice.id, review_audit_log_id=review_audit_log_id,
        field_name=field_name, original_value=old_value, corrected_value=new_value,
        vendor_id=invoice.vendor_id, document_layout_hint=invoice.vendor_name,
    )
    db.add(record)
    await db.flush()
    return record


async def approve_for_learning(db: AsyncSession, record_id: int, organization_id: int | None) -> ApInvoiceLearningRecord:
    """Human curation step (Technical Design §7's "approve" stage) —
    marks a captured correction as good evaluation-set material. Never
    triggers any training; there's nothing downstream that consumes this
    flag except invoice_evaluation_service's own filters."""
    record = await db.get(ApInvoiceLearningRecord, record_id)
    if record is None or (organization_id is not None and record.organization_id not in (None, organization_id)):
        raise InvoiceLearningError(f"Learning record {record_id} was not found.")
    record.approved_for_learning = True
    await db.commit()
    await db.refresh(record)
    return record


async def list_learning_records(
    db: AsyncSession, organization_id: int | None, *,
    field_name: str | None = None, vendor_id: int | None = None, approved_only: bool = False,
    limit: int = 50, offset: int = 0,
) -> tuple[list[ApInvoiceLearningRecord], int]:
    from sqlalchemy import func

    stmt = select(ApInvoiceLearningRecord)
    count_stmt = select(func.count()).select_from(ApInvoiceLearningRecord)
    if organization_id is not None:
        stmt = stmt.where(ApInvoiceLearningRecord.organization_id == organization_id)
        count_stmt = count_stmt.where(ApInvoiceLearningRecord.organization_id == organization_id)
    if field_name is not None:
        stmt = stmt.where(ApInvoiceLearningRecord.field_name == field_name)
        count_stmt = count_stmt.where(ApInvoiceLearningRecord.field_name == field_name)
    if vendor_id is not None:
        stmt = stmt.where(ApInvoiceLearningRecord.vendor_id == vendor_id)
        count_stmt = count_stmt.where(ApInvoiceLearningRecord.vendor_id == vendor_id)
    if approved_only:
        stmt = stmt.where(ApInvoiceLearningRecord.approved_for_learning.is_(True))
        count_stmt = count_stmt.where(ApInvoiceLearningRecord.approved_for_learning.is_(True))

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(ApInvoiceLearningRecord.id.desc()).offset(offset).limit(limit)
    return list((await db.execute(stmt)).scalars().all()), total


# ── Narrow lifecycle-event logging ──────────────────────────────────
# Deliberately NOT a duplicate of Document.timeline (OCR/extraction stage
# timestamps) or ReviewAuditLog (field corrections/review actions) — see
# ApInvoiceAuditEventType's own docstring for the exact narrow event set
# this module is responsible for.

async def record_event(
    db: AsyncSession, *, invoice_id: int, organization_id: int | None,
    event_type: ApInvoiceAuditEventType, message: str | None = None, details: dict | None = None, actor_id: int | None = None,
) -> ApInvoiceAuditEvent:
    event = ApInvoiceAuditEvent(
        organization_id=organization_id, invoice_id=invoice_id, event_type=event_type,
        message=message, details=details or {}, actor_id=actor_id,
    )
    db.add(event)
    await db.flush()
    return event


async def list_events(db: AsyncSession, invoice_id: int, limit: int = 100) -> list[ApInvoiceAuditEvent]:
    stmt = (
        select(ApInvoiceAuditEvent)
        .where(ApInvoiceAuditEvent.invoice_id == invoice_id)
        .order_by(ApInvoiceAuditEvent.id.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())
