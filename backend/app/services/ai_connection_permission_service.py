"""
ai_connection_permission_service — backs /api/connections/ai-access/* (AIC-P5):
the org-admin-facing screen for "organisation-level permissions" and
"public/private visibility" on AI connections, per the master spec.

Scope, deliberately kept narrow and self-service:

  - This is an ORG_ADMIN-facing feature, not a platform/Seller one — every
    function here requires a real `organization_id` (SUPER_ADMIN/SELLER,
    who are structurally org-less, are out of scope: they already bypass
    every AI access check unconditionally — see
    ai_connection_access_service.py's SUPER_ADMIN branch — so they have
    no need for a grant to use anything, and "my organization's users"
    has no meaning for them here).
  - Visibility set = every AI_PROVIDER connection this org could ever be
    routed to: its OWN connections (Connection.organization_id == this
    org) plus every active seller-managed one (organization_id IS NULL)
    — the exact same two-shape set ai_connection_access_service.py
    evaluates and document_service._pick_ai_connections() queries.
    Whether the org is actually entitled (AI_CONNECTION subscription) to
    a given seller-managed connection is a separate, later gate (Phase 3)
    — this listing is about configuring WHO inside the org gets it IF
    the org has it, not re-deriving entitlement.
  - is_public is only togglable on a connection this org actually OWNS.
    A seller-managed connection's is_public is a platform-wide decision
    (it changes behavior for every entitled org at once, not just this
    one) — there is no single org whose admin should control that, so
    it's surfaced here read-only with a clear reason, never silently
    editable.
  - Grants (AIConnectionPermission) are always manageable by this org's
    admin for any connection in the visibility set above, scoped to
    organization_id = this org (see models/ai_connection.py's own
    docstring: the grant's organization_id is always the GRANTEE's org,
    load-bearing specifically for the seller-managed case).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.models.ai_connection import AIConnectionGranteeType, AIConnectionPermission
from app.models.connection import Connection, ConnectionCategory
from app.models.role import Role
from app.models.user import User
from app.services import ai_quota_service
from app.services.ai_connection_access_service import ai_connection_access_service


class AiAccessError(Exception):
    """Bad request — out-of-scope caller, connection not in the visibility
    set, or an attempt to toggle is_public on a connection this org
    doesn't own."""


def _label_prefix(grantee_type: str) -> str:
    return "User" if grantee_type == AIConnectionGranteeType.USER.value else "Role"


async def _entitled_seller_managed(db: AsyncSession, organization_id: int, connections: list[Connection]) -> list[Connection]:
    """Drops any seller-managed (NULL-org) connection this org holds no
    active AI_CONNECTION entitlement for — a customer-owned connection
    always passes through untouched (no entitlement concept applies to it).
    Real gap found live: an org with ZERO AI_CONNECTION purchase could
    still see every seller-managed connection's name/type on the "Manage
    AI Access" screen — the visibility list only ever checked category/
    is_active/org-or-null, never entitlement. check_access() itself was
    already correct (a later, stricter gate), but by the time a user gets
    there the name has already leaked. Reuses the exact same entitlement
    test check_access() uses, so "can this org ever see it" and "can this
    org actually use it" never disagree about WHICH providers an org's
    subscription covers."""
    kept = []
    for conn in connections:
        if conn.organization_id is not None:
            kept.append(conn)
            continue
        if await ai_connection_access_service.has_active_ai_entitlement(db, organization_id, conn.connection_type):
            kept.append(conn)
    return kept


async def _visible_connections(db: AsyncSession, organization_id: int) -> list[Connection]:
    stmt = select(Connection).where(
        Connection.category == ConnectionCategory.AI_PROVIDER,
        Connection.is_active.is_(True),
        (Connection.organization_id == organization_id) | (Connection.organization_id.is_(None)),
    ).order_by(Connection.organization_id.is_(None), Connection.name)
    result = await db.execute(stmt)
    connections = list(result.scalars().all())
    return await _entitled_seller_managed(db, organization_id, connections)


async def _get_visible_connection(db: AsyncSession, connection_id: int, organization_id: int) -> Connection:
    stmt = select(Connection).where(
        Connection.id == connection_id,
        Connection.category == ConnectionCategory.AI_PROVIDER,
        (Connection.organization_id == organization_id) | (Connection.organization_id.is_(None)),
    )
    result = await db.execute(stmt)
    conn = result.scalar_one_or_none()
    if conn is None or not await _entitled_seller_managed(db, organization_id, [conn]):
        raise AiAccessError("AI connection not found (it may belong to a different organization).")
    return conn


async def _grantee_labels(grants: list[AIConnectionPermission]) -> dict[tuple[str, int], str]:
    """Resolves USER/ROLE grantee_ids to a human label via a PlatformBase
    session — User/Role live across the tenant boundary from the
    AIConnectionPermission rows themselves (same cross-boundary pattern
    as connection_service.owner_name_map())."""
    user_ids = {g.grantee_id for g in grants if g.grantee_type == AIConnectionGranteeType.USER}
    role_ids = {g.grantee_id for g in grants if g.grantee_type == AIConnectionGranteeType.ROLE}
    labels: dict[tuple[str, int], str] = {}
    if not user_ids and not role_ids:
        return labels
    async with AsyncSessionLocal() as platform_db:
        if user_ids:
            result = await platform_db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(user_ids)))
            for uid, full_name, email in result.all():
                labels[("USER", uid)] = full_name or email
        if role_ids:
            result = await platform_db.execute(select(Role.id, Role.name).where(Role.id.in_(role_ids)))
            for rid, name in result.all():
                labels[("ROLE", rid)] = name
    return labels


