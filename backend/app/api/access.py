"""
GET /api/access/check, /api/access/modules-summary — real-time,
multi-subscription-aware access endpoints (Multi-Subscription Billing &
Payments Platform, 2026, spec sections 40/56). Backs the frontend route
guard and the Dashboard's Active/Expiring/Expired widgets.

IMPORTANT (spec section 40): this endpoint is a UX convenience, not the
security boundary — the real enforcement already happens inside
core/deps.require_action()/api/sql_workspace.py's require_sql_action()
on every protected route (see SB5). A client could skip calling this
entirely and every actual API call would still be correctly gated.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.core.deps import get_current_organization, get_current_user
from app.database.connection import get_db
from app.models.organization import Organization
from app.models.seller import Seller
from app.models.user import User
from app.schemas.access import AccessCheckOut, ExpiringModuleOut, ModulesSummaryOut, SalesContactOut
from app.services.billing.access_service import access_service
from app.services.billing.messaging import build_access_message
from app.services.license_service import ALL_MODULES

router = APIRouter(prefix="/access", tags=["access"])


@router.get("/check", response_model=AccessCheckOut)
async def check_access(
    module: str = Query(..., description="Module key, e.g. OCR, SQL_WORKSPACE"),
    user: User = Depends(get_current_user),
    org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
) -> AccessCheckOut:
    if module not in ALL_MODULES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown module: {module}")
    result = await access_service.check_module_access(
        db, organization_id=org.id if org else None, user=user, module=module
    )
    return AccessCheckOut(
        granted=result.granted,
        reason=result.reason.value if result.reason else None,
        module=module,
        expired_at=result.expired_at,
        covered_by_other_subscription=result.covered_by_other_subscription,
        message=build_access_message(module, result),
    )


@router.get("/modules-summary", response_model=ModulesSummaryOut)
async def modules_summary(
    org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
) -> ModulesSummaryOut:
    if org is None:
        # Seller/Super Admin — no organization, nothing to summarize.
        return ModulesSummaryOut(active_modules=[], expired_modules={}, expiring_modules=[], has_tenant_access=True)
    active = await access_service.get_active_modules(db, org.id)
    expired = await access_service.get_expired_modules(db, org.id)
    expiring = await access_service.get_expiring_modules(db, org.id)
    has_access = await access_service.has_tenant_access(db, org.id)
    return ModulesSummaryOut(
        active_modules=sorted(active),
        expired_modules=expired,
        expiring_modules=[ExpiringModuleOut(**m) for m in expiring],
        has_tenant_access=has_access,
    )


@router.get("/contact", response_model=SalesContactOut)
async def sales_contact(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> SalesContactOut:
    """SB30 — the real Sales contact for the "Contact Sales" action on
    SubscriptionExpiredNotice / the all-expired block screen. Falls back
    to support_email when sales_email hasn't been configured, rather than
    showing nothing when the Seller only ever set up general support."""
    seller_row = (await db.execute(select(Seller).limit(1))).scalar_one_or_none()
    if seller_row is None:
        return SalesContactOut()
    return SalesContactOut(
        name=seller_row.sales_contact_name,
        email=seller_row.sales_email or seller_row.support_email,
        phone=seller_row.sales_phone,
        url=seller_row.sales_url,
    )
