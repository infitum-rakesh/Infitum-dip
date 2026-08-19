from datetime import datetime

from pydantic import BaseModel


class JobCreateRequest(BaseModel):
    name: str
    source_connection_id: int
    source_schema: str | None = None
    source_object: str
    target_connection_id: int
    target_schema: str | None = None
    target_object: str
    key_columns: list[str] = []
    compare_columns: list[str] = []
    comparison_types: list[str] = ["COUNT"]  # subset of COUNT/CHECKSUM/KEY_MATCH/FIELD_LEVEL


class JobUpdateRequest(BaseModel):
    name: str | None = None
    key_columns: list[str] | None = None
    compare_columns: list[str] | None = None
    comparison_types: list[str] | None = None


class JobOut(BaseModel):
    id: int
    name: str
    source_connection_id: int
    source_schema: str | None
    source_object: str
    target_connection_id: int
    target_schema: str | None
    target_object: str
    key_columns: list[str]
    compare_columns: list[str]
    comparison_types: list[str]
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

    model_config = {"from_attributes": True}


class ExceptionListOut(BaseModel):
    items: list[ExceptionOut]
    total: int
