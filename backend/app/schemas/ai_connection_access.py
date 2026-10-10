"""Pydantic models for /api/connections/ai-access/* (AIC-P5) — the
org-admin-facing "who in my organization can see/use this AI connection"
screen. See services/ai_connection_permission_service.py for the real
authorization rules this surfaces; these schemas carry no logic of
their own.
"""
from datetime import datetime

from pydantic import BaseModel


class GrantOut(BaseModel):
    id: int
    grantee_type: str
    grantee_id: int
    grantee_label: str
    can_view: bool
    can_use: bool
    created_at: datetime


class UsageSummaryOut(BaseModel):
    """Live snapshot for a seller-managed connection's AI_CONNECTION
    entitlement — see ai_quota_service.get_usage_summary(). None fields
    mean that limit simply isn't set (unlimited), not "unknown"."""
    max_tokens: int | None
    token_period: str | None
    tokens_used: int
    tokens_reserved: int
    ai_rate_limit: int | None
    requests_last_minute: int
    ai_concurrency_limit: int | None
    concurrent_in_flight: int


class AiAccessConnectionItem(BaseModel):
    connection_id: int
    name: str
    connection_type: str
    is_seller_managed: bool
    is_public: bool
    can_toggle_public: bool
    grants: list[GrantOut]
    usage: UsageSummaryOut | None = None


class AiAccessListOut(BaseModel):
    items: list[AiAccessConnectionItem]


class SetPublicRequest(BaseModel):
    is_public: bool


class GrantRequest(BaseModel):
    grantee_type: str  # "USER" | "ROLE"
    grantee_id: int
    can_view: bool = True
    can_use: bool = True


class GranteeOption(BaseModel):
    id: int
    label: str


class GranteeOptionsOut(BaseModel):
    users: list[GranteeOption]
    roles: list[GranteeOption]


# ── Default AI connection selection (AIC-P6) ───────────────────────

class UsableAiConnectionOut(BaseModel):
    """One row for the AI-connection PICKERS (AI Extraction, Run AI
    Extraction, SQL Workspace AI Assistant) — see
    ai_connection_access_service.list_usable_connections(). Deliberately
    slim (no config/secrets) — these pickers only ever need an id to pass
    back to the extraction/SQL-assist endpoints."""
    id: int
    name: str
    connection_type: str
    status: str
    is_seller_managed: bool


class UsableAiConnectionsOut(BaseModel):
    items: list[UsableAiConnectionOut]


class AiDefaultsOut(BaseModel):
    user_default_connection_id: int | None
    org_default_connection_id: int | None
    effective_connection_id: int | None


class SetDefaultRequest(BaseModel):
    connection_id: int
