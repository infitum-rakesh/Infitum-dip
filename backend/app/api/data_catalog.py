"""Data Catalog router — /api/data-catalog/...

Real discovery endpoints over live Connections + Documents — see
services/data_catalog_service.py for exactly what's real vs. disclosed
as not tracked. Write operations (tagging, edits) are intentionally out
of scope for this phase — see DATA_CATALOG_EDIT / DATA_CATALOG_CREATE /
DATA_CATALOG_DELETE permission notes in the frontend PermissionGate usage.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.services import data_catalog_service as svc

router = APIRouter(prefix="/data-catalog", tags=["data-catalog"])

_view = require_action("VIEW", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found(asset_id: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset '{asset_id}' not found")


@router.get("/stats")
async def stats(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """KPI header: total assets, datasets, tables, files, quality score, recent assets."""
    return await svc.get_stats(db, _effective_org_id(user))


@router.get("/facets")
async def facets(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """Distinct filter values for the catalog filter panel."""
    return await svc.get_facets(db, _effective_org_id(user))


@router.get("/assets")
async def list_assets(
    q: str = Query("", description="Free-text search across name/schema/database/owner/source/tags"),
    source_type: str | None = Query(None),
    environment: str | None = Query(None),
    database: str | None = Query(None),
    schema: str | None = Query(None),
    asset_type: str | None = Query(None),
    owner: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    quality: str | None = Query(None, description="high | medium | low"),
    tag: str | None = Query(None),
    source: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("last_updated"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    return await svc.list_assets(
        db, _effective_org_id(user),
        search=q, source_type=source_type, environment=environment, database=database, schema=schema,
        asset_type=asset_type, owner=owner, status=status_filter, quality=quality, tag=tag, source=source,
        page=page, page_size=page_size, sort_by=sort_by, sort_dir=sort_dir,
    )


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    asset = await svc.get_asset(db, asset_id, _effective_org_id(user))
    if asset is None:
        raise _not_found(asset_id)
    return asset


@router.get("/assets/{asset_id}/columns")
async def get_columns(asset_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    columns = await svc.get_columns(db, asset_id, _effective_org_id(user))
    if columns is None:
        raise _not_found(asset_id)
    return {"asset_id": asset_id, "columns": columns}


@router.get("/assets/{asset_id}/profile")
async def get_profile(asset_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    profile = await svc.get_profile(db, asset_id, _effective_org_id(user))
    if profile is None:
        raise _not_found(asset_id)
    return profile


@router.get("/assets/{asset_id}/lineage")
async def get_lineage(asset_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    lineage = await svc.get_lineage(db, asset_id, _effective_org_id(user))
    if lineage is None:
        raise _not_found(asset_id)
    return lineage


@router.get("/assets/{asset_id}/quality")
async def get_quality(asset_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    quality = await svc.get_quality(db, asset_id, _effective_org_id(user))
    if quality is None:
        raise _not_found(asset_id)
    return quality


@router.get("/assets/{asset_id}/incidents")
async def get_incidents(asset_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    incidents = await svc.get_incidents(db, asset_id, _effective_org_id(user))
    if incidents is None:
        raise _not_found(asset_id)
    return {"asset_id": asset_id, "incidents": incidents}


@router.get("/assets/{asset_id}/usage")
async def get_usage(asset_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    usage = await svc.get_usage(db, asset_id, _effective_org_id(user))
    if usage is None:
        raise _not_found(asset_id)
    return usage
