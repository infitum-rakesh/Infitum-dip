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

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut
    organization: OrgOut | None = None


class MessageResponse(BaseModel):
    message: str
