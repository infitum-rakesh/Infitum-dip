"""
AIConnectionAccessService — the central, mandatory authorization check for
every AI Connection (Connection rows with category=AI_PROVIDER) access
decision in this codebase. Every AI call site funnels through
check_access() here, exactly the way every module route already funnels
through services/billing/access_service.SubscriptionAccessService instead
of re-deriving entitlement logic per router (same precedent, new domain).

Two ownership shapes, both evaluated by the SAME function (spec section 2):

  - Seller-provided ("seller-managed"): Connection.organization_id IS NULL
    (the existing platform-shared convention — see connection.py). Usable
    by an org ONLY if that org currently holds an ACTIVE AI_CONNECTION
    CustomerModule entitlement whose ai_provider_bundle is empty/None (all
    providers) or includes this connection's connection_type — see
    customer_module.py's own docstring for why the limits live there
    rather than a forked entitlement table. A subscription alone is NOT
    enough (spec: "a subscription must not automatically grant every user
    permission") — an explicit AIConnectionPermission grant (or the
    connection's own is_public flag) is still required on top.

  - Customer-owned: Connection.organization_id == the caller's own org.
    No subscription/entitlement check at all — the org used its own
    credentials, no Seller billing relationship is involved. Access is
    governed purely by AIConnectionPermission / is_public, same as above.

Precedence (spec section 5.3's "define precedence explicitly"), evaluated
in this exact order — an earlier check that fails short-circuits every
later one, and none of them can be bypassed by a later one:

  1. SUPER_ADMIN bypass (same unconditional bypass core/deps.require_action
     already gives this tier everywhere else in this codebase).
  2. Connection must exist and be an AI_PROVIDER connection.
  3. Tenant boundary: a customer-owned connection belonging to a DIFFERENT
     org is an unconditional, non-negotiable deny (CROSS_TENANT) — never
     reachable via is_public, a role grant, or anything else.
  4. For "use" specifically (not "view"): the connection must be ACTIVE —
     a disabled/errored connection can still be viewed (so an admin can
     see why it's broken) but never used.
  5. Seller-managed only: the calling org must hold a live, unexpired
     AI_CONNECTION entitlement covering this connection's provider type.
  6. is_public: if set, grants both view and use to every user who
     reached this point (i.e. already passed 1-5) — organisation-scoped
     by construction, since step 3/5 already confirmed the caller's org
     relationship to this exact connection.
  7. Explicit AIConnectionPermission grant — by user id, or by the
     caller's custom_role_id if they have one. can_view/can_use are
     independent columns (view never implies use, use never implies
     secret access — secrets never leave connection_service's encryption
     boundary regardless of this check's outcome, see
     connection_service._masked_config()/decrypt_secret()).
  8. Otherwise: deny (NOT_GRANTED).

acting_user is Optional by design: automated/system-triggered AI calls
(the document pipeline's scheduled/background processing — see
document_service.py's _run_ai_extraction()) don't always have one real
"current request user" the way an interactive "Run AI Extraction" click
or the SQL Workspace AI Assistant panel does. When acting_user is None,
step 7's explicit per-user/per-role grant lookup has no grantee to check
against, so only step 6 (is_public) can grant access — an unattributed
automated call can use a connection made public to the org, never one
gated behind a specific person's individual grant. This is a real,
disclosed behavior boundary, not an oversight: callers that DO have a
real acting user (interactive routes) must pass it to get full access.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_connection import AIConnectionGranteeType, AIConnectionPermission
from app.models.connection import Connection, ConnectionCategory, ConnectionStatus, ConnectionType
from app.models.customer_module import CustomerModule
from app.models.user import User, UserRole
from app.services.billing.access_service import _entitlement_active, access_service


class AIAccessDenialReason(str, enum.Enum):
    CONNECTION_NOT_FOUND = "CONNECTION_NOT_FOUND"
    CROSS_TENANT = "CROSS_TENANT"
    CONNECTION_INACTIVE = "CONNECTION_INACTIVE"
    NOT_ENTITLED = "NOT_ENTITLED"
    NOT_GRANTED = "NOT_GRANTED"


@dataclass
class AIAccessResult:
    granted: bool
    reason: AIAccessDenialReason | None = None
    connection: Connection | None = None
    is_seller_managed: bool = False

    @property
    def denial_message(self) -> str:
        if self.granted:
            return ""
        return {
            AIAccessDenialReason.CONNECTION_NOT_FOUND: "AI connection not found (it may have been deleted, or isn't an AI Provider connection).",
            AIAccessDenialReason.CROSS_TENANT: "This AI connection belongs to a different organization.",
            AIAccessDenialReason.CONNECTION_INACTIVE: "This AI connection is currently disabled or in an error state.",
            AIAccessDenialReason.NOT_ENTITLED: "Your organization does not currently have an active AI Connection subscription covering this provider.",
            AIAccessDenialReason.NOT_GRANTED: "You do not have permission to use this AI connection. Ask your administrator to grant access.",
        }.get(self.reason, "Access to this AI connection was denied.")


class AIConnectionAccessService:
    async def has_active_ai_entitlement(
        self, db: AsyncSession, organization_id: int, connection_type: ConnectionType,
    ) -> bool:
        """Public (not just internal to check_access()) because
        ai_connection_permission_service._visible_connections() also needs
        this exact entitlement test — a customer org with no AI_CONNECTION
        purchase at all must never see a seller-managed connection's name
        or type on the "Manage AI Access" screen, not just be denied actual
        use of it. Reported live: an org with zero AI_CONNECTION entitlement
        could still see every seller-managed connection's name there."""
        active_subs = await access_service.get_active_subscriptions(db, organization_id)
        if not active_subs:
            return False
        active_sub_ids = {s.id for s in active_subs}
        stmt = select(CustomerModule).where(
            CustomerModule.organization_id == organization_id,
            CustomerModule.module == "AI_CONNECTION",
            CustomerModule.subscription_id.in_(active_sub_ids),
        )
        result = await db.execute(stmt)
        for cm in result.scalars().all():
            if not _entitlement_active(cm):
                continue
            bundle = cm.ai_provider_bundle
            if not bundle or connection_type.value in bundle:
                return True
        return False

    async def _has_explicit_grant(
        self, db: AsyncSession, *, acting_user: User, organization_id: int, connection_id: int, require_use: bool,
    ) -> bool:
        stmt = select(AIConnectionPermission).where(
            AIConnectionPermission.connection_id == connection_id,
            AIConnectionPermission.organization_id == organization_id,
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())
        wanted = [(AIConnectionGranteeType.USER, acting_user.id)]
        if acting_user.custom_role_id is not None:
            wanted.append((AIConnectionGranteeType.ROLE, acting_user.custom_role_id))
        for row in rows:
            if (row.grantee_type, row.grantee_id) in wanted:
                if require_use and row.can_use:
                    return True
                if not require_use and row.can_view:
                    return True
        return False

    async def check_access(
        self, db: AsyncSession, *, organization_id: int | None, acting_user: User | None,
        connection_id: int, required: str = "use",
    ) -> AIAccessResult:
        """required: "view" or "use" — see module docstring for the full
        precedence chain. `organization_id` is accepted separately from
        `acting_user.organization_id` so a caller that only knows the org
        (not a specific user — see module docstring) can still check
        is_public access; when both are given they must agree for a
        customer-owned connection (enforced by the CROSS_TENANT check)."""
        if acting_user is not None and acting_user.role == UserRole.SUPER_ADMIN:
            connection = await db.get(Connection, connection_id)
            if connection is None or connection.category != ConnectionCategory.AI_PROVIDER:
                return AIAccessResult(granted=False, reason=AIAccessDenialReason.CONNECTION_NOT_FOUND)
            return AIAccessResult(granted=True, connection=connection, is_seller_managed=connection.organization_id is None)

        connection = await db.get(Connection, connection_id)
        if connection is None or connection.category != ConnectionCategory.AI_PROVIDER:
            return AIAccessResult(granted=False, reason=AIAccessDenialReason.CONNECTION_NOT_FOUND)

        is_seller_managed = connection.organization_id is None
        if not is_seller_managed:
            if organization_id is None or connection.organization_id != organization_id:
                return AIAccessResult(granted=False, reason=AIAccessDenialReason.CROSS_TENANT, connection=connection)

        if organization_id is None:
            # Seller/platform-tier caller with no tenant context inspecting
            # a seller-managed connection — nothing to grant against.
            return AIAccessResult(granted=False, reason=AIAccessDenialReason.NOT_GRANTED, connection=connection, is_seller_managed=is_seller_managed)

        require_use = required == "use"
        if require_use and (not connection.is_active or connection.status != ConnectionStatus.ACTIVE):
            return AIAccessResult(granted=False, reason=AIAccessDenialReason.CONNECTION_INACTIVE, connection=connection, is_seller_managed=is_seller_managed)

        if is_seller_managed:
            entitled = await self.has_active_ai_entitlement(db, organization_id, connection.connection_type)
            if not entitled:
                return AIAccessResult(granted=False, reason=AIAccessDenialReason.NOT_ENTITLED, connection=connection, is_seller_managed=True)

        if connection.is_public:
            return AIAccessResult(granted=True, connection=connection, is_seller_managed=is_seller_managed)

        if acting_user is not None and acting_user.organization_id == organization_id:
            if await self._has_explicit_grant(
                db, acting_user=acting_user, organization_id=organization_id,
                connection_id=connection_id, require_use=require_use,
            ):
                return AIAccessResult(granted=True, connection=connection, is_seller_managed=is_seller_managed)

        return AIAccessResult(granted=False, reason=AIAccessDenialReason.NOT_GRANTED, connection=connection, is_seller_managed=is_seller_managed)

    async def filter_accessible(
        self, db: AsyncSession, *, connections: list[Connection], organization_id: int | None,
        acting_user: User | None, required: str = "use",
    ) -> list[Connection]:
        """Bulk convenience for picker/list call sites — same per-connection
        rules as check_access(), just avoiding boilerplate at each call
        site. Not optimized into a single query (the candidate lists this
        is called with are always small — an org's own AI connections) —
        clarity over micro-optimization here."""
        allowed = []
        for conn in connections:
            result = await self.check_access(
                db, organization_id=organization_id, acting_user=acting_user,
                connection_id=conn.id, required=required,
            )
            if result.granted:
                allowed.append(conn)
        return allowed

    async def list_usable_connections(
        self, db: AsyncSession, *, organization_id: int, acting_user: User,
    ) -> list[Connection]:
        """Real bug found live (AIC-P8): every AI-connection PICKER in the
        app (Document Intelligence's AI Extraction stage, the Documents/
        Review Queue "Run AI Extraction" control, the SQL Workspace AI
        Assistant panel) populated its dropdown from the plain connections
        list endpoint, which — like get_connection() before the AIC-P7 fix
        — filters strictly `organization_id == organization_id` and so
        silently excludes every seller-managed (NULL-org) connection. A
        user granted access to a seller-managed connection via "Manage AI
        Access" (AIC-P5) could never actually select it anywhere, because
        it never even appeared in the list. This mirrors
        document_service._pick_ai_connections()'s own candidate query
        (org-owned OR seller-managed) + filter_accessible() for the real
        per-user authorization narrowing."""
        stmt = select(Connection).where(
            Connection.category == ConnectionCategory.AI_PROVIDER,
            Connection.is_active.is_(True),
            or_(Connection.organization_id == organization_id, Connection.organization_id.is_(None)),
        ).order_by(Connection.id.asc())
        candidates = list((await db.execute(stmt)).scalars().all())
        return await self.filter_accessible(
            db, connections=candidates, organization_id=organization_id, acting_user=acting_user, required="use",
        )


ai_connection_access_service = AIConnectionAccessService()
