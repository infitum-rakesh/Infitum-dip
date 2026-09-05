from datetime import datetime

from pydantic import BaseModel, field_validator


class UsageMetric(BaseModel):
    # int | float, not just int: shared across users/documents/ai_calls
    # (genuine whole-number counts) AND storage_gb, whose real value
    # (services/license_service.py::_storage_used_gb — real bytes summed
    # from Document rows, /1e9, round(..., 3)) is a real fraction of a GB
    # for any org with actual documents — 0 is the only value that's ever
    # coincidentally a whole number. Confirmed live: a real org (documents
    # totalling ~4MB) 500'd on this endpoint with a Pydantic
    # int_from_float error before this fix.
    used: int | float
    limit: int | float | None  # None = unlimited (enterprise) or not tracked for this resource


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


class ContactSalesRequest(BaseModel):
    # Which module(s) prompted the inquiry, e.g. "SQL_WORKSPACE" or
    # "Not sure / general inquiry" — free text so the frontend's picker
    # (including modules the org doesn't have yet) never gets out of sync
    # with ALL_MODULES on the backend.
    module_interest: str | None = None
    contact_name: str
    contact_email: str
    contact_phone: str | None = None
    message: str

    @field_validator("contact_name", "contact_email", "message")
    @classmethod
    def _required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("This field is required.")
        return v.strip()


class ContactSalesResult(BaseModel):
    delivered: bool
    detail: str
