"""Active Sessions admin router — /api/admin/sessions/...

Real, DB-backed view of who is currently signed in to the caller's
organization (see models/user_session.py, services/session_service.py).
This is what makes the seat-limit enforcement in auth_service.issue_tokens
actionable for an Admin: they can see who's using a seat and revoke one to
free it up for someone else, instead of just hitting a wall.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_action
from app.core.security import decode_token
from app.database.connection import get_db
from app.models.user import User, UserRole
from app.schemas.sessions import ActiveSessionListOut, ActiveSessionOut
from app.services import session_service

router = APIRouter(prefix="/admin/sessions", tags=["sessions-admin"])

_admin = require_action("ADMIN", module=None)
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _effective_org_id(user: User) -> int | None:
    return None if user.role == UserRole.SUPER_ADMIN else user.organization_id


@router.get("", response_model=ActiveSessionListOut)
async def list_sessions(
    request: Request,
    user: User = Depends(_admin),
    token: str | None = Depends(_oauth2_scheme),
    db: AsyncSession = Depends(get_db),
):
    org_id = _effective_org_id(user)
    if org_id is None:
        return ActiveSessionListOut(items=[], total=0, seats_used=0, max_users=0)

    current_sid = None
    if token:
        try:
            current_sid = decode_token(token).get("sid")
        except JWTError:
            pass

    rows = await session_service.list_active_sessions_with_user(db, org_id)
    seats_used = await session_service.count_active_users(db, org_id)

    from app.services.auth_service import get_org_by_id
    org = await get_org_by_id(db, org_id)

    items = [
        ActiveSessionOut(
            id=session.id,
            user_id=session.user_id,
            user_email=u.email,
            user_full_name=u.full_name,
            login_method=session.login_method.value,
            ip_address=session.ip_address,
            user_agent=session.user_agent,
            created_at=session.created_at,
            last_seen_at=session.last_seen_at,
            expires_at=session.expires_at,
            is_current=(session.sid == current_sid),
        )
        for session, u in rows
    ]
    return ActiveSessionListOut(
        items=items, total=len(items), seats_used=seats_used,
        max_users=(org.max_users if org else 0),
    )


@router.post("/{session_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(session_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    org_id = _effective_org_id(user)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Platform admins have no organization-scoped sessions to revoke here.")
    ok = await session_service.revoke_session_by_id(db, org_id, session_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found or already inactive.")
