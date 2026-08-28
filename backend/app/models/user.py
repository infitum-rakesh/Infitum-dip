import enum
from datetime import datetime, timezone

from sqlalchemy import String, Enum, Boolean, DateTime, ForeignKey, JSON, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.connection import PlatformBase


class UserRole(str, enum.Enum):
    SUPER_ADMIN = "super_admin"
    SELLER = "seller"
    ORG_ADMIN = "org_admin"
    DATA_ENGINEER = "data_engineer"
    ANALYST = "analyst"
    VIEWER = "viewer"


class User(PlatformBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.VIEWER)
    # is_active=False -> disabled by an admin. "Pending Invitation" in the Users
    # admin UI is derived from is_active=True + last_login is None — see
    # services/user_admin_service.status_of().
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # True whenever this account's current password is a one-time dummy
    # value the user never chose themselves — every temp-password issuance
    # site sets this (user_admin_service.invite_user/reset_password,
    # seller_user_service.invite_user/reset_password, customer_service.
    # create_customer/resend_admin_welcome_email). auth_service checked
    # only after a successful PASSWORD login (never SSO — an SSO account's
    # hashed_password is never used to authenticate, so this flag would be
    # meaningless there); the frontend router hard-gates every /app/* route
    # behind it until POST /auth/change-password clears it.
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Layer-3a of the frontend's evaluateAccess() (utils/permissions.js
    # userHasModule): null = inherits every module the org is licensed for;
    # a list restricts the user to that subset. This column is what the
    # Users admin "Module Access" tab actually edits — it was previously
    # read by the frontend but never had a backing column or admin UI.
    accessible_modules: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Optional custom Role (Administration -> Roles module, models/role.py) —
    # additive on top of the fixed `role` tier above, not a replacement for
    # it. Assigned/unassigned from the Role Details "Users" tab.
    custom_role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id"), nullable=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # Seller Portal (Seller Users module) — only meaningful when
    # role == UserRole.SELLER. One of core.deps.SELLER_ROLE_ACTIONS' keys
    # (SELLER_ADMIN/SELLER_CUSTOMER_MANAGER/SELLER_LICENSE_MANAGER/
    # SELLER_SUPPORT/SELLER_VIEWER). NULL = full Seller-tier access (same
    # "no sub-role = full tier default" convention as custom_role_id being
    # unset elsewhere) — see core.deps.require_seller_action.
    seller_role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="users")  # type: ignore[name-defined]  # noqa: F821

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"
