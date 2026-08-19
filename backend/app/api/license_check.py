"""
Platform-wide license validation — GET /api/license/check (SELLER PORTAL
spec, "LICENSE VALIDATION"). Deliberately NOT under /api/seller/* — this
is meant to be callable by any authenticated part of the app checking
whether a customer+user is allowed to use a module, not a Seller-only
admin surface. Real implementation in services/seller/license_service.py's
check_license(); this router is just tenant-isolation + wiring.

Not yet called from any existing require_action() site in this codebase
(that retrofit is Phase 10 "Integrate RBAC + licensing + organization
isolation" scope) — but every reason code and quota number it returns is
computed from real Organization/CustomerModule/Document data, not stubbed.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.database.connection import get_db
from app.models.user import User, UserRole
from app.schemas.seller.licenses import LicenseCheckOut
from app.services.seller import license_service as svc

router = APIRouter(prefix="/license", tags=["license-check"])


@router.get("/check", response_model=LicenseCheckOut)
async def check_license(
    customerId: int, module: str, userId: int | None = None,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    # Tenant isolation: only Seller/Super-Admin can check an arbitrary
    # customer; anyone else can only check their own organization — never
    # rely on the frontend to enforce this (spec section 11/12).
    if user.role not in (UserRole.SELLER, UserRole.SUPER_ADMIN) and user.organization_id != customerId:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You may only check licenses for your own organization.")

    result = await svc.check_license(db, customer_id=customerId, user_id=userId, module=module)
    return result
