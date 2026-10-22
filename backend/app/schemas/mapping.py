from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.connections import ColumnOut, PreviewOut


class ObjectRef(BaseModel):
    """Document Mapping Profiles (Document Intelligence: Dynamic JSON
    Extraction spec) add two optional, mutually-exclusive-with-connection
    discriminators rather than a second ref type: `document_type` names a
    Document Intelligence document_type (e.g. "INVOICE") as the SOURCE —
    connection_id/object_name are unused when set, since there's no real
    connector; `target_format` names what kind of TARGET this is
    ("DB_TABLE" — the default, unchanged, connection-based behavior — or
    "INVOICE_SCHEMA" — ApInvoice's own real columns, also no connector).
    See services/mapping_service.py::fetch_columns(), the one place this
    branches."""
    connection_id: int | None = None
    schema_name: str | None = None
    object_name: str | None = None
    document_type: str | None = None
    target_format: str | None = None


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
    is_key: bool = False  # business/natural key — CM2/spec section 30, feeds SCD1/SCD2 business-key resolution (execution/mapping_execution.py)


# ── Responses ────────────────────────────────────────────────────

class ColumnMappingOut(BaseModel):
    id: int
    name: str
    source_connection_id: int | None
    source_schema: str | None
    source_object: str | None
    source_document_type: str | None = None
    target_connection_id: int | None
    target_schema: str | None
    target_object: str | None
    target_format: str | None = None
    mappings: list[MappingRow]  # CM2 — resolved via get_effective_mappings(), never the (retired) header column directly
    status: str
    latest_version: int
    current_published_version: int | None
    created_by: int | None
    created_by_name: str | None = None
    updated_by: int | None
    updated_by_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ColumnMappingVersionOut(BaseModel):
    id: int
    mapping_id: int
    version_number: int
    status: str  # DRAFT | VALIDATED | PUBLISHED | RETIRED
    mappings: list[MappingRow]
    source_connection_id: int | None
    source_schema: str | None
    source_object: str | None
    source_document_type: str | None = None
    target_connection_id: int | None
    target_schema: str | None
    target_object: str | None
    target_format: str | None = None
    unmapped_column_policy: str
    batch_size: int | None
    on_conversion_failure: str
    change_description: str | None
    validation_result: dict | None = None
    created_by: int | None
    created_by_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ColumnMappingVersionListOut(BaseModel):
    items: list[ColumnMappingVersionOut]
    total: int


class MappingUsageOut(BaseModel):
    id: int
    mapping_id: int
    mapping_version: int
    pipeline_id: int
    pipeline_name: str | None = None
    pipeline_version: int
    node_id: str
    environment: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MappingUsageListOut(BaseModel):
    items: list[MappingUsageOut]
    total: int


class StalenessOut(BaseModel):
    stale: bool
    issues: list[str] = []
    source_column_count_then: int
    source_column_count_now: int
    target_column_count_then: int
    target_column_count_now: int


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
    """Header-only edit (name). Mapping-row edits go through
    CreateVersionRequest / POST /mappings/{id}/versions instead — CM2
    replaces the old "update overwrites mappings in place" behavior with
    real immutable versioning, see services/mapping_service.py."""
    name: str | None = None


class CreateVersionRequest(BaseModel):
    mappings: list[MappingRow]
    change_description: str | None = None
    unmapped_column_policy: str = "IGNORE"  # IGNORE | ERROR | WARNING
    batch_size: int | None = None
    on_conversion_failure: str = "REJECT_RECORD"  # FAIL_PIPELINE | REJECT_RECORD | SET_NULL | USE_DEFAULT


class PublishRequest(BaseModel):
    confirm: bool = False  # required True to publish over an already-in-use prior published version (informational only today, see CM11)


class RetireRequest(BaseModel):
    confirm: bool = False  # required True if column_mapping_usage shows this version is still attached to any pipeline


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
