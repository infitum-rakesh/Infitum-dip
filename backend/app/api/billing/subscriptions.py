"""
GET /api/billing/subscriptions, /api/billing/subscriptions/{id}/modules —
customer self-service, read-only view of the current org's OWN
subscriptions (spec section 45's Customer Billing screens, SB29).

Deliberately NOT /api/seller/subscriptions (that router is gated
require_seller_action — Seller/Super Admin only, see
api/seller/subscriptions.py). Org-scoped users get their own read-only
view here instead. Gated the same way as invoices.py/payments.py:
require_action("VIEW", module=None) — reachable regardless of
subscription state, since a customer needs to see their subscriptions
(and know why access was denied) even when everything's expired.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_organization, require_action
from app.database.connection import get_db
from app.models.customer_module import CustomerModule
from app.models.organization import Organization
from app.models.user import User
from app.schemas.seller.subscriptions import SubscriptionListOut, SubscriptionModuleOut, SubscriptionModulesOut
from app.services.seller import subscription_service as svc

router = APIRouter(prefix="/billing", tags=["billing"])

_view = require_action("VIEW", module=None)


def _require_customer_org(org: Organization | None) -> Organization:
    if org is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This endpoint is for customer organizations; Seller accounts use /api/seller/subscriptions.",
        )
    return org


@router.get("/subscriptions", response_model=SubscriptionListOut)
async def list_my_subscriptions(
    page: int = 1, page_size: int = 20,
    user: User = Depends(_view), org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    org = _require_customer_org(org)
    items, total = await svc.list_subscriptions(db, customer_id=org.id, page=page, page_size=page_size)
    return SubscriptionListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/subscriptions/{subscription_id}/modules", response_model=SubscriptionModulesOut)
async def get_my_subscription_modules(
    subscription_id: int, user: User = Depends(_view), org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    org = _require_customer_org(org)
    sub = await svc.get_subscription(db, subscription_id)
    if sub is None or sub.organization_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    result = await db.execute(select(CustomerModule).where(CustomerModule.subscription_id == subscription_id))
    items = [
        SubscriptionModuleOut(module=cm.module, status=cm.status, enabled=cm.enabled, expiry_date=cm.expiry_date)
        for cm in result.scalars().all()
    ]
    return SubscriptionModulesOut(subscription_id=subscription_id, items=items)
