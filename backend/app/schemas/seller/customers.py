"""Pydantic models for /api/seller/customers/*."""
from datetime import datetime

from pydantic import BaseModel, EmailStr


class CustomerStats(BaseModel):
    total: int
    active: int
    trial: int
    suspended: int
    expired: int


class CustomerListItem(BaseModel):
    id: int
    name: str
    legal_name: str | None
    primary_contact_name: str | None
    primary_contact_email: str | None
    plan: str
    license_status: str
    users_count: int
    max_users: int
    modules_count: int
    subscription_status: str | None
    subscription_end: datetime | None
    created_at: datetime
    is_active: bool


class CustomerListOut(BaseModel):
    items: list[CustomerListItem]
    total: int
    page: int
    page_size: int


class CreateCustomerRequest(BaseModel):
    organization_name: str
    legal_name: str | None = None
    industry: str | None = None
    country: str | None = None
    primary_contact_name: str | None = None
    primary_contact_email: EmailStr | None = None
    primary_contact_phone: str | None = None
    timezone: str | None = None
    status: str | None = "trial"  # trial|active|expiring|expired|suspended
    plan_code: str | None = "trial"  # trial|basic|professional|enterprise|<custom>
    start_date: datetime | None = None
    expiry_date: datetime | None = None


class UpdateCustomerRequest(BaseModel):
    name: str | None = None
    legal_name: str | None = None
    industry: str | None = None
    country: str | None = None
    timezone: str | None = None
    primary_contact_name: str | None = None
    primary_contact_email: EmailStr | None = None
    primary_contact_phone: str | None = None
    max_users: int | None = None


class CreateCustomerOut(BaseModel):
    id: int
    name: str
    slug: str
    # Still returned once so the Seller can copy/share it manually even
    # when email delivery fails or SMTP isn't configured — never the sole
    # way to get it now that email_delivered/email_detail report whether
    # services/email_service.py actually sent it too (see customer_service
    # .create_customer).
    temp_password: str | None = None
    admin_email: str | None = None
    email_delivered: bool | None = None
    email_detail: str | None = None


class SubscriptionOut(BaseModel):
    id: int
    status: str
    plan_id: int | None
    start_date: datetime
    end_date: datetime | None
    billing_cycle: str
    amount: float
    currency: str
    auto_renew: bool


class SubscriptionHistoryItem(BaseModel):
    id: int
    status: str
    start_date: datetime
    end_date: datetime | None
    amount: float
    billing_cycle: str


class ModuleLicenseItem(BaseModel):
    module: str
    purchased: bool
    enabled: bool
    usage: int | None
    limit: int | None
    expiry: datetime | None


class CustomerUserItem(BaseModel):
    id: int
    full_name: str
    email: str
    role: str
    status: str
    last_login: datetime | None


class CustomerActivityItem(BaseModel):
    user_name: str
    event_type: str
    message: str | None
    timestamp: datetime


class CustomerDetailOut(BaseModel):
    id: int
    name: str
    legal_name: str | None
    slug: str
    industry: str | None
    country: str | None
    timezone: str | None
    primary_contact_name: str | None
    primary_contact_email: str | None
    primary_contact_phone: str | None
    plan: str
    license_status: str
    is_active: bool
    max_users: int
    users_count: int
    licensed_modules: list[str]
    created_at: datetime
    trial_expires_at: datetime | None
    license_expires_at: datetime | None
    subscription: SubscriptionOut | None
    subscription_history: list[SubscriptionHistoryItem]
    usage: dict
    modules: list[ModuleLicenseItem]
    users: list[CustomerUserItem]
    recent_activity: list[CustomerActivityItem]


class ImpersonatedUser(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    is_active: bool
    is_verified: bool
    organization_id: int | None
    created_at: datetime
    accessible_modules: list[str] | None = None


class ImpersonatedOrg(BaseModel):
    id: int
    name: str
    slug: str
    license_status: str
    plan: str
    licensed_modules: list[str]
    max_users: int


class ImpersonateOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    target_user_id: int
    target_email: str
    user: ImpersonatedUser
    organization: ImpersonatedOrg


class CustomerAuditEntry(BaseModel):
    id: str
    timestamp: datetime
    user_name: str | None
    action: str
    module: str
    resource: str | None = None
    status: str
    source: str  # "organization" | "seller"


class CustomerAuditOut(BaseModel):
    items: list[CustomerAuditEntry]
    total: int
    page: int
    page_size: int
