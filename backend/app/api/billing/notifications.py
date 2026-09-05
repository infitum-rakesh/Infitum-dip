"""
GET /api/notifications, POST /api/notifications/{id}/read,
POST /api/notifications/read-all — the real API behind the header Bell
(SB24 wires frontend/src/components/navigation/Header.jsx to this).

Org-scoped: returns the calling user's own organization's notifications
(org-wide rows plus any user_id-targeted rows for this specific user —
see Notification's own docstring for that convention). Seller/Super
Admin users have no organization (get_current_organization returns
None) — the Notification model is customer-facing only (spec section
51's event list is all customer-side events), so they simply get an
empty list rather than an error; Sellers already have their own
customer-event visibility via SellerAuditLog + the Seller Dashboard.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_organization, get_current_user
from app.database.connection import get_db
from app.models.billing.notification import Notification
from app.models.organization import Organization
from app.models.user import User
from app.schemas.billing.notifications import NotificationListOut, NotificationOut
from app.services.billing import notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListOut)
async def list_notifications(
    unread_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    if org is None:
        return NotificationListOut(items=[], total=0, unread_count=0)
    items, total = await notification_service.list_notifications(
        db, organization_id=org.id, user_id=user.id, unread_only=unread_only, page=page, page_size=page_size,
    )
    unread = await notification_service.unread_count(db, organization_id=org.id, user_id=user.id)
    return NotificationListOut(items=[NotificationOut.model_validate(n) for n in items], total=total, unread_count=unread)


async def _get_own_notification_or_404(db: AsyncSession, notification_id: int, org: Organization | None) -> Notification:
    notification = await db.get(Notification, notification_id)
    if notification is None or org is None or notification.organization_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found.")
    return notification


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    notification_id: int, org: Organization | None = Depends(get_current_organization), db: AsyncSession = Depends(get_db),
):
    notification = await _get_own_notification_or_404(db, notification_id, org)
    notification = await notification_service.mark_read(db, notification)
    return NotificationOut.model_validate(notification)


@router.post("/read-all")
async def mark_all_notifications_read(
    user: User = Depends(get_current_user), org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    if org is None:
        return {"marked": 0}
    items, _ = await notification_service.list_notifications(
        db, organization_id=org.id, user_id=user.id, unread_only=True, page=1, page_size=1_000_000,
    )
    for n in items:
        await notification_service.mark_read(db, n)
    return {"marked": len(items)}
