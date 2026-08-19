"""Seller Portal -> Seller Users router — /api/seller/users/* (Phase 7).
Manages the Seller's OWN staff accounts — see services/seller/
seller_user_service.py. Gated on the "SellerUsers" category (core/deps.py
SELLER_CATEGORIES) — separate from /api/admin/users/* which manages a
*customer's* own org users."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.seller.seller_users import (
    ChangeSellerRoleRequest,
    InviteSellerUserRequest,
    InviteSellerUserResultOut,
    SellerPasswordResetResultOut,
    SellerUserActivityListOut,
    SellerUserListOut,
    SellerUserOut,
    SellerUserStatsOut,
    UpdateSellerUserRequest,
)
from app.services.seller import seller_user_service as svc

router = APIRouter(prefix="/seller/users", tags=["seller-users"])

_view = require_seller_action("VIEW", "SellerUsers")
_create = require_seller_action("CREATE", "SellerUsers")
_edit = require_seller_action("EDIT", "SellerUsers")
_delete = require_seller_action("DELETE", "SellerUsers")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Seller user not found")


async def _get_or_404(db: AsyncSession, user_id: int) -> User:
    target = await svc.get_user(db, user_id)
    if target is None:
        raise _not_found()
    return target


def _out(target: User) -> SellerUserOut:
    return SellerUserOut(**svc.user_to_out(target))


# ── KPIs ──────────────────────────────────────────────────────────
# literal-path /stats must stay registered before /{user_id} — Starlette
# matches by position, not type.

@router.get("/stats", response_model=SellerUserStatsOut)
async def stats(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await svc.get_stats(db)


# ── List / get ────────────────────────────────────────────────────

@router.get("", response_model=SellerUserListOut)
async def list_users(
    q: str = Query("", description="Search name, email, department"),
    seller_role: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("created_at"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    users, total = await svc.list_users(
        db, search=q, seller_role_filter=seller_role, status_filter=status_filter,
        page=page, page_size=page_size, sort_by=sort_by, sort_dir=sort_dir,
    )
    return SellerUserListOut(items=[_out(u) for u in users], total=total, page=page, page_size=page_size)


@router.get("/{user_id}", response_model=SellerUserOut)
async def get_user(user_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return _out(await _get_or_404(db, user_id))


# ── Invite / update / delete ────────────────────────────────────

@router.post("", response_model=InviteSellerUserResultOut, status_code=status.HTTP_201_CREATED)
async def invite_user(body: InviteSellerUserRequest, request: Request, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
    try:
        target, temp_password = await svc.invite_user(db, actor=user, body=body, ip_address=_client_ip(request))
    except svc.SellerUserServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return InviteSellerUserResultOut(user=_out(target), temp_password=temp_password)


@router.put("/{user_id}", response_model=SellerUserOut)
async def update_user(user_id: int, body: UpdateSellerUserRequest, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id)
    target = await svc.update_user(db, target, body, user, _client_ip(request))
    return _out(target)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, request: Request, user: User = Depends(_delete), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id)
    try:
        await svc.delete_user(db, target, user, _client_ip(request))
    except svc.SellerUserServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# ── Actions ───────────────────────────────────────────────────────

@router.post("/{user_id}/disable", response_model=SellerUserOut)
async def disable_user(user_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id)
    try:
        target = await svc.disable_user(db, target, user, _client_ip(request))
    except svc.SellerUserServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return _out(target)


@router.post("/{user_id}/enable", response_model=SellerUserOut)
async def enable_user(user_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id)
    target = await svc.enable_user(db, target, user, _client_ip(request))
    return _out(target)


@router.post("/{user_id}/reset-password", response_model=SellerPasswordResetResultOut)
async def reset_password(user_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id)
    target, temp_password = await svc.reset_password(db, target, user, _client_ip(request))
    return SellerPasswordResetResultOut(user=_out(target), temp_password=temp_password)


@router.post("/{user_id}/role", response_model=SellerUserOut)
async def change_role(user_id: int, body: ChangeSellerRoleRequest, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    target = await _get_or_404(db, user_id)
    try:
        target = await svc.change_seller_role(db, target, body.seller_role, user, _client_ip(request))
    except svc.SellerUserServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return _out(target)


# ── Activity / Login history ──────────────────────────────────────

@router.get("/{user_id}/activity", response_model=SellerUserActivityListOut)
async def get_activity(user_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    await _get_or_404(db, user_id)
    events = await svc.get_activity(db, user_id)
    name_map = await svc.actor_name_map(db, {e.actor_id for e in events})
    return SellerUserActivityListOut(user_id=user_id, items=[svc.activity_to_out(e, name_map) for e in events])


@router.get("/{user_id}/login-history", response_model=SellerUserActivityListOut)
async def get_login_history(user_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    await _get_or_404(db, user_id)
    events = await svc.get_login_history(db, user_id)
    name_map = await svc.actor_name_map(db, {e.actor_id for e in events})
    return SellerUserActivityListOut(user_id=user_id, items=[svc.activity_to_out(e, name_map) for e in events])
