"""Pydantic models for /api/seller/modules/* (Phase 4)."""
from datetime import datetime

from pydantic import BaseModel


class ModuleCatalogItem(BaseModel):
    module: str
    label: str
    category: str
    version: str
    available: bool
    customers: int
    license_type: str
    status: str
    dependencies: list[str]


class ModuleCatalogOut(BaseModel):
    items: list[ModuleCatalogItem]


class CustomerModuleItem(BaseModel):
    module: str
    label: str
    category: str
    purchased: bool
    enabled: bool
    status: str | None = None
    license_type: str
    max_users: int | None
    monthly_credits: int | None
    max_documents: int | None
    start_date: datetime | None
    expiry_date: datetime | None
    dependencies: list[str]
    dependencies_met: bool
    pending_invoice_id: int | None = None
    # AI Connection Subscription (AIC-P4) — the Seller-controlled entitlement
    # limits for the AI_CONNECTION module specifically; meaningless/None for
    # every other module, same additive convention as every other column on
    # this row (see models/customer_module.py's docstring).
    max_tokens: int | None = None
    token_period: str | None = None
    ai_provider_bundle: list[str] | None = None
    ai_rate_limit: int | None = None
    ai_concurrency_limit: int | None = None


class CustomerModulesOut(BaseModel):
    organization_id: int
    items: list[CustomerModuleItem]


class SetModuleRequest(BaseModel):
    enabled: bool
    license_type: str | None = None
    max_users: int | None = None
    monthly_credits: int | None = None
    max_documents: int | None = None
    expiry_date: datetime | None = None
    max_tokens: int | None = None
    token_period: str | None = None
    ai_provider_bundle: list[str] | None = None
    ai_rate_limit: int | None = None
    ai_concurrency_limit: int | None = None


class SetModuleOut(BaseModel):
    module: str
    enabled: bool
    status: str
    license_type: str
    max_users: int | None
    monthly_credits: int | None
    max_documents: int | None
    start_date: datetime | None
    expiry_date: datetime | None
    pending_invoice_id: int | None = None
    max_tokens: int | None = None
    token_period: str | None = None
    ai_provider_bundle: list[str] | None = None
    ai_rate_limit: int | None = None
    ai_concurrency_limit: int | None = None


class ModulePriceItem(BaseModel):
    module: str
    label: str
    category: str
    price: float
    currency: str


class ModulePriceListOut(BaseModel):
    items: list[ModulePriceItem]


class SetModulePriceRequest(BaseModel):
    price: float
    currency: str = "USD"


class SetModulePriceOut(BaseModel):
    module: str
    price: float
    currency: str
