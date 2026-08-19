from datetime import datetime
from typing import Any

from pydantic import BaseModel


class QueryCreateRequest(BaseModel):
    name: str
    connection_id: int
    schema_name: str | None = None
    sql_text: str
    is_shared: bool = False


class QueryUpdateRequest(BaseModel):
    name: str | None = None
    sql_text: str | None = None
    is_shared: bool | None = None


class QueryOut(BaseModel):
    id: int
    name: str
    connection_id: int
    schema_name: str | None
    sql_text: str
    is_shared: bool
    is_favorite: bool = False
    created_by: int | None
    created_by_name: str | None = None
    updated_by: int | None
    updated_by_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class QueryListOut(BaseModel):
    items: list[QueryOut]
    total: int


class FavoriteToggleOut(BaseModel):
    is_favorite: bool


class RunSqlRequest(BaseModel):
    sql: str
    parameters: dict[str, Any] = {}
    limit: int | None = None
    source: str = "MANUAL"  # "MANUAL" | "AI_GENERATED" — provenance tag for the execution log; invalid values fall back to MANUAL server-side
    client_run_id: str | None = None  # client-generated id (e.g. crypto.randomUUID()) — lets a later /cancel call target this specific in-flight run
    timeout_seconds: int | None = None  # real server-side cutoff on PostgreSQL/MySQL (see connectors' supports_query_timeout); clamped server-side, no-op elsewhere


class CancelRunRequest(BaseModel):
    client_run_id: str


class CancelRunOut(BaseModel):
    cancelled: bool
    message: str


class RunSqlResponse(BaseModel):
    is_result_set: bool
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated: bool = False
    rowcount: int | None = None
    elapsed_ms: float


class ExplainRequest(BaseModel):
    sql: str
    parameters: dict[str, Any] = {}


class ExplainResponse(BaseModel):
    plan_text: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    elapsed_ms: float


class AiGenerateSqlRequest(BaseModel):
    ai_connection_id: int
    question: str
    schema_name: str | None = None
    current_sql: str | None = None


class AiGenerateSqlResponse(BaseModel):
    sql: str
    explanation: str
    warnings: list[str] = []
    statement_command: str
    statement_category: str
    model: str
    elapsed_ms: float


class HistoryEntryOut(BaseModel):
    id: int
    connection_id: int
    connection_name: str | None = None
    user_id: int | None
    user_name: str | None = None
    sql_text: str
    statement_command: str
    statement_category: str
    source: str
    status: str
    row_count: int | None
    duration_ms: float | None
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class HistoryListOut(BaseModel):
    items: list[HistoryEntryOut]
    total: int


class SqlPermissionsOut(BaseModel):
    actions: list[str]  # the caller's real, effective SQL Workspace action set — see role_service.effective_sql_permissions


class ExportCreateRequest(BaseModel):
    sql: str
    format: str  # CSV | JSON | XLSX


class ExportJobOut(BaseModel):
    id: int
    connection_id: int
    format: str
    status: str
    row_count: int
    file_size_bytes: int | None = None
    error_message: str | None = None
    created_by: int | None = None
    created_by_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExportJobListOut(BaseModel):
    items: list[ExportJobOut]
    total: int


class ExportCancelOut(BaseModel):
    cancelled: bool
    message: str
