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
    license_type: str
    max_users: int | None
    monthly_credits: int | None
    max_documents: int | None
    start_date: datetime | None
    expiry_date: datetime | None
    dependencies: list[str]
    dependencies_met: bool


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


class SetModuleOut(BaseModel):
    module: str
    enabled: bool
    license_type: str
    max_users: int | None
    monthly_credits: int | None
    max_documents: int | None
    start_date: datetime | None
    expiry_date: datetime | None
