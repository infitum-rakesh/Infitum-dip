from datetime import datetime

from pydantic import BaseModel


class JobCreateRequest(BaseModel):
    name: str
    # 0 is the real, established "not a real Connection — see
    # *_uploaded_file_id instead" sentinel (matches the AI-extraction
    # engine's connection_id=0 convention elsewhere in this codebase).
    source_connection_id: int
    source_schema: str | None = None
    source_object: str
    source_uploaded_file_id: int | None = None
    target_connection_id: int
    target_schema: str | None = None
    target_object: str
    target_uploaded_file_id: int | None = None
    key_columns: list[str] = []
    compare_columns: list[str] = []
    comparison_types: list[str] = ["COUNT"]  # subset of COUNT/CHECKSUM/KEY_MATCH/FIELD_LEVEL
    column_mapping: dict[str, str] = {}  # {source_column: target_column}, only for columns that differ by name
    # CM10 — pure convenience: when set, and column_mapping/key_columns
    # above were left empty, the service resolves this EXACT published
    # Column Mapping version and pre-populates column_mapping (source-
    # >target renames) and key_columns (is_key-flagged rows) from its
    # real rows. Explicit column_mapping/key_columns above always win —
    # this never overrides a value the caller actually provided. Purely
    # a same-request convenience; does not persist a link back to the
    # mapping (the standalone Column Mapping module's own versioning
    # already owns that lifecycle — see services/reconciliation_service.py
    # module docstring for why the real diff engine stays untouched).
    source_mapping_id: int | None = None
    source_mapping_version: int | None = None
    # ID->NAME fallback matching (spec sections 12-17) — source-side column
    # name(s); empty list (the default) means no fallback, exact-key-only
    # matching (byte-identical to every job created before this existed).
    fallback_match_columns: list[str] = []
    # {"trim": bool, "case_insensitive": bool, "collapse_spaces": bool,
    # "unicode_normalize": bool} — only read when fallback_match_columns is
    # non-empty; empty dict means the engine's own sensible defaults apply.
    name_normalization: dict = {}


class JobUpdateRequest(BaseModel):
    name: str | None = None
    key_columns: list[str] | None = None
    compare_columns: list[str] | None = None
    comparison_types: list[str] | None = None
    column_mapping: dict[str, str] | None = None
    fallback_match_columns: list[str] | None = None
    name_normalization: dict | None = None


class JobOut(BaseModel):
    id: int
    name: str
    source_connection_id: int
    source_schema: str | None
    source_object: str
    source_uploaded_file_id: int | None = None
    target_connection_id: int
    target_schema: str | None
    target_object: str
    target_uploaded_file_id: int | None = None
    key_columns: list[str]
    compare_columns: list[str]
    comparison_types: list[str]
    column_mapping: dict[str, str] = {}
    fallback_match_columns: list[str] = []
    name_normalization: dict = {}
    created_by: int | None
    created_by_name: str | None = None
    updated_by: int | None
    updated_by_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int


class RunOut(BaseModel):
    id: int
    job_id: int
    status: str
    error_message: str | None
    source_count: int | None
    target_count: int | None
    matched_count: int | None
    missing_count: int | None
    extra_count: int | None
    mismatched_count: int | None
    checksum_match: bool | None
    matched_by_id_count: int | None = None
    matched_by_name_count: int | None = None
    ambiguous_count: int | None = None
    duplicate_target_count: int | None = None
    exceptions_truncated: bool
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExceptionOut(BaseModel):
    id: int
    exception_type: str
    key_value: dict
    column: str | None
    source_value: str | None
    target_value: str | None
    match_method: str | None = None
    source_id: str | None = None
    source_name: str | None = None
    target_id: str | None = None
    target_name: str | None = None

    model_config = {"from_attributes": True}


class ExceptionListOut(BaseModel):
    items: list[ExceptionOut]
    total: int
