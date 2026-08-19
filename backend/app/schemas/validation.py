from datetime import datetime
from typing import Any

from pydantic import BaseModel


class RuleSetCreateRequest(BaseModel):
    name: str
    connection_id: int
    schema_name: str | None = None
    object_name: str
    rules: list[dict] = []


class RuleSetUpdateRequest(BaseModel):
    name: str | None = None
    rules: list[dict] | None = None


class RuleSetOut(BaseModel):
    id: int
    name: str
    connection_id: int
    schema_name: str | None
    object_name: str
    rules: list[dict]
    created_by: int | None
    created_by_name: str | None = None
    updated_by: int | None
    updated_by_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RuleSetListOut(BaseModel):
    items: list[RuleSetOut]
    total: int


class RunRuleSetRequest(BaseModel):
    pass


class RuleResultOut(BaseModel):
    rule_id: str
    rule_name: str
    rule_type: str
    columns: list[str]
    severity: str
    status: str
    pass_count: int
    fail_count: int
    pass_rate: float

    model_config = {"from_attributes": True}


class ValidationRunOut(BaseModel):
    id: int
    rule_set_id: int
    status: str
    total_records: int | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    results: list[RuleResultOut] = []

    model_config = {"from_attributes": True}


class FailedRecordsOut(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
