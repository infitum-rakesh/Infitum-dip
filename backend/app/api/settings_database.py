"""Settings -> Database router — /api/settings/database (BYO-Database
Phase 1). Lets an Org Admin configure and real-test-connect the backend
database this organization wants to bring for its own control-plane data.
Phase 1 only: saving/testing here has no effect yet on which database
actually serves this org's requests — see services/tenant_database_
service.py's module docstring and the Phase 2+ plan for the migration/
cutover engine that will eventually read TenantDatabase.is_active.

Same ADMIN-tier gate as api/settings.py — Database is presented as a
sub-section of Settings on the frontend, not a separately licensed module.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.tenant_database import (
    DatabaseTypeCatalogueOut,
    TenantDatabaseOut,
    TestConnectionResultOut,
    UpdateTenantDatabaseRequest,
)
from app.schemas.tenant_migration import TenantMigrationJobListOut, TenantMigrationJobLogsOut, TenantMigrationJobOut
from app.services import tenant_database_service as svc
from app.services import tenant_migration_service as migration_svc

router = APIRouter(prefix="/settings/database", tags=["settings-database"])

_admin = require_action("ADMIN", module=None)


async def _get_org_or_404(db: AsyncSession, user: User) -> Organization:
    if not user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No organization is associated with this account.")
    result = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found.")
    return org


@router.get("/types", response_model=DatabaseTypeCatalogueOut)
async def get_type_catalogue(user: User = Depends(_admin)):
    return DatabaseTypeCatalogueOut(schemas=svc.type_catalogue())


@router.get("", response_model=TenantDatabaseOut | None)
async def get_database(user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    org = await _get_org_or_404(db, user)
    row = await svc.get_row(db, org.id)
    return svc.to_out(row)


@router.put("", response_model=TenantDatabaseOut)
async def update_database(
    body: UpdateTenantDatabaseRequest, request: Request,
    user: User = Depends(_admin), db: AsyncSession = Depends(get_db),
):
    org = await _get_org_or_404(db, user)
    try:
        row = await svc.update_config(
            db, organization_id=org.id, connection_type=body.connection_type, raw_config=body.config,
            actor_id=user.id, ip_address=request.client.host if request.client else None,
        )
    except svc.TenantDatabaseValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return svc.to_out(row)


@router.post("/test", response_model=TestConnectionResultOut)
async def test_database(
    request: Request, user: User = Depends(_admin), db: AsyncSession = Depends(get_db),
):
    org = await _get_org_or_404(db, user)
    row = await svc.get_row(db, org.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No backend database is configured yet — save one first.")
    result = await svc.test_connection(db, row, actor_id=user.id, ip_address=request.client.host if request.client else None)
    return TestConnectionResultOut(**result)


# ── Phase 4 — migration/cutover ─────────────────────────────────────

@router.post("/migrate", response_model=TenantMigrationJobOut, status_code=status.HTTP_201_CREATED)
async def start_migration(
    request: Request, user: User = Depends(_admin), db: AsyncSession = Depends(get_db),
):
    org = await _get_org_or_404(db, user)
    try:
        job = await migration_svc.start_migration(
            db, organization_id=org.id, actor_id=user.id, ip_address=request.client.host if request.client else None,
        )
    except migration_svc.TenantMigrationValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return job


@router.get("/migrations", response_model=TenantMigrationJobListOut)
async def list_migrations(user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    org = await _get_org_or_404(db, user)
    items = await migration_svc.list_jobs(db, org.id)
    return TenantMigrationJobListOut(items=items)


@router.get("/migrations/{job_id}", response_model=TenantMigrationJobOut)
async def get_migration(job_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    org = await _get_org_or_404(db, user)
    job = await migration_svc.get_job(db, job_id, org.id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Migration job not found.")
    return job


@router.get("/migrations/{job_id}/logs", response_model=TenantMigrationJobLogsOut)
async def get_migration_logs(
    job_id: int, since_id: int | None = Query(None), user: User = Depends(_admin), db: AsyncSession = Depends(get_db),
):
    org = await _get_org_or_404(db, user)
    job = await migration_svc.get_job(db, job_id, org.id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Migration job not found.")
    logs = await migration_svc.get_logs(db, job_id, since_id)
    return TenantMigrationJobLogsOut(items=logs, job_status=job.status.value)
