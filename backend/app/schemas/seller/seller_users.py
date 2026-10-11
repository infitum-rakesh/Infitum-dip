"""Pydantic models for /api/seller/users/* (Phase 7 — Seller Portal's own
staff accounts). Mirrors schemas/user_admin.py's shape closely — same
underlying User model, different scope — but kept as its own file, same
convention as every other Seller Portal schema module in this build."""
from datetime import datetime

from pydantic import BaseModel, EmailStr


# ── Responses ────────────────────────────────────────────────────

class SellerUserOut(BaseModel):
    id: int
    email: str
    full_name: str
    department: str | None
    seller_role: str | None  # null = full Seller Admin access (unset = full tier default)
    status: str  # ACTIVE | INACTIVE | PENDING — computed, see user_admin_service.status_of
    is_active: bool
    last_login: datetime | None
    created_at: datetime
    updated_at: datetime


class SellerUserListOut(BaseModel):
    items: list[SellerUserOut]
    total: int
    page: int
    page_size: int


class SellerUserStatsOut(BaseModel):
    total: int
    active: int
    inactive: int
    pending: int
    admins: int


class InviteSellerUserResultOut(BaseModel):
    user: SellerUserOut
    temp_password: str  # shown once regardless — kept as the fallback even when email_delivered is True
    email_delivered: bool | None = None  # None = no email provider configured at all, not attempted
    email_detail: str | None = None


class SellerPasswordResetResultOut(BaseModel):
    user: SellerUserOut
    temp_password: str


class SellerUserActivityEventOut(BaseModel):
    id: int
    event_type: str
    message: str | None
    actor_id: int | None
    actor_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SellerUserActivityListOut(BaseModel):
    user_id: int
    items: list[SellerUserActivityEventOut]


# ── Requests ─────────────────────────────────────────────────────

class InviteSellerUserRequest(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    department: str | None = None
    seller_role: str | None = None  # null = full Seller Admin access
    is_active: bool = True


class UpdateSellerUserRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    department: str | None = None


class ChangeSellerRoleRequest(BaseModel):
    seller_role: str | None = None  # null = full Seller Admin access
