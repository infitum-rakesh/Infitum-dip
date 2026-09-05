"""
invoice_evaluation_service.py — read-only metrics over already-real data
(ApInvoice decisions + ApInvoiceLearningRecord corrections). Explicitly
NOT automated retraining or a model-quality score — see
invoice_learning_service.py's module docstring for the same disclosure.

Four named metrics (Technical Design §7):
  straight_through_rate — % of decided invoices the decision engine
                           routed STRAIGHT_THROUGH with zero human step.
  exception_rate        — % of decided invoices that were NOT
                           STRAIGHT_THROUGH (REVIEW_REQUIRED/REJECT/RETRY
                           combined — decide()'s own decision matrix,
                           never recomputed here).
  field_accuracy         — 1 - (correction rows / total extracted scalar
                           fields) across the same invoice set. A real
                           ratio of "fields a human never had to touch,"
                           not a model-confidence estimate.
  correction_recurrence  — which fields/vendors get corrected most often
                           (straight counts from ApInvoiceLearningRecord,
                           grouped) — the honest substitute for "which
                           extraction patterns need attention" that this
                           build can compute without a real ML pipeline.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceDecision, ApInvoiceVendor
from app.models.invoice_intelligence.learning import ApInvoiceLearningRecord


def _round_pct(numerator: int, denominator: int) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


async def get_metrics(db: AsyncSession, organization_id: int | None, *, since: datetime | None = None) -> dict:
    stmt = select(ApInvoice).where(ApInvoice.final_decision.is_not(None))
    if organization_id is not None:
        stmt = stmt.where(ApInvoice.organization_id == organization_id)
    if since is not None:
        stmt = stmt.where(ApInvoice.created_at >= since)
    decided = list((await db.execute(stmt)).scalars().all())

    total_decided = len(decided)
    straight_through = sum(1 for i in decided if i.final_decision == ApInvoiceDecision.STRAIGHT_THROUGH)
    exceptions = total_decided - straight_through

    # Field accuracy — batched fetch of the underlying Documents (one
    # query, not N) to count real extracted scalar fields per invoice.
    doc_ids = [i.document_id for i in decided]
    total_fields = 0
    if doc_ids:
        docs = list((await db.execute(select(Document.extraction_result).where(Document.id.in_(doc_ids)))).scalars().all())
        for extraction_result in docs:
            fields = (extraction_result or {}).get("fields", [])
            total_fields += sum(1 for f in fields if f.get("type") != "table")

    invoice_ids = [i.id for i in decided]
    correction_count = 0
    if invoice_ids:
        correction_stmt = select(func.count()).select_from(ApInvoiceLearningRecord).where(ApInvoiceLearningRecord.invoice_id.in_(invoice_ids))
        correction_count = (await db.execute(correction_stmt)).scalar_one()

    field_accuracy = None
    if total_fields:
        field_accuracy = round(100.0 * max(0, total_fields - correction_count) / total_fields, 1)

    return {
        "total_decided": total_decided,
        "straight_through_count": straight_through,
        "straight_through_rate_pct": _round_pct(straight_through, total_decided),
        "exception_count": exceptions,
        "exception_rate_pct": _round_pct(exceptions, total_decided),
        "total_extracted_fields": total_fields,
        "total_corrections": correction_count,
        "field_accuracy_pct": field_accuracy,
    }


async def get_recurring_corrections_by_field(db: AsyncSession, organization_id: int | None, limit: int = 10) -> list[dict]:
    stmt = select(ApInvoiceLearningRecord.field_name, func.count().label("count")).group_by(ApInvoiceLearningRecord.field_name)
    if organization_id is not None:
        stmt = stmt.where(ApInvoiceLearningRecord.organization_id == organization_id)
    stmt = stmt.order_by(func.count().desc()).limit(limit)
    return [{"field_name": row.field_name, "count": row.count} for row in (await db.execute(stmt)).all()]


async def get_recurring_corrections_by_vendor(db: AsyncSession, organization_id: int | None, limit: int = 10) -> list[dict]:
    stmt = (
        select(ApInvoiceLearningRecord.vendor_id, ApInvoiceVendor.name, func.count().label("count"))
        .join(ApInvoiceVendor, ApInvoiceVendor.id == ApInvoiceLearningRecord.vendor_id)
        .group_by(ApInvoiceLearningRecord.vendor_id, ApInvoiceVendor.name)
    )
    if organization_id is not None:
        stmt = stmt.where(ApInvoiceLearningRecord.organization_id == organization_id)
    stmt = stmt.order_by(func.count().desc()).limit(limit)
    return [{"vendor_id": row.vendor_id, "vendor_name": row.name, "count": row.count} for row in (await db.execute(stmt)).all()]
