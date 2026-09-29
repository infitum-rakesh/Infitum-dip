from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Responses ────────────────────────────────────────────────────

class ReviewQueueItemOut(BaseModel):
    id: int
    document_id: int
    document_name: str
    document_type: str
    review_type: str
    issue: str
    field_name: str | None
    extracted_value: str | None
    expected_value: str | None
    confidence: float | None
    validation_status: str | None
    priority: str  # computed live — see review_queue_service.compute_priority
    status: str
    assigned_to_id: int | None
    assigned_to_name: str | None = None
    created_at: datetime
    age_minutes: int

    model_config = {"from_attributes": True}


class ReviewQueueListOut(BaseModel):
    items: list[ReviewQueueItemOut]
    total: int
    page: int
    page_size: int


class ReviewQueueCountOut(BaseModel):
    pending: int


class ReviewItemLookupOut(BaseModel):
    """Backs the Document Details page's "Review" button (real bug found
    live, user-reported: that page showed 'Cancel Review' on a
    NEEDS_REVIEW document but had no way to actually GET to the review
    item — nothing pointed the reviewer at the Review Queue's own detail
    page). None when the document has no open item — a real, honest
    state (e.g. it was just cancelled/resolved in another tab), not an
    error; the frontend falls back to the Review Queue list in that case."""
    review_id: int | None


class ReviewQueueFacetsOut(BaseModel):
    assignees: list[dict[str, Any]]  # [{id, full_name}]


class ReviewQueueStatsOut(BaseModel):
    pending_review: int
    assigned_to_me: int
    high_priority: int
    overdue: int
    approved_today: int
    rejected_today: int


class ExtractionFieldReviewOut(BaseModel):
    name: str
    value: Any
    corrected_value: Any | None = None
    confidence: float | None
    source_page: int | None
    bbox: list[float] | None = None
    validation_status: str | None
    suggested_correction: Any | None = None
    missing: bool = False


class AuditLogOut(BaseModel):
    id: int
    user_name: str | None
    action: str
    old_value: str | None
    new_value: str | None
    reason: str | None
    created_at: datetime


class ReviewQueueDetailOut(BaseModel):
    id: int
    document_id: int
    document_name: str
    document_type: str
    document_status: str
    document_preview_available: bool
    # File type ("PDF"/"IMAGE"/etc, from Document.doc_type) — distinct from
    # document_type above (the business classification, e.g. "Invoice").
    # The viewer needs this to pick <iframe> (PDF) vs <img> (image); without
    # it, PDFs were always rendered through <img>, which browsers cannot
    # display, producing a broken-image icon instead of the document.
    document_file_type: str | None = None
    review_type: str
    issue: str
    field_name: str | None
    extracted_value: str | None
    expected_value: str | None
    confidence: float | None
    validation_status: str | None
    priority: str
    status: str
    assigned_to_id: int | None
    assigned_to_name: str | None
    corrections: dict[str, Any]
    rejection_reason: str | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    age_minutes: int
    fields: list[ExtractionFieldReviewOut]
    audit_log: list[AuditLogOut]


# ── Requests ─────────────────────────────────────────────────────

class AssignRequest(BaseModel):
    user_id: int | None = None  # None = unassign


class RejectRequest(BaseModel):
    reason: str


class EscalateRequest(BaseModel):
    reason: str | None = None
    assign_to_user_id: int | None = None  # the senior reviewer, if known


class FieldUpdateRequest(BaseModel):
    field_name: str
    corrected_value: str
    reason: str | None = None


class BulkAssignRequest(BaseModel):
    review_ids: list[int]
    user_id: int | None = None


class BulkApproveRequest(BaseModel):
    review_ids: list[int]


class BulkRejectRequest(BaseModel):
    review_ids: list[int]
    reason: str
