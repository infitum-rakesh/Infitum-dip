"""
Schemas for Document Mapping Profiles' document-specific surface —
applying a PUBLISHED, document-sourced ColumnMappingVersion to one
Document and viewing its append-only execution history. The mapping
DEFINITION itself (create/version/validate/publish) is served entirely
by the existing schemas/mapping.py — see models/column_mapping.py's
module docstring for why no second set of CRUD schemas exists here.
"""
from datetime import datetime

from pydantic import BaseModel


class DocumentSourceColumnOut(BaseModel):
    name: str
    data_type: str


class DocumentSourceColumnsOut(BaseModel):
    document_type: str
    columns: list[DocumentSourceColumnOut]


class RejectedRowOut(BaseModel):
    row: dict
    error: str


class DocumentMappedOutputOut(BaseModel):
    id: int
    document_id: int
    mapping_id: int
    mapping_version: int
    target_format: str
    columns: list[str]
    rows: list[dict]
    row_count: int
    status: str  # SUCCESS | FAILED
    error_message: str | None
    rejected_rows: list[RejectedRowOut]
    created_by: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentMappedOutputListOut(BaseModel):
    items: list[DocumentMappedOutputOut]
    total: int


class SampleDocumentOut(BaseModel):
    id: int
    filename: str
    uploaded_at: datetime


class SampleDocumentListOut(BaseModel):
    items: list[SampleDocumentOut]


class PreviewAgainstDocumentRequest(BaseModel):
    document_id: int
    source_document_type: str
    target_format: str | None = None
    mappings: list[dict] = []


class PreviewAgainstDocumentResponse(BaseModel):
    columns: list[str]
    rows: list[dict]
    row_count: int
    rejected_rows: list[RejectedRowOut]


# ── Scheduled Mapping (spec section 7) ──────────────────────────────

class DocumentMappingDefaultOut(BaseModel):
    id: int
    organization_id: int | None
    document_type: str
    mapping_id: int
    mapping_name: str | None = None
    created_by: int | None
    updated_by: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentMappingDefaultListOut(BaseModel):
    items: list[DocumentMappingDefaultOut]


class SetDocumentMappingDefaultRequest(BaseModel):
    document_type: str
    mapping_id: int


class ScheduleUsingMappingOut(BaseModel):
    schedule_id: int
    schedule_name: str
    document_type: str | None
    usage: str  # "EXPLICIT" | "DEFAULT"


class ScheduleUsingMappingListOut(BaseModel):
    items: list[ScheduleUsingMappingOut]


class RetryScheduledMappingResponse(BaseModel):
    candidates: int
    applied_success: int
    applied_failed: int
    already_applied: int
    config_error: int
    no_mapping_configured: int
    last_error: str | None = None