async def list_access(db: AsyncSession, *, organization_id: int) -> list[dict]:
    connections = await _visible_connections(db, organization_id)
    if not connections:
        return []
    conn_ids = [c.id for c in connections]
    result = await db.execute(
        select(AIConnectionPermission).where(
            AIConnectionPermission.connection_id.in_(conn_ids),
            AIConnectionPermission.organization_id == organization_id,
        )
    )
    all_grants = list(result.scalars().all())
    labels = await _grantee_labels(all_grants)
    grants_by_conn: dict[int, list[AIConnectionPermission]] = {}
    for g in all_grants:
        grants_by_conn.setdefault(g.connection_id, []).append(g)

    items = []
    for conn in connections:
        is_seller_managed = conn.organization_id is None
        # AIC-P7: real, live usage/quota snapshot — None for a customer-
        # owned connection or when this org has no AI_CONNECTION
        # entitlement covering it (see get_usage_summary()'s own docstring).
        usage = await ai_quota_service.get_usage_summary(db, organization_id=organization_id, connection=conn) \
            if is_seller_managed else None
        items.append({
            "connection_id": conn.id,
            "name": conn.name or f"{conn.connection_type.value} connection #{conn.id}",
            "connection_type": conn.connection_type.value,
            "is_seller_managed": is_seller_managed,
            "is_public": conn.is_public,
            "can_toggle_public": not is_seller_managed,
            "usage": usage,
            "grants": [
                {
                    "id": g.id, "grantee_type": g.grantee_type.value, "grantee_id": g.grantee_id,
                    "grantee_label": labels.get((g.grantee_type.value, g.grantee_id), f"{_label_prefix(g.grantee_type.value)} #{g.grantee_id}"),
                    "can_view": g.can_view, "can_use": g.can_use, "created_at": g.created_at,
                }
                for g in sorted(grants_by_conn.get(conn.id, []), key=lambda g: g.created_at)
            ],
        })
    return items


async def grantee_options(db: AsyncSession, *, organization_id: int) -> dict:
    """Candidate USER/ROLE pickers for the grant form — this org's own
    users and custom roles only; a grant naming another org's user/role
    id would be meaningless (AIConnectionPermission.organization_id is
    always this org, so the grantee must belong to it)."""
    async with AsyncSessionLocal() as platform_db:
        user_result = await platform_db.execute(
            select(User.id, User.full_name, User.email).where(User.organization_id == organization_id, User.is_active.is_(True))
        )
        users = [{"id": uid, "label": full_name or email} for uid, full_name, email in user_result.all()]
        role_result = await platform_db.execute(select(Role.id, Role.name).where(Role.organization_id == organization_id))
        roles = [{"id": rid, "label": name} for rid, name in role_result.all()]
    return {"users": users, "roles": roles}


async def set_public(db: AsyncSession, *, connection_id: int, organization_id: int, is_public: bool) -> Connection:
    conn = await _get_visible_connection(db, connection_id, organization_id)
    if conn.organization_id != organization_id:
        raise AiAccessError(
            "This connection is managed by your AI provider seller — its visibility is a platform-wide setting, "
            "not something your organization controls. Use the grants below to control which of your users can use it."
        )
    conn.is_public = is_public
    await db.commit()
    await db.refresh(conn)
    return conn


async def upsert_grant(
    db: AsyncSession, *, connection_id: int, organization_id: int, grantee_type: str,
    grantee_id: int, can_view: bool, can_use: bool, granted_by: int,
) -> AIConnectionPermission:
    await _get_visible_connection(db, connection_id, organization_id)  # raises if out of scope
    try:
        grantee_type_enum = AIConnectionGranteeType(grantee_type)
    except ValueError:
        raise AiAccessError(f"'{grantee_type}' must be USER or ROLE.")

    result = await db.execute(
        select(AIConnectionPermission).where(
            AIConnectionPermission.connection_id == connection_id,
            AIConnectionPermission.grantee_type == grantee_type_enum,
            AIConnectionPermission.grantee_id == grantee_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = AIConnectionPermission(
            connection_id=connection_id, organization_id=organization_id,
            grantee_type=grantee_type_enum, grantee_id=grantee_id, granted_by=granted_by,
        )
        db.add(row)
    elif row.organization_id != organization_id:
        # Can't happen via this org's own grantee picker (grantee_options()
        # only offers this org's own users/roles), but defends against a
        # grantee_id collision with a DIFFERENT org's identically-id'd
        # user/role on the SAME shared seller-managed connection.
        raise AiAccessError("This grantee belongs to a different organization.")
    row.can_view = can_view
    row.can_use = can_use
    row.granted_by = granted_by
    await db.commit()
    await db.refresh(row)

    labels = await _grantee_labels([row])
    label = labels.get((row.grantee_type.value, row.grantee_id), f"{_label_prefix(row.grantee_type.value)} #{row.grantee_id}")
    return {
        "id": row.id, "grantee_type": row.grantee_type.value, "grantee_id": row.grantee_id,
        "grantee_label": label, "can_view": row.can_view, "can_use": row.can_use, "created_at": row.created_at,
    }


async def remove_grant(db: AsyncSession, *, connection_id: int, organization_id: int, grant_id: int) -> None:
    result = await db.execute(
        select(AIConnectionPermission).where(
            AIConnectionPermission.id == grant_id,
            AIConnectionPermission.connection_id == connection_id,
            AIConnectionPermission.organization_id == organization_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise AiAccessError("Grant not found.")
    await db.delete(row)
    await db.commit()
