"""
In-app notification persistence (spec section 51). SB22's two scheduled
jobs (expiry_job.py, billing_scheduler.py) both write real Notification
rows through create_notification() below; SB23 adds the real email send
(via the existing EmailService — same SMTP config Seller Settings ->
Email already configures, RR1/RR4) and the list/mark-read API
(api/billing/notifications.py) that backs the real header Bell
(frontend/src/components/navigation/Header.jsx).

Email delivery is best-effort and never blocks the in-app notification:
EmailService.send_email() itself already returns delivered=False with a
clear reason (SMTP not configured, send failure, ...) rather than
raising, so a missing/misconfigured SMTP setup never breaks a
notification write — the Bell always gets the real row either way.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing.notification import Notification
from app.models.organization import Organization
from app.models.user import User
from app.services import email_service


async def _recipient_email(db: AsyncSession, *, organization_id: int, user_id: int | None) -> str | None:
    if user_id is not None:
        user = await db.get(User, user_id)
        if user and user.email:
            return user.email
    org = await db.get(Organization, organization_id)
    return org.primary_contact_email if org else None


async def create_notification(
    db: AsyncSession, *, organization_id: int, type: str, title: str, body: str,
    user_id: int | None = None, related_object_type: str | None = None, related_object_id: int | None = None,
    send_email: bool = True,
) -> Notification:
    """send_email=False (Payment Invoice Approval Workflow, Phase 6):
    write the Bell row only, skip the inline SMTP send below. Approval
    assignment emails go through the new, real queued/retried
    services/approval/notification_outbox_service.py instead of this
    fire-and-forget inline send (see that module's docstring for why —
    an approver must eventually be emailed even through a transient SMTP
    failure, which this inline call can't offer). Every other existing
    caller is unaffected — the default stays True, identical behavior."""
    notification = Notification(
        organization_id=organization_id, user_id=user_id, type=type, title=title, body=body,
        related_object_type=related_object_type, related_object_id=related_object_id,
    )
    db.add(notification)
    await db.commit()
    await db.refresh(notification)

    if send_email:
        recipient = await _recipient_email(db, organization_id=organization_id, user_id=user_id)
        if recipient:
            await email_service.send_email(db, to=recipient, subject=title, body=body)
    return notification


async def list_notifications(
    db: AsyncSession, *, organization_id: int, user_id: int | None = None, unread_only: bool = False,
    page: int = 1, page_size: int = 20,
) -> tuple[list[Notification], int]:
    # Org-wide rows (user_id IS NULL) are visible to every user in the
    # org; user-targeted rows only to that user — see Notification's own
    # docstring for the convention.
    stmt = select(Notification).where(
        Notification.organization_id == organization_id,
        (Notification.user_id.is_(None)) if user_id is None else
        ((Notification.user_id.is_(None)) | (Notification.user_id == user_id)),
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc())

    all_matching = list((await db.execute(stmt)).scalars().all())
    total = len(all_matching)
    start = (page - 1) * page_size
    return all_matching[start:start + page_size], total


async def mark_read(db: AsyncSession, notification: Notification) -> Notification:
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(notification)
    return notification


async def unread_count(db: AsyncSession, *, organization_id: int, user_id: int | None) -> int:
    _, total = await list_notifications(db, organization_id=organization_id, user_id=user_id, unread_only=True, page=1, page_size=1_000_000)
    return total
