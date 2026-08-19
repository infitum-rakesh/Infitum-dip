"""Seller Portal -> Plans + Subscriptions router (Phase 3).

Two resource families in one file on purpose — matches the spec's own
services/api file layout (no separate plan_service.py/plans.py listed;
Plan CRUD lives alongside Subscription lifecycle since plans only exist to
back subscriptions in this build)."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.plan import Plan
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.seller.subscriptions import (
    ChangePlanRequest,
    CreatePlanRequest,
    CreateSubscriptionRequest,
    PlanListOut,
    PlanOut,
    SubscriptionListOut,
    SubscriptionOut,
    UpdatePlanRequest,
    UpdateSubscriptionRequest,
)
from app.services.seller import subscription_service as svc

router = APIRouter(prefix="/seller", tags=["seller-subscriptions"])

_view = require_seller_action("VIEW", "Subscriptions")
_create = require_seller_action("CREATE", "Subscriptions")
_edit = require_seller_action("EDIT", "Subscriptions")


async def _get_sub_or_404(db: AsyncSession, subscription_id: int) -> Subscription:
    sub = await svc.get_subscription(db, subscription_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    return sub


async def _get_plan_or_404(db: AsyncSession, plan_id: int) -> Plan:
    plan = await svc.get_plan(db, plan_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    return plan


# ── Plans ────────────────────────────────────────────────────────

@router.get("/plans", response_model=PlanListOut)
async def list_plans(include_inactive: bool = True, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    plans = await svc.list_plans(db, include_inactive=include_inactive)
    return PlanListOut(items=plans)


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(body: CreatePlanRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
    try:
        return await svc.create_plan(db, body=body, actor=user)
    except svc.SubscriptionServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.put("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(plan_id: int, body: UpdatePlanRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    plan = await _get_plan_or_404(db, plan_id)
    return await svc.update_plan(db, plan, body=body, actor=user)


# ── Subscriptions ────────────────────────────────────────────────

@router.get("/subscriptions", response_model=SubscriptionListOut)
async def list_subscriptions(
    customer_id: int | None = None, status_filter: str | None = None, q: str = "",
    page: int = 1, page_size: int = 20,
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    items, total = await svc.list_subscriptions(db, customer_id=customer_id, status_filter=status_filter, search=q, page=page, page_size=page_size)
    return SubscriptionListOut(items=items, total=total, page=page, page_size=page_size)


@router.post("/subscriptions", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
async def create_subscription(body: CreateSubscriptionRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
    try:
        return await svc.create_subscription(db, body=body, actor=user)
    except svc.SubscriptionServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def get_subscription(subscription_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await _get_sub_or_404(db, subscription_id)


@router.put("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def update_subscription(subscription_id: int, body: UpdateSubscriptionRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    sub = await _get_sub_or_404(db, subscription_id)
    return await svc.update_subscription(db, sub, body=body, actor=user)


@router.post("/subscriptions/{subscription_id}/renew", response_model=SubscriptionOut)
async def renew_subscription(subscription_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    sub = await _get_sub_or_404(db, subscription_id)
    return await svc.renew_subscription(db, sub, actor=user)


@router.post("/subscriptions/{subscription_id}/change-plan", response_model=SubscriptionOut)
async def change_plan(subscription_id: int, body: ChangePlanRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    sub = await _get_sub_or_404(db, subscription_id)
    try:
        return await svc.change_plan(db, sub, new_plan_id=body.plan_id, actor=user)
    except svc.SubscriptionServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/subscriptions/{subscription_id}/suspend", response_model=SubscriptionOut)
async def suspend_subscription(subscription_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    sub = await _get_sub_or_404(db, subscription_id)
    return await svc.suspend_subscription(db, sub, actor=user)


@router.post("/subscriptions/{subscription_id}/resume", response_model=SubscriptionOut)
async def resume_subscription(subscription_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    sub = await _get_sub_or_404(db, subscription_id)
    return await svc.resume_subscription(db, sub, actor=user)


@router.post("/subscriptions/{subscription_id}/cancel", response_model=SubscriptionOut)
async def cancel_subscription(subscription_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    sub = await _get_sub_or_404(db, subscription_id)
    return await svc.cancel_subscription(db, sub, actor=user)
