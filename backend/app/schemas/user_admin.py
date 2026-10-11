from datetime import datetime

from pydantic import BaseModel, EmailStr


# ── Responses ────────────────────────────────────────────────────

class AdminUserOut(BaseModel):
    id: int
    email: str
    full_name: str
    department: str | None
    role: str
    status: str  # ACTIVE | INACTIVE | PENDING — computed, see user_admin_service.status_of
    is_active: bool
    is_verified: bool
    accessible_modules: list[str] | None
    last_login: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminUserListOut(BaseModel):
    items: list[AdminUserOut]
    total: int
    page: int
    page_size: int


class AdminUserStatsOut(BaseModel):
    total: int
    active: int
    inactive: int
    pending: int
    administrators: int


class InviteResultOut(BaseModel):
    user: AdminUserOut
    temp_password: str  # shown once regardless — kept as the fallback even when email_delivered is True
    email_delivered: bool | None = None  # None = send_invite=False, never attempted
    email_detail: str | None = None


class PasswordResetResultOut(BaseModel):
    user: AdminUserOut
    temp_password: str


class UserActivityEventOut(BaseModel):
    id: int
    event_type: str
    message: str | None
    actor_id: int | None
    actor_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserActivityListOut(BaseModel):
    user_id: int
    items: list[UserActivityEventOut]


# ── Requests ─────────────────────────────────────────────────────

class InviteUserRequest(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    department: str | None = None
    role: str = "viewer"
    is_active: bool = True  # "Status" field on the create form — Active vs Inactive from day one
    send_invite: bool = True  # UI toggle: "allow invitation email" — see service docstring


class UpdateUserRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    department: str | None = None
    accessible_modules: list[str] | None = None


class ChangeRoleRequest(BaseModel):
    role: str
