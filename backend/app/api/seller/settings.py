"""Seller Portal -> Seller Settings router — /api/seller/settings
(Phase 9). Company/Branding/Default License Settings on the singleton
Seller row — see services/seller/seller_settings_service.py. Plan CRUD
lives at /api/seller/plans (api/seller/subscriptions.py, built Phase 3)
— the Settings page's Plans tab calls that router directly."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.seller.settings import (
    SellerSettingsOut,
    SendTestEmailRequest,
    UpdateSellerSettingsRequest,
    UpdateSmtpConfigRequest,
)
from app.schemas.tenant_database import (
    DatabaseTypeCatalogueOut,
    TenantDatabaseOut,
    TestConnectionResultOut,
    UpdateTenantDatabaseRequest,
)
from app.schemas.tenant_migration import TenantMigrationJobListOut, TenantMigrationJobLogsOut, TenantMigrationJobOut
from app.services import email_service
from app.services import tenant_database_service as db_svc
from app.services import tenant_migration_service as migration_svc
from app.services.seller import seller_settings_service as svc

router = APIRouter(prefix="/seller/settings", tags=["seller-settings"])

_view = require_seller_action("VIEW", "Settings")
_edit = require_seller_action("EDIT", "Settings")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("", response_model=SellerSettingsOut)
async def get_settings(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    seller = await svc.get_settings(db)
    return svc.to_out(seller)


@router.put("", response_model=SellerSettingsOut)
async def update_settings(body: UpdateSellerSettingsRequest, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    seller = await svc.get_settings(db)
    seller = await svc.update_settings(db, seller, body, user, _client_ip(request))
    return svc.to_out(seller)


@router.put("/smtp", response_model=SellerSettingsOut)
async def update_smtp(body: UpdateSmtpConfigRequest, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    seller = await svc.get_settings(db)
    seller = await svc.update_smtp_config(db, seller, body, user, _client_ip(request))
    return svc.to_out(seller)


@router.post("/smtp/test")
async def send_test(body: SendTestEmailRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    result = await email_service.send_test_email(db, to=body.to)
    return result.to_dict()


# ── Database (Bring-Your-Own-Database Phase 1) ─────────────────────
# Platform-level backend database — the same feature as the Organization
# Settings "Database" tab (api/settings_database.py), scoped to
# organization_id = NULL (the Seller/platform-wide row) via
# tenant_database_service's shared organization_id-is-None convention.
# See that module's docstring for why saving/testing here doesn't yet move
# any real traffic off the platform's current SQLite database (Phase 1).

@router.get("/database/types", response_model=DatabaseTypeCatalogueOut)
async def get_database_type_catalogue(user: User = Depends(_view)):
    return DatabaseTypeCatalogueOut(schemas=db_svc.type_catalogue())


@router.get("/database", response_model=TenantDatabaseOut | None)
async def get_database(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    row = await db_svc.get_row(db, None)
    return db_svc.to_out(row)


@router.put("/database", response_model=TenantDatabaseOut)
async def update_database(body: UpdateTenantDatabaseRequest, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    try:
        row = await db_svc.update_config(
            db, organization_id=None, connection_type=body.connection_type, raw_config=body.config,
            actor_id=user.id, ip_address=request.client.host if request.client else None,
        )
    except db_svc.TenantDatabaseValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return db_svc.to_out(row)


@router.post("/database/test", response_model=TestConnectionResultOut)
async def test_database(request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    row = await db_svc.get_row(db, None)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No backend database is configured yet — save one first.")
    result = await db_svc.test_connection(db, row, actor_id=user.id, ip_address=request.client.host if request.client else None)
    return TestConnectionResultOut(**result)


# ── Phase 6 — platform-level migration/cutover ──────────────────────
# The Seller's counterpart to api/settings_database.py's /migrate,
# /migrations, /migrations/{id}, /migrations/{id}/logs — same service
# (tenant_migration_service.py), scoped to organization_id=None (the
# platform-wide row). This is what actually moves the whole platform's
# remaining shared data off SQLite (or wherever it lives today) onto the
# database configured/tested just above: every org WITHOUT its own
# individual override starts routing to it automatically once the
# cutover completes — see tenant_engine_registry.get_sessionmaker()'s
# fallback cascade. Gated EDIT (start a migration is a real, impactful
# action) vs VIEW (read-only progress/history) exactly like the config
# endpoints above.

@router.post("/database/migrate", response_model=TenantMigrationJobOut, status_code=status.HTTP_201_CREATED)
async def start_platform_migration(request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    try:
        job = await migration_svc.start_migration(
            db, organization_id=None, actor_id=user.id, ip_address=request.client.host if request.client else None,
        )
    except migration_svc.TenantMigrationValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return job


@router.get("/database/migrations", response_model=TenantMigrationJobListOut)
async def list_platform_migrations(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    items = await migration_svc.list_jobs(db, None)
    return TenantMigrationJobListOut(items=items)


@router.get("/database/migrations/{job_id}", response_model=TenantMigrationJobOut)
async def get_platform_migration(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    job = await migration_svc.get_job(db, job_id, None)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Migration job not found.")
    return job


@router.get("/database/migrations/{job_id}/logs", response_model=TenantMigrationJobLogsOut)
async def get_platform_migration_logs(
    job_id: int, since_id: int | None = Query(None), user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    job = await migration_svc.get_job(db, job_id, None)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Migration job not found.")
    logs = await migration_svc.get_logs(db, job_id, since_id)
    return TenantMigrationJobLogsOut(items=logs, job_status=job.status.value)
