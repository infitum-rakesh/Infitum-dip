"""
AI Connection Subscription & Organisation-Level Access Management.

Three new concerns layered on top of the EXISTING Connection model
(models/connection.py) and the existing Seller/Subscription/CustomerModule
entitlement system (models/customer_module.py, services/billing/
access_service.py) — nothing here duplicates those; see each model's own
docstring for which existing piece it extends.

  1. Per-user/per-role access grants on a specific AI_PROVIDER connection
     (AIConnectionPermission) — "view" and "use" are separate grants;
     neither implies secret access, which stays exactly where it already
     is (connection_service.encrypted_secrets, never exposed via this
     table or any API response).
  2. Default-connection resolution per user/org (AIConnectionDefault).
  3. Real usage accounting (AITokenUsageEvent) and atomic quota
     enforcement (AIQuotaReservation) — the entitlement LIMITS themselves
     (max_tokens/token_period/...) live on CustomerModule (see that
     model's own docstring for why it was extended rather than forked).

All four tables are TenantBase (org business data, same boundary as
Connection/AuditLog/SqlWorkspaceQuery) — every column that would otherwise
cross the TenantBase/PlatformBase split (user_id, role_id referencing
PlatformBase's User/Role; customer_module_id referencing PlatformBase's
CustomerModule) is a plain unconstrained int, the same "no literal FK
across this boundary" convention Connection.owner_id/organization_id and
Organization.seller_id already established — see database/connection.py's
PlatformBase/TenantBase docstrings for why a real ForeignKey can't span a
tenant that has migrated to its own physical database.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase

# Sentinel for "the organisation-wide default row," not a specific user —
# same convention as extraction_service.CUSTOM_ENGINE_CONNECTION_ID = 0:
# real User.id is DB autoincrement starting at 1, so 0 can never collide
# with a genuine user. Needed because a plain nullable user_id would make
# UniqueConstraint(organization_id, user_id) useless for "at most one
# org-wide default" — SQL treats every NULL as distinct for uniqueness
# purposes, so two NULL rows for the same org would NOT violate the
# constraint. A sentinel integer does not have that problem.
ORG_DEFAULT_USER_ID = 0


class AIConnectionGranteeType(str, enum.Enum):
    USER = "USER"
    ROLE = "ROLE"


class AIConnectionPermission(TenantBase):
    """Explicit per-user or per-role grant on ONE AI_PROVIDER (or any
    other) Connection — the access-control layer Connection itself never
    had (see connection.py's own docstring: "no per-user sharing/
    visibility column on Connection itself" before this feature).

    can_view / can_use are deliberately separate columns, not one
    combined flag: per spec, "view permission does not automatically
    imply use permission" and the reverse ("use permission does not
    grant permission to view or retrieve secrets" — secrets are already
    unreachable through any API response regardless of this table, see
    connection_service._masked_config(); can_view here only ever governs
    whether the connection's SAFE, non-secret summary shows up in a list/
    picker at all).

    grantee_type/grantee_id is a polymorphic pair (USER or ROLE) rather
    than two separate nullable FK-shaped columns, because this feature
    needs both per-user AND per-role grants on the same connection and
    neither of this codebase's two existing single-purpose precedents
    (SqlWorkspaceQueryFavorite's plain user_id join; Connection.owner_id)
    combines both — see Phase 1 assessment. grantee_id is a plain int: a
    USER row's grantee_id is a User.id (PlatformBase), a ROLE row's is a
    Role.id (also PlatformBase) — both are the standard no-FK-across-
    boundary convention, same reasoning as connection_id above.
    """
    __tablename__ = "ai_connection_permissions"
    __table_args__ = (
        UniqueConstraint("connection_id", "grantee_type", "grantee_id", name="uq_ai_connection_permission_grantee"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    # Redundant with connection.organization_id for a normal org-owned
    # connection, but load-bearing for a platform-shared one
    # (Connection.organization_id IS NULL): this column is always the
    # GRANTEE's own org, so a grant always resolves/routes under the
    # organisation actually using it, regardless of which physical
    # database the referenced connection itself currently lives in.
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)

    grantee_type: Mapped[AIConnectionGranteeType] = mapped_column(Enum(AIConnectionGranteeType), nullable=False)
    grantee_id: Mapped[int] = mapped_column(nullable=False, index=True)

    can_view: Mapped[bool] = mapped_column(Boolean, default=True)
    can_use: Mapped[bool] = mapped_column(Boolean, default=True)

    granted_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<AIConnectionPermission connection={self.connection_id} {self.grantee_type}:{self.grantee_id}>"


class AIConnectionDefault(TenantBase):
    """The default AI connection a specific user (or, via the
    ORG_DEFAULT_USER_ID sentinel row, the whole organisation) resolves to
    when a feature needs "the" AI connection without the caller picking
    one explicitly — see services/ai_connection_resolver.py (Phase 6) for
    the real precedence order (user default -> org default -> explicit
    fallback only if configured -> clear error, never a silent first-row
    pick).

    No existing precedent for "default X" exists anywhere else in this
    codebase (confirmed in the Phase 1 assessment), so user_id's sentinel-
    for-org-wide convention mirrors ORG_DEFAULT_USER_ID above rather than
    inventing a second, different nullable-column idiom for the same
    "is this org-wide or per-user" distinction within one feature.
    """
    __tablename__ = "ai_connection_defaults"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_ai_connection_default_org_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(nullable=False, index=True)  # ORG_DEFAULT_USER_ID = org-wide row
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<AIConnectionDefault org={self.organization_id} user={self.user_id} connection={self.connection_id}>"


class AIUsageSource(str, enum.Enum):
    """Per spec: 'use provider-reported usage as the authoritative token
    count when available... if a provider does not return usage for a
    request, record the usage as unavailable or estimated and clearly
    label it; do not fabricate exact token counts.'"""
    PROVIDER_REPORTED = "PROVIDER_REPORTED"
    ESTIMATED = "ESTIMATED"
    UNAVAILABLE = "UNAVAILABLE"


class AITokenUsageEvent(TenantBase):
    """Append-only usage ledger — one row per completed (or failed) AI
    request. This is the real accounting data behind every usage screen
    (Seller usage-by-customer/subscription, Customer Admin remaining-
    quota, per-user own-usage) and behind quota enforcement in
    AIQuotaReservation below.

    request_id + connection_id carries the idempotency guarantee the spec
    requires ("retries must be idempotent for usage records... do not
    double-count the same provider request"): the CALLER (the AI
    connection access/resolver service, Phase 3/7) generates one
    request_id per logical request attempt and reuses it across any
    internal retry of that SAME attempt — never a new id per retry — so
    the UniqueConstraint below makes a duplicate insert for the same
    attempt a no-op/conflict rather than a second counted usage row.
    """
    __tablename__ = "ai_token_usage_events"
    __table_args__ = (UniqueConstraint("connection_id", "request_id", name="uq_ai_token_usage_event_request"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    # Plain unconstrained int — references customer_modules.id (PlatformBase);
    # which entitlement/subscription this usage was counted against, so a
    # usage screen can group by subscription even though CustomerModule
    # itself lives across the TenantBase/PlatformBase boundary from this table.
    customer_module_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    # Which AI-enabled feature made the call — e.g. "DOCUMENT_EXTRACTION",
    # "SQL_WORKSPACE_NL2SQL", "INVOICE_EXTRACTION" — free text, same
    # "plain string column, not a shared enum" precedent as AuditLog.module.
    feature: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(100), nullable=False)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_source: Mapped[AIUsageSource] = mapped_column(Enum(AIUsageSource), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), default="SUCCESS")  # SUCCESS|FAILED
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self) -> str:
        return f"<AITokenUsageEvent connection={self.connection_id} feature={self.feature} tokens={self.total_tokens}>"


class AIQuotaReservationStatus(str, enum.Enum):
    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"


class AIQuotaReservation(TenantBase):
    """Short-lived row backing ATOMIC quota enforcement under concurrency
    (Phase 7) — per spec: 'concurrent requests cannot exceed a hard quota
    through race conditions.' A plain read-then-write against
    CustomerModule's limit columns is exactly the race the spec warns
    about (two concurrent requests both read 'quota not yet exhausted',
    both proceed, both exceed it); reserving BEFORE the real AI call and
    committing/releasing AFTER closes that window the same way an atomic
    `UPDATE ... WHERE used + ? <= limit` would, but as a visible row so a
    crashed/never-committed reservation can be swept and released rather
    than permanently locking out quota it never actually consumed.

    customer_module_id is the same plain-unconstrained-int cross-boundary
    reference as AITokenUsageEvent.customer_module_id above.
    """
    __tablename__ = "ai_quota_reservations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    customer_module_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AIQuotaReservationStatus] = mapped_column(
        Enum(AIQuotaReservationStatus), default=AIQuotaReservationStatus.RESERVED, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<AIQuotaReservation connection={self.connection_id} tokens={self.reserved_tokens} status={self.status}>"
