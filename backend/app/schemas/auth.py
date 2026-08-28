from datetime import datetime
from pydantic import BaseModel, EmailStr, field_validator


# ── Request schemas ─────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    company_name: str
    country: str | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    # current_password is optional — required unless the account is
    # currently flagged must_change_password (a dummy password an admin
    # just issued isn't something the user should have to re-type back to
    # prove they know it). api/auth.py enforces that distinction; this
    # schema alone can't, since it has no access to the authenticated user.
    current_password: str | None = None
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


# ── Response schemas ────────────────────────────────────────────

class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    is_active: bool
    is_verified: bool
    organization_id: int | None
    created_at: datetime
    # True whenever the account's current password is a one-time dummy
    # value (just invited, or an admin/Seller just reset it) — never true
    # for an SSO-provisioned account, since SSO never sets hashed_password
    # via one of these flows. The frontend router hard-gates every /app/*
    # route behind this until POST /auth/change-password clears it.
    must_change_password: bool = False
    # Layer-3a of utils/permissions.js evaluateAccess() — null means "inherits
    # every module the org is licensed for" (frontend's userHasModule()
    # already treats null this way); set by an org admin via Users admin
    # module → Module Access tab.
    accessible_modules: list[str] | None = None

    model_config = {"from_attributes": True}


class OrgOut(BaseModel):
    id: int
    name: str
    slug: str
    license_status: str
    plan: str = "trial"
    licensed_modules: list[str]
    max_users: int
    # Needed by the frontend's getTrialDaysLeft()/trial banner (utils/permissions.js,
    # components/navigation/Sidebar.jsx) to compute real days-remaining and
    # progress-bar fill instead of a hardcoded value — previously omitted here,
    # so trial_expires_at was always undefined on the client no matter what
    # the org row actually held.
    trial_expires_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut
    organization: OrgOut | None = None


class MessageResponse(BaseModel):
    message: str
