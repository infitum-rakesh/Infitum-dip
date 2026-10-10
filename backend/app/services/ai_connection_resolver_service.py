"""
ai_connection_resolver_service — "default AI connection selection" (AIC-P6),
one of the master spec's five AI Connection feature bullets. Answers "which
ONE AI connection should this feature use when the caller hasn't picked
one explicitly" with a real, disclosed precedence chain — never a silent
first-row pick (see models/ai_connection.py's AIConnectionDefault
docstring, which this module implements):

  1. This specific user's own configured default (AIConnectionDefault row
     keyed on their real user_id).
  2. This organization's org-wide default (the same table, keyed on the
     ORG_DEFAULT_USER_ID sentinel row — see that constant's own docstring
     for why a sentinel int rather than a nullable column).
  3. The org's Settings > AI > "AI Provider" TYPE preference (already a
     real setting — see document_service.py's _pick_ai_connections()),
     resolved to a specific connection: the first active, accessible
     connection of that type. This is a real, disclosed FALLBACK tier,
     not a second "default" concept — it only engages when neither 1 nor
     2 has ever been configured.
  4. None. A caller that needs a hard requirement (vs. being willing to
     try several candidates — see _pick_ai_connections()'s own multi-
     candidate resilience) gets a clear "no default configured" rather
     than whatever happens to sort first.

Every candidate at every tier is re-validated through
ai_connection_access_service.check_access() (required="use") before being
returned — a configured default that's since been disabled, revoked, or
no longer entitled is skipped, falling through to the next tier, rather
than handing back a connection the caller can't actually use.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_connection import ORG_DEFAULT_USER_ID, AIConnectionDefault
from app.models.connection import Connection, ConnectionCategory, ConnectionStatus, ConnectionType
from app.models.user import User
from app.services import settings_service
from app.services.ai_connection_access_service import ai_connection_access_service

# Same mapping document_service._pick_ai_connections() uses for its own
# (coarser, type-only) preference sort — owned here since this module is
# the one both that function and this resolver's own fallback tier need,
# and resolver_service has no dependency the other direction that would
# make this a circular import.
AI_PROVIDER_SETTING_TO_CONNECTION_TYPE = {
    "openai": ConnectionType.OPENAI,
    "anthropic": ConnectionType.ANTHROPIC,
    "ollama": ConnectionType.OLLAMA,
}


class AiDefaultError(Exception):
    """Bad request — connection not visible to this org, or caller lacks
    USE access to the connection being set as a default."""


async def _get_default_row(db: AsyncSession, organization_id: int, user_id: int) -> AIConnectionDefault | None:
    result = await db.execute(
        select(AIConnectionDefault).where(
            AIConnectionDefault.organization_id == organization_id, AIConnectionDefault.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _resolve_row_if_accessible(
    db: AsyncSession, row: AIConnectionDefault | None, *, organization_id: int, acting_user: User | None,
) -> Connection | None:
    if row is None:
        return None
    access = await ai_connection_access_service.check_access(
        db, organization_id=organization_id, acting_user=acting_user, connection_id=row.connection_id, required="use",
    )
    return access.connection if access.granted else None


async def _settings_fallback(db: AsyncSession, organization_id: int, acting_user: User | None) -> Connection | None:
    org_settings = await settings_service.get_or_create(db, organization_id)
    ai_settings = settings_service.effective_ai_settings(org_settings)
    preferred_type = AI_PROVIDER_SETTING_TO_CONNECTION_TYPE.get(ai_settings.get("ai_provider"))
    if preferred_type is None:
        return None
    stmt = select(Connection).where(
        Connection.category == ConnectionCategory.AI_PROVIDER,
        Connection.connection_type == preferred_type,
        Connection.status == ConnectionStatus.ACTIVE,
        Connection.is_active.is_(True),
        (Connection.organization_id == organization_id) | (Connection.organization_id.is_(None)),
    ).order_by(Connection.organization_id.is_(None), Connection.id.asc())
    result = await db.execute(stmt)
    candidates = list(result.scalars().all())
    accessible = await ai_connection_access_service.filter_accessible(
        db, connections=candidates, organization_id=organization_id, acting_user=acting_user, required="use",
    )
    return accessible[0] if accessible else None


async def resolve_default(db: AsyncSession, *, organization_id: int | None, acting_user: User | None = None) -> Connection | None:
    """The real resolution entry point — see module docstring for the
    full precedence. organization_id=None (a platform/Seller caller with
    no tenant context) has no meaningful "default" to resolve."""
    if organization_id is None:
        return None

    if acting_user is not None:
        user_row = await _get_default_row(db, organization_id, acting_user.id)
        conn = await _resolve_row_if_accessible(db, user_row, organization_id=organization_id, acting_user=acting_user)
        if conn is not None:
            return conn

    org_row = await _get_default_row(db, organization_id, ORG_DEFAULT_USER_ID)
    conn = await _resolve_row_if_accessible(db, org_row, organization_id=organization_id, acting_user=acting_user)
    if conn is not None:
        return conn

    return await _settings_fallback(db, organization_id, acting_user)


async def get_defaults(db: AsyncSession, *, organization_id: int, acting_user: User) -> dict:
    """Powers the Manage AI Access screen's "My Default" / "Org Default"
    display — the raw configured rows (even if since inaccessible —
    surfaced honestly rather than hidden) plus the actually-resolved
    effective connection."""
    user_row = await _get_default_row(db, organization_id, acting_user.id)
    org_row = await _get_default_row(db, organization_id, ORG_DEFAULT_USER_ID)
    effective = await resolve_default(db, organization_id=organization_id, acting_user=acting_user)
    return {
        "user_default_connection_id": user_row.connection_id if user_row else None,
        "org_default_connection_id": org_row.connection_id if org_row else None,
        "effective_connection_id": effective.id if effective else None,
    }


async def _validate_settable(db: AsyncSession, *, connection_id: int, organization_id: int, acting_user: User) -> None:
    access = await ai_connection_access_service.check_access(
        db, organization_id=organization_id, acting_user=acting_user, connection_id=connection_id, required="use",
    )
    if not access.granted:
        raise AiDefaultError(access.denial_message or "You don't have access to use this connection, so it can't be set as a default.")


async def set_user_default(db: AsyncSession, *, organization_id: int, user_id: int, connection_id: int, created_by: int) -> None:
    await _validate_settable(db, connection_id=connection_id, organization_id=organization_id, acting_user=await db.get(User, user_id))
    await _upsert(db, organization_id=organization_id, user_id=user_id, connection_id=connection_id, created_by=created_by)


async def set_org_default(db: AsyncSession, *, organization_id: int, connection_id: int, created_by: int) -> None:
    # Validated against the ACTOR setting it (an org admin), not against
    # the sentinel row — ORG_DEFAULT_USER_ID isn't a real user to check
    # access_service's per-user grant lookup against, and the admin
    # setting an org default must themselves already be able to use it.
    await _validate_settable(db, connection_id=connection_id, organization_id=organization_id, acting_user=await db.get(User, created_by))
    await _upsert(db, organization_id=organization_id, user_id=ORG_DEFAULT_USER_ID, connection_id=connection_id, created_by=created_by)


async def _upsert(db: AsyncSession, *, organization_id: int, user_id: int, connection_id: int, created_by: int) -> None:
    row = await _get_default_row(db, organization_id, user_id)
    if row is None:
        row = AIConnectionDefault(organization_id=organization_id, user_id=user_id, connection_id=connection_id, created_by=created_by)
        db.add(row)
    else:
        row.connection_id = connection_id
        row.created_by = created_by
    await db.commit()


async def clear_default(db: AsyncSession, *, organization_id: int, user_id: int) -> None:
    row = await _get_default_row(db, organization_id, user_id)
    if row is None:
        return
    await db.delete(row)
    await db.commit()
