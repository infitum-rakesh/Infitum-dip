"""
ReviewQueueService — human-in-the-loop workflow that sits between AI
Extraction's confidence/validation check and the structured dataset.

Integration, not duplication: this module never re-implements OCR or AI
Extraction. It reads a Document's existing `extraction_result` (produced by
document_service.process_document) to decide what to flag and, on Approve,
writes corrected values back through document_service.build_dataset() —
the one place dataset shape is defined.

    Document -> Image Scanner -> OCR -> AI Extraction -> Confidence Check
                                                              |
                                                   PASS ------+------ FAIL/LOW
                                                    |                    |
                                              Structured Dataset   Review Queue (this module)
                                                                          |
                                                                    Human Review
                                                                    /          \\
                                                               Approve       Reject
                                                                  |             |
                                                          Structured Dataset  Reprocess
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import get_settings
from app.models.document import Document, DocumentStatus
from app.models.review_queue import ReviewAuditLog, ReviewPriority, ReviewQueueItem, ReviewStatus, ReviewType
from app.models.user import User
from app.services import document_service as doc_svc

settings = get_settings()

OPEN_STATUSES = {ReviewStatus.PENDING, ReviewStatus.IN_PROGRESS, ReviewStatus.ESCALATED}

# Business-document classification, derived from the pipeline name assigned
# during processing. Tune here — not something a page should hardcode.
PIPELINE_TO_DOCUMENT_TYPE = {
    "Invoice Pipeline": "Invoice",
    "Contract Pipeline": "Contract",
    "Receipt Pipeline": "Other",
    "General Document Pipeline": "Other",
}

# Document types carrying inherent business risk get a priority floor,
# regardless of how confident extraction was. Tune here.
HIGH_IMPACT_DOCUMENT_TYPES = {"Contract"}

_PRIORITY_LEVELS = [ReviewPriority.LOW, ReviewPriority.MEDIUM, ReviewPriority.HIGH, ReviewPriority.CRITICAL]


def business_document_type(doc: Document) -> str:
    return PIPELINE_TO_DOCUMENT_TYPE.get(doc.pipeline, "Other")


def compute_priority(
    *,
    confidence: float | None,
    validation_status: str | None,
    document_type: str,
    created_at: datetime,
    review_type: ReviewType | str,
) -> ReviewPriority:
    """Confidence + validation severity + business impact (document type) +
    age — all configurable via Settings (see core/config.py)."""
    if confidence is None:
        idx = _PRIORITY_LEVELS.index(ReviewPriority.MEDIUM)
    elif confidence < settings.review_priority_critical_below:
        idx = _PRIORITY_LEVELS.index(ReviewPriority.CRITICAL)
    elif confidence < settings.review_priority_high_below:
        idx = _PRIORITY_LEVELS.index(ReviewPriority.HIGH)
    elif confidence < settings.review_priority_medium_below:
        idx = _PRIORITY_LEVELS.index(ReviewPriority.MEDIUM)
    else:
        idx = _PRIORITY_LEVELS.index(ReviewPriority.LOW)

    review_type_val = review_type.value if isinstance(review_type, ReviewType) else review_type
    if review_type_val == ReviewType.BUSINESS_RULE.value:
        idx = max(idx, _PRIORITY_LEVELS.index(ReviewPriority.HIGH))
    if document_type in HIGH_IMPACT_DOCUMENT_TYPES:
        idx = max(idx, _PRIORITY_LEVELS.index(ReviewPriority.HIGH))
    if validation_status == "invalid":
        idx = min(idx + 1, len(_PRIORITY_LEVELS) - 1)

    age_hours = (datetime.now(timezone.utc) - _aware(created_at)).total_seconds() / 3600
    if age_hours > settings.review_priority_age_escalation_hours:
        idx = min(idx + 1, len(_PRIORITY_LEVELS) - 1)

    return _PRIORITY_LEVELS[idx]


def is_overdue(created_at: datetime) -> bool:
    age_hours = (datetime.now(timezone.utc) - _aware(created_at)).total_seconds() / 3600
    return age_hours > settings.review_priority_age_escalation_hours


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_minutes(created_at: datetime) -> int:
    return int((datetime.now(timezone.utc) - _aware(created_at)).total_seconds() / 60)


# ── Picking what to flag ──────────────────────────────────────────

def _pick_primary_issue(doc: Document) -> dict | None:
    """Chooses the single most severe flagged field on a document, in order:
    business-rule violation > missing field > lowest-confidence field."""
    fields = (doc.extraction_result or {}).get("fields", [])
    if not fields:
        return None

    if (doc.extraction_result or {}).get("business_rule_flag"):
        total_field = next((f for f in fields if f["name"] == "Total"), None)
        if total_field:
            return {
                "review_type": ReviewType.BUSINESS_RULE,
                "issue": f"Total exceeds auto-approval threshold ({settings.review_business_rule_total_threshold:,.0f})",
                "field_name": total_field["name"],
                "extracted_value": str(total_field["value"]),
                "expected_value": None,
                "confidence": total_field["confidence"],
                "validation_status": total_field["validation_status"],
            }

    missing = [f for f in fields if f.get("missing")]
    if missing:
        f = missing[0]
        return {
            "review_type": ReviewType.AI_EXTRACTION,
            "issue": "Missing Field",
            "field_name": f["name"],
            "extracted_value": f["value"] or None,
            "expected_value": f.get("suggested_correction"),
            "confidence": f["confidence"],
            "validation_status": f["validation_status"],
        }

    flagged = [f for f in fields if f["validation_status"] != "valid"]
    if flagged:
        worst = min(flagged, key=lambda f: f["confidence"])
        issue = "Low AI Confidence" if worst["validation_status"] == "warning" else "Low AI Confidence — Validation Failed"
        return {
            "review_type": ReviewType.AI_EXTRACTION,
            "issue": issue,
            "field_name": worst["name"],
            "extracted_value": str(worst["value"]),
            "expected_value": worst.get("suggested_correction"),
            "confidence": worst["confidence"],
            "validation_status": worst["validation_status"],
        }

    return None


async def _open_item_for_document(db: AsyncSession, document_id: int) -> ReviewQueueItem | None:
    result = await db.execute(
        select(ReviewQueueItem).where(
            ReviewQueueItem.document_id == document_id,
            ReviewQueueItem.status.in_([s for s in OPEN_STATUSES]),
        )
    )
    return result.scalar_one_or_none()


def _build_item(doc: Document, picked: dict, *, assigned_to_id: int | None = None) -> ReviewQueueItem:
    return ReviewQueueItem(
        organization_id=doc.organization_id,
        document_id=doc.id,
        review_type=picked["review_type"],
        issue=picked["issue"],
        document_type=business_document_type(doc),
        field_name=picked["field_name"],
        extracted_value=picked["extracted_value"],
        expected_value=picked["expected_value"],
        confidence=picked["confidence"],
        validation_status=picked["validation_status"],
        status=ReviewStatus.IN_PROGRESS if assigned_to_id is not None else ReviewStatus.PENDING,
        assigned_to_id=assigned_to_id,
    )


async def create_from_document(db: AsyncSession, doc: Document) -> ReviewQueueItem | None:
    """Called by the Documents API right after processing lands a document in
    NEEDS_REVIEW. Idempotent-ish: skips if an open item already exists."""
    if await _open_item_for_document(db, doc.id):
        return None

    picked = _pick_primary_issue(doc)
    if picked is None:
        return None

    item = _build_item(doc, picked)
    db.add(item)
    await db.flush()
    await _log(db, item.id, None, "Flagged for Review", new_value=picked["issue"], reason="Auto-flagged by Document Intelligence pipeline")
    await db.commit()
    await db.refresh(item)
    return item


_MANUAL_ISSUE = {
    "review_type": ReviewType.VALIDATION,
    "issue": "Manually sent for review",
    "field_name": None,
    "extracted_value": None,
    "expected_value": None,
    "confidence": None,
    "validation_status": None,
}


async def create_manual(
    db: AsyncSession, doc: Document, *, requested_by: User, reviewer_id: int | None, note: str | None
) -> ReviewQueueItem:
    """Called when a human explicitly uses Documents' "Send to Review" action
    (single or bulk) — as opposed to the pipeline auto-flagging on low
    confidence. Always produces (or reuses) a queue item, even for a document
    with no specific low-confidence field to point at."""
    existing = await _open_item_for_document(db, doc.id)
    if existing:
        if reviewer_id is not None and existing.assigned_to_id != reviewer_id:
            return await assign(db, existing, reviewer_id, requested_by)
        return existing

    picked = _pick_primary_issue(doc) or {**_MANUAL_ISSUE, "confidence": doc.confidence}
    item = _build_item(doc, picked, assigned_to_id=reviewer_id)
    db.add(item)
    await db.flush()
    await _log(db, item.id, requested_by.id, "Sent to Review", new_value=picked["issue"], reason=note)
    if reviewer_id is not None:
        await _log(db, item.id, requested_by.id, "Assigned", new_value=reviewer_id)
    await db.commit()
    await db.refresh(item)
    return item


async def auto_resolve_for_document(db: AsyncSession, document_id: int) -> None:
    """Called when a reprocess brings a document out of NEEDS_REVIEW — any
    still-open review items for it are superseded."""
    result = await db.execute(
        select(ReviewQueueItem).where(
            ReviewQueueItem.document_id == document_id,
            ReviewQueueItem.status.in_([s for s in OPEN_STATUSES]),
        )
    )
    for item in result.scalars().all():
        item.status = ReviewStatus.APPROVED
        item.resolved_at = datetime.now(timezone.utc)
        await _log(db, item.id, None, "Auto-Resolved", reason="Document reprocessed successfully — no longer needs review")
    await db.commit()


# ── Audit ────────────────────────────────────────────────────────

async def _log(db: AsyncSession, item_id: int, user_id: int | None, action: str, *, old_value=None, new_value=None, reason=None) -> None:
    db.add(ReviewAuditLog(
        review_item_id=item_id, user_id=user_id, action=action,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None,
        reason=reason,
    ))


async def _user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    result = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row.id: row.full_name for row in result.all()}


# ── Query / list ─────────────────────────────────────────────────

async def list_items(
    db: AsyncSession,
    *,
    organization_id: int | None,
    requesting_user_id: int,
    search: str = "",
    status_filter: str | None = None,
    priority_filter: str | None = None,
    review_type_filter: str | None = None,
    assigned: str | None = None,  # unassigned | me | specific_user
    assigned_user_id: int | None = None,
    document_type_filter: str | None = None,
    page: int = 1,
    page_size: int = 20,
    sort_by: str = "created_at",
    sort_dir: str = "asc",
) -> tuple[list[dict], int]:
    stmt = select(ReviewQueueItem)
    if organization_id is not None:
        stmt = stmt.where(ReviewQueueItem.organization_id == organization_id)
    if status_filter:
        stmt = stmt.where(ReviewQueueItem.status == status_filter)
    if review_type_filter:
        stmt = stmt.where(ReviewQueueItem.review_type == review_type_filter)
    if document_type_filter:
        stmt = stmt.where(ReviewQueueItem.document_type == document_type_filter)
    if assigned == "unassigned":
        stmt = stmt.where(ReviewQueueItem.assigned_to_id.is_(None))
    elif assigned == "me":
        stmt = stmt.where(ReviewQueueItem.assigned_to_id == requesting_user_id)
    elif assigned == "specific_user" and assigned_user_id:
        stmt = stmt.where(ReviewQueueItem.assigned_to_id == assigned_user_id)

    result = await db.execute(stmt)
    items = list(result.scalars().all())

    doc_ids = {i.document_id for i in items}
    docs_result = await db.execute(select(Document).where(Document.id.in_(doc_ids))) if doc_ids else None
    doc_map = {d.id: d for d in docs_result.scalars().all()} if docs_result else {}

    assignee_ids = {i.assigned_to_id for i in items if i.assigned_to_id}
    name_map = await _user_name_map(db, assignee_ids)

    enriched = []
    for i in items:
        doc = doc_map.get(i.document_id)
        priority = compute_priority(
            confidence=i.confidence, validation_status=i.validation_status,
            document_type=i.document_type, created_at=i.created_at, review_type=i.review_type,
        )
        if priority_filter and priority.value != priority_filter:
            continue
        needle = search.lower()
        if search:
            haystacks = [
                doc.filename if doc else "", str(i.id), i.document_type, i.issue,
                i.field_name or "", i.extracted_value or "", i.status.value,
                name_map.get(i.assigned_to_id, ""),
            ]
            if not any(needle in h.lower() for h in haystacks if h):
                continue
        enriched.append({
            "item": i, "document": doc, "priority": priority,
            "assigned_to_name": name_map.get(i.assigned_to_id),
        })

    reverse = sort_dir == "desc"
    if sort_by == "priority":
        order = {p: idx for idx, p in enumerate(reversed(_PRIORITY_LEVELS))}
        enriched.sort(key=lambda e: order.get(e["priority"], 99), reverse=reverse)
    else:
        enriched.sort(key=lambda e: getattr(e["item"], sort_by, e["item"].created_at) or e["item"].created_at, reverse=reverse)

    total = len(enriched)
    start = (page - 1) * page_size
    return enriched[start:start + page_size], total


def to_list_out(entry: dict) -> dict:
    i, doc = entry["item"], entry["document"]
    return {
        "id": i.id,
        "document_id": i.document_id,
        "document_name": doc.filename if doc else f"doc_{i.document_id}",
        "document_type": i.document_type,
        "review_type": i.review_type.value,
        "issue": i.issue,
        "field_name": i.field_name,
        "extracted_value": i.extracted_value,
        "expected_value": i.expected_value,
        "confidence": i.confidence,
        "validation_status": i.validation_status,
        "priority": entry["priority"].value,
        "status": i.status.value,
        "assigned_to_id": i.assigned_to_id,
        "assigned_to_name": entry["assigned_to_name"],
        "created_at": i.created_at,
        "age_minutes": _age_minutes(i.created_at),
    }


async def get_stats(db: AsyncSession, organization_id: int | None, requesting_user_id: int) -> dict:
    stmt = select(ReviewQueueItem)
    if organization_id is not None:
        stmt = stmt.where(ReviewQueueItem.organization_id == organization_id)
    result = await db.execute(stmt)
    items = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    today = now.date()

    pending = [i for i in items if i.status == ReviewStatus.PENDING]
    high_priority = [
        i for i in items if i.status in OPEN_STATUSES
        and compute_priority(confidence=i.confidence, validation_status=i.validation_status,
                              document_type=i.document_type, created_at=i.created_at, review_type=i.review_type)
        in (ReviewPriority.HIGH, ReviewPriority.CRITICAL)
    ]
    overdue = [i for i in items if i.status in OPEN_STATUSES and is_overdue(i.created_at)]
    approved_today = [i for i in items if i.status == ReviewStatus.APPROVED and i.resolved_at and _aware(i.resolved_at).date() == today]
    rejected_today = [i for i in items if i.status == ReviewStatus.REJECTED and i.resolved_at and _aware(i.resolved_at).date() == today]
    assigned_to_me = [i for i in items if i.assigned_to_id == requesting_user_id and i.status in OPEN_STATUSES]

    return {
        "pending_review": len(pending),
        "assigned_to_me": len(assigned_to_me),
        "high_priority": len(high_priority),
        "overdue": len(overdue),
        "approved_today": len(approved_today),
        "rejected_today": len(rejected_today),
    }


async def get_assignable_users(db: AsyncSession, organization_id: int | None) -> list[dict]:
    """Org users who can be assigned/escalated to — backs the 'Assigned To:
    Specific User' filter and the reassign/escalate pickers."""
    stmt = select(User.id, User.full_name).where(User.is_active.is_(True))
    if organization_id is not None:
        stmt = stmt.where(User.organization_id == organization_id)
    result = await db.execute(stmt)
    return [{"id": row.id, "full_name": row.full_name} for row in result.all()]


