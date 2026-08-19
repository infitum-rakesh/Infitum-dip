"""Seller Portal -> Seller Audit Logs router — /api/seller/audit-logs/*
(Phase 8). A real, read-only view over the SellerAuditLog table every
prior phase already writes to — see services/seller/seller_audit_service.py.
Gated on the "Audit" category (core/deps.py SELLER_CATEGORIES)."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.seller.audit_logs import SellerAuditDetailOut, SellerAuditListOut, SellerAuditStatsOut
from app.services.seller import seller_audit_service as svc

router = APIRouter(prefix="/seller/audit-logs", tags=["seller-audit-logs"])

_view = require_seller_action("VIEW", "Audit")


def _parse_date(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid date: {v}")


@router.get("/stats", response_model=SellerAuditStatsOut)
async def stats(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await svc.get_stats(db)


@router.get("", response_model=SellerAuditListOut)
async def list_audit_logs(
    q: str = Query("", description="Free-text search across seller user/customer/action/module/resource/details"),
    seller_user: str | None = Query(None),
    customer: str | None = Query(None),
    action: str | None = Query(None),
    module: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    items, total = await svc.list_entries(
        db, seller_user=seller_user, customer=customer, action=action, module=module,
        date_from=_parse_date(date_from), date_to=_parse_date(date_to), search=q, page=page, page_size=page_size,
    )
    return SellerAuditListOut(items=items, total=total, page=page, page_size=page_size, actions=svc.ACTIONS, modules=svc.MODULES)


@router.get("/export")
async def export_audit_logs(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    q: str = Query(""),
    seller_user: str | None = Query(None),
    customer: str | None = Query(None),
    action: str | None = Query(None),
    module: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    content, media_type, filename = await svc.export_entries(
        db, fmt=format, seller_user=seller_user, customer=customer, action=action, module=module,
        date_from=_parse_date(date_from), date_to=_parse_date(date_to), search=q,
    )
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{entry_id}", response_model=SellerAuditDetailOut)
async def get_audit_log(entry_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    entry = await svc.get_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit log entry not found")
    return SellerAuditDetailOut(**entry)
