"""Request/response models for /api/notifications (SB23)."""
from datetime import datetime

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: int
    type: str
    title: str
    body: str
    related_object_type: str | None
    related_object_id: int | None
    read_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    total: int
    unread_count: int
