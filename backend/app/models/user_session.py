"""
UserSession — real, persisted record of one login (password or SSO),
keyed by the `sid` claim embedded in both the access and refresh JWTs
issued for it (see services/auth_service.issue_tokens()). This is what
makes two things real instead of client-side-only claims:

  1. Seat-limit enforcement ("no more than Organization.max_users
     concurrently active users", requirement from the SSO ask) — counted
     as DISTINCT active users per org, not raw session rows, so the same
     person opening a second tab/device doesn't consume a second seat.
  2. Real logout — /api/auth/logout previously did nothing server-side
     ("token invalidation handled client-side"); it now revokes the
     session here, which is what actually frees the seat immediately
     instead of waiting for the refresh token's natural expiry.

"Active" = revoked_at IS NULL AND expires_at > now(). expires_at mirrors
the refresh token's own expiry (settings.refresh_token_expire_days) since
that's the credential that keeps a session alive across access-token
renewals — see auth_service.refresh_access_token(), which touches
last_seen_at on every use.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class LoginMethod(str, enum.Enum):
    PASSWORD = "PASSWORD"
    SSO = "SSO"


class UserSession(PlatformBase):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    sid: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)  # the JWT "sid" claim

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)

    login_method: Mapped[LoginMethod] = mapped_column(Enum(LoginMethod), default=LoginMethod.PASSWORD)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<UserSession user={self.user_id} org={self.organization_id} {'revoked' if self.revoked_at else 'active'}>"
