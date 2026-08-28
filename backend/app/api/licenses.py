"""Licenses router — /api/licenses/...

Administration -> Licenses: read-only for organization admins. Seller
controls purchased modules/subscription/limits/expiration (see
services/license_service.py docstring); org admins only control which of
their users get access to purchased modules (Administration -> Users).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.database.tenant_routing import get_tenant_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.licenses import (
    LicenseDashboardOut,
    LicenseStatusOut,
    LicenseUsageOut,
    ModuleLicenseListOut,
)
from app.services import license_service as svc

router = APIRouter(prefix="/licenses", tags=["licenses"])

_admin = require_action("ADMIN", module=None)


async def _get_org_or_404(db: AsyncSession, user: User) -> Organization:
    if not user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No organization is associated with this account.")
    result = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found.")
    return org


@router.get("", response_model=LicenseDashboardOut)
async def get_dashboard(user: User = Depends(_admin), db: AsyncSession = Depends(get_db), tenant_db: AsyncSession = Depends(get_tenant_db)):
    org = await _get_org_or_404(db, user)
    return await svc.build_dashboard(db, tenant_db, org)


@router.get("/modules", response_model=ModuleLicenseListOut)
async def get_modules(user: User = Depends(_admin), db: AsyncSession = Depends(get_db), tenant_db: AsyncSession = Depends(get_tenant_db)):
    org = await _get_org_or_404(db, user)
    return ModuleLicenseListOut(items=await svc.build_module_licenses(tenant_db, org))


@router.get("/usage", response_model=LicenseUsageOut)
async def get_usage(user: User = Depends(_admin), db: AsyncSession = Depends(get_db), tenant_db: AsyncSession = Depends(get_tenant_db)):
    org = await _get_org_or_404(db, user)
    return LicenseUsageOut(usage=await svc.build_usage(db, tenant_db, org))


@router.get("/status", response_model=LicenseStatusOut)
async def get_status(user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    org = await _get_org_or_404(db, user)
    return await svc.build_status(db, org)
