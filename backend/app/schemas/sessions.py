from datetime import datetime

from pydantic import BaseModel


class ActiveSessionOut(BaseModel):
    id: int
    user_id: int
    user_email: str
    user_full_name: str
    login_method: str
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool  # True if this row is the caller's own session


class ActiveSessionListOut(BaseModel):
    items: list[ActiveSessionOut]
    total: int          # session row count
    seats_used: int      # distinct active users — the number that counts against max_users
    max_users: int        # <= 0 means unlimited
