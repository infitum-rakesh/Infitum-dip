"""
Notification — real in-app notification persistence (spec section 51).
Nothing like this existed before this feature: the header Bell icon
(frontend/src/components/navigation/Header.jsx) was a static dead button
with no backend at all. See services/billing/notification_service.py for
the write path (also sends an email via the existing EmailService) and
api/billing/notifications.py for the read path backing the real Bell UI.
"""
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class Notification(PlatformBase):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    # None = org-wide (visible to every user in the org, e.g. "all
    # subscriptions expired"); set = targeted at one user.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    # TRIAL_STARTED|TRIAL_ENDING_SOON|TRIAL_EXPIRED|SUBSCRIPTION_ACTIVATED|
    # SUBSCRIPTION_EXPIRING_SOON|SUBSCRIPTION_EXPIRED|INVOICE_GENERATED|
    # INVOICE_DUE|PAYMENT_SUCCESSFUL|PAYMENT_FAILED|SUBSCRIPTION_RENEWED
    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    related_object_type: Mapped[str | None] = mapped_column(String(40), nullable=True)  # "subscription"|"invoice"|"payment"
    related_object_id: Mapped[int | None] = mapped_column(nullable=True)

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self) -> str:
        return f"<Notification org={self.organization_id} {self.type}>"
