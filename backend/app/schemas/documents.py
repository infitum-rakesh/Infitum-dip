from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Shared ───────────────────────────────────────────────────────

class TimelineStage(BaseModel):
    stage: str
    status: str  # pending | running | completed | failed
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    confidence: float | None = None
    error: str | None = None
    # Set on the OCR stage only, only when Tesseract found no readable text
    # and the AI-vision fallback ran instead (services/document_service.py's
    # _run_vision_ocr_fallback()) — informational, not an error.
    note: str | None = None


# ── Responses ────────────────────────────────────────────────────

class DocumentOut(BaseModel):
    id: int
    filename: str
    folder: str
    doc_type: str
    mime_type: str | None
    size_bytes: int
    page_count: int
    status: str
    pipeline: str | None
    ocr_percent: float | None
    extraction_percent: float | None
    confidence: float | None
    language_config: dict[str, Any] | None = None
    uploaded_by_id: int | None
    uploaded_by_name: str | None = None
    uploaded_at: datetime
    processing_time_seconds: float | None

    model_config = {"from_attributes": True}


class DocumentDetailOut(DocumentOut):
    organization_id: int | None
    storage_path: str
    timeline: list[TimelineStage] = []
    error_message: str | None
    processing_started_at: datetime | None
    processing_completed_at: datetime | None


class DocumentListOut(BaseModel):
    items: list[DocumentOut]
    total: int
    page: int
    page_size: int


class DocumentStatsOut(BaseModel):
    total_documents: int
    processing: int
    completed: int
    needs_review: int
    failed: int
    storage_used_bytes: int


class DocumentFacetsOut(BaseModel):
    uploaders: list[dict[str, Any]]  # [{id, full_name}]
    pipelines: list[str]


class OcrRegion(BaseModel):
    page: int
    text: str
    bbox: list[float]  # [x, y, width, height] in source-image pixels
    confidence: float


class OcrPageDims(BaseModel):
    page: int
    width: int   # source-image pixels — use with OcrRegion.bbox to compute % overlays
    height: int


class OcrOut(BaseModel):
    document_id: int
    full_text: str
    confidence: float | None
    regions: list[OcrRegion]
    pages: list[OcrPageDims] = []


class ExtractionField(BaseModel):
    name: str
    value: str
    confidence: float
    source_page: int
    validation_status: str  # valid | warning | invalid


class ExtractionOut(BaseModel):
    document_id: int
    fields: list[ExtractionField]
    overall_confidence: float | None


class DatasetSchemaField(BaseModel):
    field: str
    type: str


class DatasetTable(BaseModel):
    columns: list[str]
    rows: list[list[Any]]


class DatasetOut(BaseModel):
    document_id: int
    dataset_schema: list[DatasetSchemaField] = []
    table: DatasetTable
    json_view: dict[str, Any]


# ── Requests ─────────────────────────────────────────────────────

class DocumentUpdateRequest(BaseModel):
    filename: str | None = None
    folder: str | None = None


class BulkActionRequest(BaseModel):
    document_ids: list[int]
    action: str  # process | send_to_review | delete
    reviewer_id: int | None = None  # used when action == "send_to_review"
    note: str | None = None         # used when action == "send_to_review"


class SendToReviewRequest(BaseModel):
    reviewer_id: int | None = None
    note: str | None = None


class ManualFieldIn(BaseModel):
    name: str
    value: str


class ManualTableIn(BaseModel):
    name: str
    columns: list[str]
    rows: list[dict[str, str]]


class RunExtractionRequest(BaseModel):
    # e.g. "INVOICE" — see document_taxonomies.py's available_document_types().
    # None/omitted keeps the original ad-hoc "let the LLM guess a schema"
    # behavior instead of seeding a canonical taxonomy.
    document_type: str | None = None
    # Explicit AI Provider connection id to use instead of the automatic
    # best-effort pick — lets a user route around a connection that's
    # genuinely broken (e.g. out of API credit) right from the Run AI
    # Extraction control. None/omitted keeps "Auto" behavior: try every
    # active connection in order until one works.
    connection_id: int | None = None


class ManualExtractionRequest(BaseModel):
    """User-authored structured data, built by hand from the document's OCR
    text when AI Extraction found nothing (or the user wants to override
    it) — see document_service.apply_manual_extraction(). `fields` covers
    flat name/value pairs; `tables` covers genuine repeating-row data
    (e.g. invoice line items) using the same table shape extraction_service
    produces for the AI path, so build_dataset() handles both identically."""
    fields: list[ManualFieldIn] = []
    tables: list[ManualTableIn] = []
