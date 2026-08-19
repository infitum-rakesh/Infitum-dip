"""Roles router — /api/roles/...

Administration -> Roles: custom, org-scoped RBAC on top of the platform's
fixed user-role tiers. Every route requires the ADMIN action tier — same
uniform gate as /api/admin/users (see app/api/user_admin.py docstring) —
mirrored on the frontend by ROLE_PERMISSIONS' `role: ROLES.ORG_ADMIN`.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.models.role import PERMISSION_CATEGORIES
from app.models.user import User, UserRole
from app.schemas.roles import (
    AssignUserRequest,
    RoleAuditListOut,
    RoleCreateRequest,
    RoleDetailOut,
    RoleListOut,
    RoleOut,
    RoleUpdateRequest,
    RoleUserOut,
)
from app.services import role_service as svc
from app.services import user_admin_service
from app.services.auth_service import get_org_by_id

router = APIRouter(prefix="/roles", tags=["roles"])

_admin = require_action("ADMIN", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")


async def _get_or_404(db: AsyncSession, role_id: int, user: User):
    role = await svc.get_role(db, role_id, _effective_org_id(user))
    if role is None:
        raise _not_found()
    return role


async def _get_org(db: AsyncSession, user: User):
    org_id = _effective_org_id(user)
    return await get_org_by_id(db, org_id) if org_id is not None else None


# ── List / get ────────────────────────────────────────────────────

@router.get("", response_model=RoleListOut)
async def list_roles(user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    org = await _get_org(db, user)
    await svc.ensure_default_roles(db, user.organization_id, org)

    roles = await svc.list_roles(db, _effective_org_id(user))
    name_map = await svc.actor_name_map(db, {r.created_by for r in roles})
    items = [RoleOut(**await svc.role_to_out(db, r, name_map)) for r in roles]
    return RoleListOut(
        items=items, total=len(items), categories=PERMISSION_CATEGORIES,
        licensed_categories=svc.licensed_categories(org), category_actions=svc.CATEGORY_ACTIONS,
    )


@router.get("/{role_id}", response_model=RoleDetailOut)
async def get_role(role_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    role = await _get_or_404(db, role_id, user)
    org = await _get_org(db, user)
    name_map = await svc.actor_name_map(db, {role.created_by})

    permissions = await svc.get_permissions(db, role_id)
    users = await svc.get_users(db, role_id)

    base = await svc.role_to_out(db, role, name_map)
    return RoleDetailOut(
        **base,
        permissions=[{"category": p.category, "action": p.action.value} for p in permissions],
        users=[RoleUserOut(id=u.id, full_name=u.full_name, email=u.email, department=u.department, status=user_admin_service.status_of(u)) for u in users],
        categories=PERMISSION_CATEGORIES,
        licensed_categories=svc.licensed_categories(org),
        category_actions=svc.CATEGORY_ACTIONS,
    )


# ── Create / update / delete ────────────────────────────────────

@router.post("", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(body: RoleCreateRequest, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    org = await _get_org(db, user)
    try:
        role = await svc.create_role(db, organization_id=user.organization_id, actor_id=user.id, org=org, body=body)
    except svc.RoleValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name_map = await svc.actor_name_map(db, {user.id})
    return RoleOut(**await svc.role_to_out(db, role, name_map))


@router.put("/{role_id}", response_model=RoleOut)
async def update_role(role_id: int, body: RoleUpdateRequest, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    role = await _get_or_404(db, role_id, user)
    org = await _get_org(db, user)
    try:
        role = await svc.update_role(db, role, org=org, body=body, actor_id=user.id)
    except svc.RoleValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name_map = await svc.actor_name_map(db, {role.created_by})
    return RoleOut(**await svc.role_to_out(db, role, name_map))


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    role = await _get_or_404(db, role_id, user)
    try:
        await svc.delete_role(db, role)
    except svc.RoleValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# ── User assignment ──────────────────────────────────────────────

@router.post("/{role_id}/users", response_model=RoleUserOut, status_code=status.HTTP_201_CREATED)
async def assign_user(role_id: int, body: AssignUserRequest, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    role = await _get_or_404(db, role_id, user)
    target = await user_admin_service.get_user(db, body.user_id, _effective_org_id(user))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    await svc.assign_user(db, role, target, user.id)
    return RoleUserOut(id=target.id, full_name=target.full_name, email=target.email, department=target.department, status=user_admin_service.status_of(target))


@router.delete("/{role_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unassign_user(role_id: int, user_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    role = await _get_or_404(db, role_id, user)
    target = await user_admin_service.get_user(db, user_id, _effective_org_id(user))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    await svc.unassign_user(db, role, target, user.id)


# ── Audit history ─────────────────────────────────────────────────

@router.get("/{role_id}/audit", response_model=RoleAuditListOut)
async def get_audit(role_id: int, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    await _get_or_404(db, role_id, user)
    events = await svc.get_audit_log(db, role_id)
    name_map = await svc.actor_name_map(db, {e.actor_id for e in events})
    return RoleAuditListOut(role_id=role_id, items=[svc.audit_to_out(e, name_map) for e in events])
