from datetime import datetime

from pydantic import BaseModel


class UsageMetric(BaseModel):
    used: int
    limit: int | None  # None = unlimited (enterprise) or not tracked for this resource


class LicenseDashboardOut(BaseModel):
    organization_id: int
    license_status: str
    plan: str
    plan_label: str
    start_date: datetime
    expiry_date: datetime | None
    days_remaining: int | None
    users_allowed: int
    users_used: int
    modules_purchased: int
    modules_total: int
    usage: dict[str, UsageMetric]  # e.g. {"users": {...}, "documents": {...}, "ai_calls": {...}, "storage_gb": {...}}


class ModuleLicenseOut(BaseModel):
    module: str
    purchased: bool
    enabled: bool
    usage: int | None
    limit: int | None
    expiry: datetime | None


class ModuleLicenseListOut(BaseModel):
    items: list[ModuleLicenseOut]


class LicenseUsageOut(BaseModel):
    usage: dict[str, UsageMetric]


class LicenseStatusOut(BaseModel):
    status: str
    is_trial: bool
    trial_start: datetime | None
    trial_end: datetime | None
    days_remaining: int | None
    usage_limits: dict[str, int | None]
