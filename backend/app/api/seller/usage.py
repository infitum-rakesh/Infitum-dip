"""Seller Portal -> Usage router — /api/seller/usage/* (Phase 6).

Filters accepted: customer_id, module, plan — all real dimensions of the
underlying data. No `environment` filter — see services/seller/
usage_service.py's module docstring for why (no usage-generating table in
this codebase carries that column)."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.seller.usage import CustomerUsageOut, PlatformUsageKpis, UsageChartsOut, UsageTableOut
from app.services.seller import usage_service as svc

router = APIRouter(prefix="/seller/usage", tags=["seller-usage"])

_view = require_seller_action("VIEW", "Usage")


@router.get("/kpis", response_model=PlatformUsageKpis)
async def get_kpis(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await svc.get_platform_kpis(db)


@router.get("/table", response_model=UsageTableOut)
async def get_table(
    customer_id: int | None = None, module: str | None = None, plan: str | None = None,
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    items = await svc.get_usage_table(db, customer_id=customer_id, module=module, plan=plan)
    return UsageTableOut(items=items)


@router.get("/charts", response_model=UsageChartsOut)
async def get_charts(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await svc.get_charts(db)


@router.get("/customers/{customer_id}", response_model=CustomerUsageOut)
async def get_customer_usage(customer_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    result = await svc.get_customer_usage(db, customer_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    return result
