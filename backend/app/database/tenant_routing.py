"""
get_tenant_db — the tenant-tier counterpart of database/connection.get_db(),
for routers touching any TenantBase model (Documents, Pipelines, Validation,
Reconciliation, Connections, SQL Workspace, Monitoring, ...). Resolves the
caller's organization (via the same get_current_organization dependency
require_action()/require_seller_action() already use) and hands back a
session bound to wherever that org's TenantBase tables currently live —
today, for every org until Bring-Your-Own-Database's migration/cutover
phase actually runs, that's this same shared database, so get_tenant_db is
behaviorally identical to get_db (see tenant_engine_registry.py).

Seller-tier callers (organization_id is None) resolve to the platform-wide
TenantDatabase row (organization_id IS NULL) the exact same way — the
Seller Portal itself doesn't use this dependency (its routers only touch
PlatformBase models), but reconciliation_service/validation_service/etc.
already treat organization_id=None as "platform-wide scope" for a
SUPER_ADMIN caller, and this dependency honors that identically.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends, HTTPException, Request, status

from app.core.deps import get_current_organization
from app.database import tenant_engine_registry
from app.database.connection import AsyncSessionLocal, get_db
from app.models.organization import Organization

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def get_tenant_db(
    request: Request,
    org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
) -> AsyncSession:  # type: ignore[return]
    organization_id = org.id if org is not None else None

    # Brief-pause write-gate (Phase 4 — tenant_migration_service.py's
    # CUTTING_OVER step): a write mid-cutover could land on whichever
    # side of the flip loses the race, silently dropping it. Reads are
    # never gated — the source is still fully live and correct right up
    # until the flip actually happens.
    #
    # Phase 6: a platform-level (organization_id=None) cutover also gates
    # every OTHER org's writes, not just the platform row's own. Any org
    # without its own individual TenantDatabase override cascades to the
    # platform row in get_sessionmaker() — precisely determining which
    # orgs currently resolve that way (vs. having their own active
    # override, unaffected by this cutover) would mean re-running the
    # same lookup this dependency already does downstream just to decide
    # whether to gate. Simpler and safer to pause every org's writes for
    # the platform cutover's few seconds than to risk underpausing one
    # that turns out to be riding the fallback.
    if request.method not in _SAFE_METHODS and (
        tenant_engine_registry.is_migrating(organization_id) or tenant_engine_registry.is_migrating(None)
    ):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "This organization's backend database is completing a migration cutover — writes are "
            "paused for a few seconds. Please retry.",
        )

    sessionmaker = await tenant_engine_registry.get_sessionmaker(db, organization_id)

    if sessionmaker is AsyncSessionLocal:
        # Common case (every org until they actually migrate): the
        # request's own already-open `db` session already IS the right
        # one — reuse it instead of opening a second session for nothing.
        yield db
        return

    async with sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
