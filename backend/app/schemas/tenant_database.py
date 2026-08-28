"""Pydantic models for the Bring-Your-Own-Database feature's Phase 1 —
/api/settings/database (org) and /api/seller/settings/database (Seller/
platform-level) — see services/tenant_database_service.py."""
from datetime import datetime

from pydantic import BaseModel


class TenantDatabaseOut(BaseModel):
    id: int
    organization_id: int | None
    connection_type: str
    config: dict  # secret fields masked — see tenant_database_service._masked_config
    status: str
    last_tested_at: datetime | None
    last_test_success: bool | None
    last_test_message: str | None
    secrets_rotated_at: datetime | None
    is_active: bool
    activated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UpdateTenantDatabaseRequest(BaseModel):
    connection_type: str  # POSTGRESQL | MYSQL | SQL_SERVER
    config: dict  # raw field values, secrets included in plaintext — split/encrypted server-side


class TestConnectionResultOut(BaseModel):
    success: bool
    message: str
    tested_at: datetime
    status: str
    latency_ms: float | None = None
    server_version: str | None = None


class DatabaseTypeCatalogueOut(BaseModel):
    schemas: dict  # connection_type -> {label, category, fields: [...]}
