"""
UserActivityLog — one append-only feed backing both the "Activity" tab and
the "Login History" tab on a user's detail page (same pattern as
IncidentTimelineEvent: one table, a type discriminator, filtered client-side
by tab). Additive: no changes to models/user.py's own columns beyond the
department field added alongside this.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class UserActivityType(str, enum.Enum):
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    INVITED = "INVITED"
    UPDATED = "UPDATED"
    ROLE_CHANGED = "ROLE_CHANGED"
    DISABLED = "DISABLED"
    ENABLED = "ENABLED"
    PASSWORD_RESET = "PASSWORD_RESET"
    DELETED = "DELETED"


class UserActivityLog(PlatformBase):
    __tablename__ = "user_activity_log"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    event_type: Mapped[UserActivityType] = mapped_column(Enum(UserActivityType), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Who performed the action on this user — null for self-service events (LOGIN).
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<UserActivityLog user={self.user_id} {self.event_type}>"
