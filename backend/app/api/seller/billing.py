"""Seller Portal -> Billing Dashboard — GET /api/seller/billing/dashboard (SB25)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.billing.billing_dashboard import BillingDashboardOut
from app.services.billing import billing_dashboard_service

router = APIRouter(prefix="/seller/billing", tags=["seller-billing"])

_view = require_seller_action("VIEW", "Billing")


@router.get("/dashboard", response_model=BillingDashboardOut)
async def get_billing_dashboard(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await billing_dashboard_service.build_billing_dashboard(db)
