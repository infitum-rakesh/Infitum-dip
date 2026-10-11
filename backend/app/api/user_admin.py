"""Users admin router — /api/admin/users/...

Organization administrators managing users belonging to their own
organization. Every route requires the ADMIN action tier, which
ROLE_ACTIONS (app/core/deps.py) only grants to ORG_ADMIN/SELLER/
SUPER_ADMIN — this is the uniform gate for the whole module (mirrors the
frontend's `role: ROLES.ORG_ADMIN` PermissionGate — see
frontend/src/utils/constants.js USER_PERMISSIONS).
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.models.user import User, UserRole
from app.schemas.user_admin import (
    AdminUserListOut,
    AdminUserOut,
    AdminUserStatsOut,
    ChangeRoleRequest,
    InviteResultOut,
    InviteUserRequest,
    PasswordResetResultOut,
    UpdateUserRequest,
    UserActivityListOut,
)
from app.services import user_admin_service as svc

router = APIRouter(prefix="/admin/users", tags=["users-admin"])

_admin = require_action("ADMIN", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "User not found")


async def _get_or_404(db: AsyncSession, user_id: int, user: User):
    target = await svc.get_user(db, user_id, _effective_org_id(user))
    if target is None:
        raise _not_found()
    return target


def _out(target: User) -> AdminUserOut:
    return AdminUserOut(**svc.user_to_out(target))


# ── KPIs ──────────────────────────────────────────────────────────
# NOTE: literal-path /stats must stay registered before /{user_id} —
# Starlette matches by position, not type.

@router.get("/stats", response_model=AdminUserStatsOut)
async def stats(user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    return await svc.get_stats(db, _effective_org_id(user))


# ── List / get ────────────────────────────────────────────────────

@router.get("", response_model=AdminUserListOut)
async def list_users(
    q: str = Query("", description="Search name, email, department"),
    role: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("created_at"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(_admin),
    db: AsyncSession = Depends(get_db),
):
    users, total = await svc.list_users(
        db, organization_id=_effective_org_id(user), search=q, role=role,
        status_filter=status_filter, page=page, page_size=page_size, sort_by=sort_by, sort_dir=sort_dir,
    )
    return AdminUserListOut(items=[_out(u) for u in users], total=total, page=page, page_size=page_size)


@router.get("/{user_id}", response_model=AdminUserOut)
async def get_user(user_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id, user)
    return _out(target)


# ── Invite / update / delete ────────────────────────────────────

@router.post("", response_model=InviteResultOut, status_code=status.HTTP_201_CREATED)
async def invite_user(body: InviteUserRequest, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    try:
        target, temp_password, email_result = await svc.invite_user(db, organization_id=user.organization_id, actor_id=user.id, body=body)
    except svc.UserAdminValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return InviteResultOut(
        user=_out(target), temp_password=temp_password,
        email_delivered=email_result.delivered if email_result else None,
        email_detail=email_result.detail if email_result else None,
    )


@router.put("/{user_id}", response_model=AdminUserOut)
async def update_user(user_id: int, body: UpdateUserRequest, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id, user)
    target = await svc.update_user(db, target, body, user.id)
    return _out(target)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id, user)
    try:
        await svc.delete_user(db, target, user.id)
    except svc.UserAdminValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# ── Actions ───────────────────────────────────────────────────────

@router.post("/{user_id}/disable", response_model=AdminUserOut)
async def disable_user(user_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id, user)
    try:
        target = await svc.disable_user(db, target, user.id)
    except svc.UserAdminValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return _out(target)


@router.post("/{user_id}/enable", response_model=AdminUserOut)
async def enable_user(user_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id, user)
    target = await svc.enable_user(db, target, user.id)
    return _out(target)


@router.post("/{user_id}/reset-password", response_model=PasswordResetResultOut)
async def reset_password(user_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id, user)
    target, temp_password, email_result = await svc.reset_password(db, target, user.id)
    return PasswordResetResultOut(
        user=_out(target), temp_password=temp_password,
        email_delivered=email_result.delivered, email_detail=email_result.detail,
    )


@router.post("/{user_id}/role", response_model=AdminUserOut)
async def change_role(user_id: int, body: ChangeRoleRequest, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id, user)
    try:
        target = await svc.change_role(db, target, body.role, user.id)
    except svc.UserAdminValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return _out(target)


# ── Activity / Login history ──────────────────────────────────────

@router.get("/{user_id}/activity", response_model=UserActivityListOut)
async def get_activity(user_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    await _get_or_404(db, user_id, user)
    events = await svc.get_activity(db, user_id)
    name_map = await svc.actor_name_map(db, {e.actor_id for e in events})
    return UserActivityListOut(user_id=user_id, items=[svc.activity_to_out(e, name_map) for e in events])


@router.get("/{user_id}/login-history", response_model=UserActivityListOut)
async def get_login_history(user_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    await _get_or_404(db, user_id, user)
    events = await svc.get_login_history(db, user_id)
    name_map = await svc.actor_name_map(db, {e.actor_id for e in events})
    return UserActivityListOut(user_id=user_id, items=[svc.activity_to_out(e, name_map) for e in events])
