"""
session_service — real, DB-backed login sessions (see models/user_session.py).

Two things this makes real:
  1. Seat-limit enforcement: an Organization.max_users ceiling on the number
     of DISTINCT users concurrently signed in (password or SSO). <= 0 means
     unlimited, matching the "-1 == unlimited" convention already used on
     Plan.limits (e.g. Enterprise's {"users": -1}), which flows into
     Organization.max_users at customer creation time.
  2. Real logout: revoking a session by its `sid` is what actually frees a
     seat immediately, instead of waiting for the refresh token to expire.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.user_session import LoginMethod, UserSession

settings = get_settings()


def new_sid() -> str:
    return str(uuid4())


async def count_active_users(db: AsyncSession, organization_id: int, *, exclude_user_id: int | None = None) -> int:
    """Distinct active user_id count for the org — the seat-consuming unit."""
    now = datetime.now(timezone.utc)
    stmt = select(func.count(func.distinct(UserSession.user_id))).where(
        UserSession.organization_id == organization_id,
        UserSession.revoked_at.is_(None),
        UserSession.expires_at > now,
    )
    if exclude_user_id is not None:
        stmt = stmt.where(UserSession.user_id != exclude_user_id)
    result = await db.execute(stmt)
    return result.scalar_one() or 0


async def user_has_active_session(db: AsyncSession, user_id: int) -> bool:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(UserSession.id).where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def check_seat_available(db: AsyncSession, organization_id: int | None, user_id: int) -> None:
    """
    Raises SeatLimitError if issuing a new session for `user_id` would push
    the org's org past its max_users ceiling. A user who already has an
    active session never gets blocked by their own re-login (e.g. a second
    device) — only a genuinely NEW distinct user does.
    """
    if organization_id is None:
        return  # seller/platform users aren't org-seat-limited
    from app.models.organization import Organization  # local import avoids a cycle

    org = (await db.execute(select(Organization).where(Organization.id == organization_id))).scalar_one_or_none()
    if org is None or org.max_users is None or org.max_users <= 0:
        return  # unlimited

    if await user_has_active_session(db, user_id):
        return  # re-login by an already-active user consumes no new seat

    active = await count_active_users(db, organization_id)
    if active >= org.max_users:
        raise SeatLimitError(
            f"This organization's plan allows {org.max_users} concurrently signed-in user"
            f"{'s' if org.max_users != 1 else ''}, and that limit is already in use. "
            "Ask an admin to sign another user out, or upgrade the plan."
        )


class SeatLimitError(Exception):
    """Raised when creating a new session would exceed Organization.max_users."""


async def create_session(
    db: AsyncSession, *, sid: str, user_id: int, organization_id: int | None,
    login_method: LoginMethod = LoginMethod.PASSWORD,
    ip_address: str | None = None, user_agent: str | None = None,
) -> UserSession:
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    session = UserSession(
        sid=sid, user_id=user_id, organization_id=organization_id,
        login_method=login_method, ip_address=ip_address, user_agent=user_agent,
        expires_at=expires_at,
    )
    db.add(session)
    await db.flush()
    return session


async def touch_session(db: AsyncSession, sid: str) -> bool:
    """Called on every refresh — extends expires_at and bumps last_seen_at,
    mirroring the refreshed refresh-token's own new expiry. Returns False
    if the session was revoked (real logout) or has expired — the caller
    must then refuse the refresh outright, or a logged-out session's
    still-valid-looking refresh token would keep working forever."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(UserSession).where(
            UserSession.sid == sid, UserSession.revoked_at.is_(None), UserSession.expires_at > now,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        return False
    session.last_seen_at = now
    session.expires_at = now + timedelta(days=settings.refresh_token_expire_days)
    await db.flush()
    return True


async def revoke_session(db: AsyncSession, sid: str) -> bool:
    """Real logout. Returns True if a session was actually revoked."""
    result = await db.execute(select(UserSession).where(UserSession.sid == sid, UserSession.revoked_at.is_(None)))
    session = result.scalar_one_or_none()
    if session is None:
        return False
    session.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def list_active_sessions(db: AsyncSession, organization_id: int):
    """For the Admin 'Active Sessions' view — one row per active session
    (not deduped by user), newest first."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(UserSession).where(
            UserSession.organization_id == organization_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        ).order_by(UserSession.last_seen_at.desc())
    )
    return list(result.scalars().all())


async def list_active_sessions_with_user(db: AsyncSession, organization_id: int):
    """Same as list_active_sessions but joined with User for display fields
    (email/full name) the Admin Active Sessions page needs."""
    from app.models.user import User  # local import avoids a cycle

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(
            UserSession.organization_id == organization_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.last_seen_at.desc())
    )
    return list(result.all())  # list[(UserSession, User)]


async def revoke_session_by_id(db: AsyncSession, organization_id: int, session_id: int) -> bool:
    """Admin-initiated revoke of another user's session, scoped to their own org."""
    result = await db.execute(
        select(UserSession).where(
            UserSession.id == session_id,
            UserSession.organization_id == organization_id,
            UserSession.revoked_at.is_(None),
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        return False
    session.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    return True
