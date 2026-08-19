"""Pydantic models for /api/seller/settings (Phase 9). Wraps the singleton
Seller row — see models/seller.py + services/seller/seller_settings_service.py."""
from datetime import datetime

from pydantic import BaseModel


class SellerBrandingOut(BaseModel):
    logo_url: str | None = None
    primary_color: str | None = None


class SellerSettingsOut(BaseModel):
    id: int
    name: str
    legal_name: str | None
    support_email: str | None
    billing_email: str | None
    website: str | None
    branding: dict
    default_trial_days: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UpdateSellerSettingsRequest(BaseModel):
    name: str | None = None
    legal_name: str | None = None
    support_email: str | None = None
    billing_email: str | None = None
    website: str | None = None
    branding: dict | None = None
    default_trial_days: int | None = None