async def get_count(db: AsyncSession, organization_id: int | None) -> int:
    stmt = select(ReviewQueueItem).where(ReviewQueueItem.status == ReviewStatus.PENDING)
    if organization_id is not None:
        stmt = stmt.where(ReviewQueueItem.organization_id == organization_id)
    result = await db.execute(stmt)
    return len(list(result.scalars().all()))


async def get_item(db: AsyncSession, item_id: int, organization_id: int | None) -> ReviewQueueItem | None:
    stmt = select(ReviewQueueItem).where(ReviewQueueItem.id == item_id)
    if organization_id is not None:
        stmt = stmt.where(ReviewQueueItem.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_detail(db: AsyncSession, item: ReviewQueueItem) -> dict:
    doc = await doc_svc.get_document(db, item.document_id, item.organization_id)
    fields_raw = (doc.extraction_result or {}).get("fields", []) if doc else []
    fields = [
        {
            "name": f["name"], "value": f["value"],
            "corrected_value": item.corrections.get(f["name"]),
            "confidence": f.get("confidence"), "source_page": f.get("source_page"),
            "bbox": f.get("bbox"), "validation_status": f.get("validation_status"),
            "suggested_correction": f.get("suggested_correction"), "missing": f.get("missing", False),
        }
        for f in fields_raw
    ]

    audit_result = await db.execute(
        select(ReviewAuditLog).where(ReviewAuditLog.review_item_id == item.id).order_by(ReviewAuditLog.created_at.asc())
    )
    audit_rows = list(audit_result.scalars().all())
    actor_ids = {a.user_id for a in audit_rows if a.user_id} | ({item.assigned_to_id} if item.assigned_to_id else set())
    name_map = await _user_name_map(db, actor_ids)

    priority = compute_priority(
        confidence=item.confidence, validation_status=item.validation_status,
        document_type=item.document_type, created_at=item.created_at, review_type=item.review_type,
    )

    return {
        "id": item.id, "document_id": item.document_id,
        "document_name": doc.filename if doc else f"doc_{item.document_id}",
        "document_type": item.document_type,
        "document_status": doc.status.value if doc else "UNKNOWN",
        "document_preview_available": doc is not None,
        "review_type": item.review_type.value, "issue": item.issue,
        "field_name": item.field_name, "extracted_value": item.extracted_value,
        "expected_value": item.expected_value, "confidence": item.confidence,
        "validation_status": item.validation_status, "priority": priority.value,
        "status": item.status.value, "assigned_to_id": item.assigned_to_id,
        "assigned_to_name": name_map.get(item.assigned_to_id) if item.assigned_to_id else None,
        "corrections": item.corrections or {}, "rejection_reason": item.rejection_reason,
        "created_at": item.created_at, "updated_at": item.updated_at, "resolved_at": item.resolved_at,
        "age_minutes": _age_minutes(item.created_at),
        "fields": fields,
        "audit_log": [
            {
                "id": a.id, "user_name": name_map.get(a.user_id) if a.user_id else "System",
                "action": a.action, "old_value": a.old_value, "new_value": a.new_value,
                "reason": a.reason, "created_at": a.created_at,
            }
            for a in audit_rows
        ],
    }


# ── Actions ──────────────────────────────────────────────────────

async def assign(db: AsyncSession, item: ReviewQueueItem, user_id: int | None, actor: User) -> ReviewQueueItem:
    old = item.assigned_to_id
    item.assigned_to_id = user_id
    if user_id is not None and item.status == ReviewStatus.PENDING:
        item.status = ReviewStatus.IN_PROGRESS
    await _log(db, item.id, actor.id, "Assigned", old_value=old, new_value=user_id)
    await db.commit()
    await db.refresh(item)
    return item


async def update_field(db: AsyncSession, item: ReviewQueueItem, field_name: str, corrected_value: str, reason: str | None, actor: User) -> ReviewQueueItem:
    doc = await doc_svc.get_document(db, item.document_id, item.organization_id)
    old_value = None
    if doc and doc.extraction_result:
        f = next((f for f in doc.extraction_result.get("fields", []) if f["name"] == field_name), None)
        old_value = f["value"] if f else None

    corrections = dict(item.corrections or {})
    corrections[field_name] = corrected_value
    item.corrections = corrections
    if item.status == ReviewStatus.PENDING:
        item.status = ReviewStatus.IN_PROGRESS

    await _log(db, item.id, actor.id, "Field Updated", old_value=old_value, new_value=corrected_value, reason=reason)
    await db.commit()
    await db.refresh(item)
    return item


async def approve(db: AsyncSession, item: ReviewQueueItem, actor: User) -> ReviewQueueItem:
    doc = await doc_svc.get_document(db, item.document_id, item.organization_id)
    if doc and doc.extraction_result:
        corrections = item.corrections or {}
        # Build brand-new field dicts (never mutate the ones already inside
        # doc.extraction_result in place) — otherwise old/new compare equal
        # by reference and SQLAlchemy's JSON column won't see a change to persist.
        fields = [
            {**f, "value": corrections[f["name"]], "validation_status": "valid", "missing": False}
            if f["name"] in corrections else dict(f)
            for f in doc.extraction_result.get("fields", [])
        ]
        doc.extraction_result = {**doc.extraction_result, "fields": fields}
        doc.dataset = doc_svc.build_dataset(fields)
        flag_modified(doc, "extraction_result")
        flag_modified(doc, "dataset")
        valid_confidences = [f["confidence"] for f in fields if f.get("confidence") is not None]
        if valid_confidences:
            doc.extraction_percent = round(sum(valid_confidences) / len(valid_confidences), 1)
        doc.status = DocumentStatus.COMPLETED

    item.status = ReviewStatus.APPROVED
    item.resolved_at = datetime.now(timezone.utc)
    await _log(db, item.id, actor.id, "Approved", reason=None)
    await db.commit()
    await db.refresh(item)
    return item


async def reject(db: AsyncSession, item: ReviewQueueItem, reason: str, actor: User) -> ReviewQueueItem:
    doc = await doc_svc.get_document(db, item.document_id, item.organization_id)
    if doc:
        doc.status = DocumentStatus.UPLOADED  # back to the top of the pipeline for reprocessing

    item.status = ReviewStatus.REJECTED
    item.rejection_reason = reason
    item.resolved_at = datetime.now(timezone.utc)
    await _log(db, item.id, actor.id, "Rejected", reason=reason)
    await db.commit()
    await db.refresh(item)
    return item


async def cancel_for_document(db: AsyncSession, document_id: int, actor: User, reason: str | None = None) -> ReviewQueueItem | None:
    """Documents' "Cancel Review" action — withdraws the open review request.
    The document goes back to COMPLETED if it already has extraction data
    (the common case: someone flagged an otherwise-fine document out of an
    abundance of caution), or UPLOADED if it was never processed."""
    item = await _open_item_for_document(db, document_id)
    if item is None:
        return None

    doc = await doc_svc.get_document(db, document_id, item.organization_id)
    if doc:
        doc.status = DocumentStatus.COMPLETED if doc.extraction_result else DocumentStatus.UPLOADED

    item.status = ReviewStatus.CANCELLED
    item.resolved_at = datetime.now(timezone.utc)
    await _log(db, item.id, actor.id, "Cancelled", reason=reason)
    await db.commit()
    await db.refresh(item)
    return item


async def escalate(db: AsyncSession, item: ReviewQueueItem, reason: str | None, assign_to_user_id: int | None, actor: User) -> ReviewQueueItem:
    item.status = ReviewStatus.ESCALATED
    if assign_to_user_id is not None:
        item.assigned_to_id = assign_to_user_id
    await _log(db, item.id, actor.id, "Escalated", new_value=assign_to_user_id, reason=reason)
    await db.commit()
    await db.refresh(item)
    return item
