"""Pydantic models for Bring-Your-Own-Database Phase 4 — the migration/
cutover engine (services/tenant_migration_service.py). Mirrors
schemas/pipeline_jobs.py's shape (job + append-only logs, polled live)."""
from datetime import datetime

from pydantic import BaseModel


class TenantMigrationJobOut(BaseModel):
    id: int
    organization_id: int | None
    tenant_database_id: int
    status: str
    tables_total: int
    tables_done: int
    current_table: str | None
    rows_copied: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_by: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantMigrationJobListOut(BaseModel):
    items: list[TenantMigrationJobOut]


class TenantMigrationJobLogOut(BaseModel):
    id: int
    ts: datetime
    level: str
    message: str

    model_config = {"from_attributes": True}


class TenantMigrationJobLogsOut(BaseModel):
    items: list[TenantMigrationJobLogOut]
    job_status: str
