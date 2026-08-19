from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.connections import ColumnOut, PreviewOut


class ObjectRef(BaseModel):
    connection_id: int
    schema_name: str | None = None
    object_name: str


class LookupConfig(BaseModel):
    table: str
    key_column: str
    return_column: str


class MappingRow(BaseModel):
    id: str
    source_columns: list[str] = []
    target_column: str
    transformation: str = ""
    constant_value: Any | None = None
    default_value: Any | None = None
    lookup: LookupConfig | None = None
    status: str = "MAPPED"  # MAPPED | SUGGESTED | UNMAPPED | CONFLICT
    confidence: float | None = None
    match_method: str = "manual"  # exact | case_insensitive | normalized | alias | similarity | manual


# ── Responses ────────────────────────────────────────────────────

class ColumnMappingOut(BaseModel):
    id: int
    name: str
    source_connection_id: int
    source_schema: str | None
    source_object: str | None
    target_connection_id: int
    target_schema: str | None
    target_object: str | None
    mappings: list[MappingRow]
    status: str
    created_by: int | None
    created_by_name: str | None = None
    updated_by: int | None
    updated_by_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ColumnMappingListOut(BaseModel):
    items: list[ColumnMappingOut]
    total: int


class ColumnMappingDetailOut(ColumnMappingOut):
    """Adds LIVE re-fetched schema — never cached, so drift since the
    mapping was last saved is always visible in the editor."""
    source_columns: list[ColumnOut]
    target_columns: list[ColumnOut]


class AutoMapResponse(BaseModel):
    proposals: list[MappingRow]
    ai_assisted_available: bool = False  # honest — see mapping/auto_mapper.py docstring


class ValidateIssue(BaseModel):
    row_id: str | None
    severity: str  # ERROR | WARNING | INFO
    message: str


class ValidateResponse(BaseModel):
    issues: list[ValidateIssue]
    row_status: dict[str, str]
    overall_status: str  # VALIDATED | WARNING | CONFLICT


# ── Requests ─────────────────────────────────────────────────────

class ColumnMappingCreateRequest(BaseModel):
    name: str
    source: ObjectRef
    target: ObjectRef
    mappings: list[MappingRow] = []


class ColumnMappingUpdateRequest(BaseModel):
    name: str | None = None
    mappings: list[MappingRow] | None = None


class AutoMapRequest(BaseModel):
    source: ObjectRef
    target: ObjectRef
    existing_mappings: list[MappingRow] = []


class ValidateRequest(BaseModel):
    source: ObjectRef
    target: ObjectRef
    mappings: list[MappingRow]


class MappingPreviewRequest(BaseModel):
    source: ObjectRef
    mappings: list[MappingRow]
    limit: int = 50


MappingPreviewResponse = PreviewOut
