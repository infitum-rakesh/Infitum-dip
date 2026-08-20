"""Seller Portal -> Seller Settings router — /api/seller/settings
(Phase 9). Company/Branding/Default License Settings on the singleton
Seller row — see services/seller/seller_settings_service.py. Plan CRUD
lives at /api/seller/plans (api/seller/subscriptions.py, built Phase 3)
— the Settings page's Plans tab calls that router directly."""
from fastapi import APIRouter, Depends, Request
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
from app.services import email_service
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
