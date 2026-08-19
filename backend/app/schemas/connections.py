from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Responses ────────────────────────────────────────────────────

class ConnectionOut(BaseModel):
    id: int
    name: str
    category: str
    connection_type: str
    environment: str
    config: dict[str, Any]  # secret fields already masked — see connection_service._masked_config
    status: str
    host_display: str | None = None
    database_display: str | None = None
    owner_id: int | None
    owner_name: str | None = None
    last_tested_at: datetime | None
    last_test_success: bool | None
    last_test_message: str | None
    secrets_rotated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConnectionListOut(BaseModel):
    items: list[ConnectionOut]
    total: int
    page: int
    page_size: int


class ConnectionStatsOut(BaseModel):
    total: int
    active: int
    inactive: int
    errors: int
    recently_tested: int  # tested within the last 24h


class TestConnectionResult(BaseModel):
    success: bool
    message: str
    tested_at: datetime
    status: str
    latency_ms: float | None = None
    server_version: str | None = None


class ConnectionTypeSchemaOut(BaseModel):
    """Field-schema catalogue — single source of truth the frontend's
    New Connection / dynamic form reads from, so form definitions never
    drift between backend validation and frontend rendering."""
    categories: list[dict[str, Any]]  # [{key, label, types: [{key, label}]}]
    schemas: dict[str, Any]           # {connection_type: {label, category, fields: [...]}}


# ── Requests ─────────────────────────────────────────────────────

class ConnectionCreateRequest(BaseModel):
    name: str
    category: str
    connection_type: str
    environment: str = "DEVELOPMENT"
    config: dict[str, Any] = {}  # may include plaintext secret values — split/encrypted server-side


class ConnectionUpdateRequest(BaseModel):
    name: str | None = None
    environment: str | None = None
    config: dict[str, Any] | None = None  # only include secret keys the user actually changed


# ── Schema/object browsing — Source Dataset node (Phase 2) ────────

class SchemaListOut(BaseModel):
    schemas: list[str]


class ObjectOut(BaseModel):
    name: str
    schema_name: str | None = None
    object_type: str  # "TABLE" | "VIEW"


class ObjectListOut(BaseModel):
    items: list[ObjectOut]


class ColumnOut(BaseModel):
    name: str
    data_type: str
    nullable: bool
    primary_key: bool = False
    unique: bool = False
    indexed: bool = False
    default: str | None = None
    foreign_key: dict | None = None  # {"table", "column", "schema"} when real — see connectors/base.py ColumnInfo


class ColumnListOut(BaseModel):
    columns: list[ColumnOut]
    row_count: int | None = None  # None when not requested or not cheaply available


class PreviewRequest(BaseModel):
    """Exactly one of (schema_name + object_name) or sql must be given —
    browse-and-preview an existing object, or preview a custom SELECT.
    `parameters` are named binds substituted safely via SQLAlchemy's
    text()/execute(params) — never string-concatenated into `sql`."""
    schema_name: str | None = None
    object_name: str | None = None
    sql: str | None = None
    parameters: dict[str, Any] = {}
    limit: int = 100


class PreviewOut(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int | None = None
    truncated: bool = False


class ApiPreviewRequest(BaseModel):
    """Node-level REST source config — see connectors/api/rest.py's
    docstring for the pagination dict's shape (style/page_size/etc.)."""
    endpoint: str
    method: str = "GET"
    query_params: dict[str, Any] = {}
    headers: dict[str, str] = {}
    body: dict[str, Any] | None = None
    json_path: str | None = None
    pagination: dict[str, Any] = {"style": "none"}
    limit: int = 100
