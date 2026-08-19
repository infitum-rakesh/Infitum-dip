"""Pydantic models for /api/seller/licenses/* and /api/license/check (Phase 5)."""
from datetime import datetime

from pydantic import BaseModel


class LicenseStats(BaseModel):
    total: int
    active: int
    expiring: int
    expired: int
    suspended: int
    revoked: int


class LicenseListItem(BaseModel):
    id: int
    organization_id: int
    organization_name: str
    module: str
    module_label: str
    license_type: str
    start_date: datetime | None
    expiry_date: datetime | None
    max_users: int | None
    usage_limit: int | None
    status: str
    monthly_credits: int | None
    max_documents: int | None
    max_records: int | None
    storage_limit_gb: float | None


class LicenseListOut(BaseModel):
    items: list[LicenseListItem]
    total: int
    page: int
    page_size: int


class LicenseOut(BaseModel):
    id: int
    organization_id: int
    module: str
    license_type: str
    status: str
    max_users: int | None
    monthly_credits: int | None
    max_documents: int | None
    max_records: int | None
    storage_limit_gb: float | None
    start_date: datetime | None
    expiry_date: datetime | None

    model_config = {"from_attributes": True}


class CreateLicenseRequest(BaseModel):
    organization_id: int
    module: str
    license_type: str = "SUBSCRIPTION"
    start_date: datetime | None = None
    expiry_date: datetime | None = None
    max_users: int | None = None
    record_limit: int | None = None
    document_limit: int | None = None
    storage_limit_gb: float | None = None
    ai_credit_limit: int | None = None


class ExtendLicenseRequest(BaseModel):
    days: int = 30


class ChangeLicenseTypeRequest(BaseModel):
    license_type: str


class LicenseCheckOut(BaseModel):
    allowed: bool
    module: str
    reason: str
    expiresAt: datetime | None
    remainingQuota: int | None
