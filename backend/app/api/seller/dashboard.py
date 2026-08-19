"""Seller Portal router — GET /api/seller/dashboard (Phase 1)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.seller.dashboard import SellerDashboardOut
from app.services.seller import dashboard_service

router = APIRouter(prefix="/seller", tags=["seller"])

_view_dashboard = require_seller_action("VIEW", "Dashboard")


@router.get("/dashboard", response_model=SellerDashboardOut)
async def get_dashboard(user: User = Depends(_view_dashboard), db: AsyncSession = Depends(get_db)):
    return await dashboard_service.build_dashboard(db)
