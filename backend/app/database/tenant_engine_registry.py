"""
tenant_engine_registry — the async analogue of connectors/database/
pool_manager.py (Phase 1 of the ETL Connections feature): a process-wide
cache of one async_sessionmaker per organization, so a live HTTP request
doesn't rebuild a connection pool on every call.

Cache key is `organization_id` (or the literal Python `None` for the
Seller/platform-wide row) mapped to an `async_sessionmaker` — NOT
necessarily one built from a real external engine. The overwhelming
common case, for every org until they complete a real migration/cutover
(Bring-Your-Own-Database Phase 4), is that `TenantDatabase.is_active` is
False or no row exists at all — get_sessionmaker() returns the existing
shared `AsyncSessionLocal` (backend/app/database/connection.py) BY
IDENTITY in that case, so database/tenant_routing.get_tenant_db() can
detect it and skip opening a second session entirely. An org with no
active TenantDatabase row costs nothing.

Only once an org's row is genuinely `is_active=True` does this module
build a real `create_async_engine()` against their external database,
using the same config/encrypted_secrets shape Phase 1's
tenant_database_service.py already validates and Test-Connects — just
with async dialect drivers instead of the sync ones connectors/database/
{postgres,mysql}.py use for the one-shot Test Connection call.

Phase 6 (platform-level migration): an org with no individual active
TenantDatabase row doesn't fall straight to AsyncSessionLocal anymore —
it first cascades to the Seller's platform-wide row
(organization_id=None). Once the Seller cuts the whole platform over to
their own database, every org without its own override transparently
starts routing there too. See get_sessionmaker()'s docstring for the
exact cascade, and evict_all() for why a platform cutover needs a
harsher cache invalidation than a single evict().

SQL_SERVER is intentionally unsupported here — disclosed, not silently
skipped. The sync SqlServerConnector (connectors/database/sqlserver.py)
deliberately uses pymssql/FreeTDS specifically to avoid needing the
system ODBC driver manager + msodbcsql driver installed on the host; the
only SQLAlchemy-supported async mssql dialect (mssql+aioodbc) needs
exactly that dependency back. Until a real pure-Python (or already-
guaranteed-present) async SQL Server driver exists, `get_sessionmaker()`
raises `TenantRoutingUnsupportedError` for an active SQL_SERVER row
rather than pretending a sync driver can back an AsyncSession (it
can't — SQLAlchemy's AsyncEngine requires a real async DBAPI underneath).
"""
import asyncio

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core import crypto
from app.database.connection import AsyncSessionLocal
from app.services import tenant_database_service

_PLATFORM_KEY = None  # organization_id is already None for the Seller row — no separate sentinel needed

_cache: dict[int | None, async_sessionmaker] = {}
_lock = asyncio.Lock()

# Orgs currently mid-cutover (Phase 4 — tenant_migration_service.py's
# CUTTING_OVER step) — checked by database/tenant_routing.get_tenant_db()
# to 503 a WRITE for that org during the brief pause, while reads keep
# working from the still-active source. Deliberately in-memory (same
# single-process disclosure as _running_tasks/_cancelled elsewhere in
# this codebase) rather than a DB column poll on every request — cheap,
# and the cutover pause is itself only ever seconds long.
_migrating: set[int | None] = set()

_ASYNC_DEFAULT_PORT = {"POSTGRESQL": 5432, "MYSQL": 3306}


class TenantRoutingUnsupportedError(Exception):
    """Raised when an org's active TenantDatabase.connection_type has no
    real async driver wired up yet (currently: SQL_SERVER — see module
    docstring). Never silently falls back to the platform DB for an org
    that's genuinely supposed to be routed elsewhere."""


def _build_url(connection_type: str, config: dict, secrets: dict) -> URL:
    port = int(config.get("port") or _ASYNC_DEFAULT_PORT.get(connection_type, 0))
    if connection_type == "POSTGRESQL":
        ssl_mode = {"Disable": "disable", "Allow": "allow", "Require": "require", "Verify-Full": "verify-full"}.get(config.get("ssl_mode"), "require")
        return URL.create(
            "postgresql+asyncpg", username=config.get("username"), password=secrets.get("password"),
            host=config.get("host"), port=port, database=config.get("database"),
            query={"ssl": "true"} if ssl_mode not in ("disable",) else {},
        )
    if connection_type == "MYSQL":
        return URL.create(
            "mysql+aiomysql", username=config.get("username"), password=secrets.get("password"),
            host=config.get("host"), port=port, database=config.get("database"),
        )
    raise TenantRoutingUnsupportedError(
        f"Live tenant routing for '{connection_type}' isn't supported yet — only PostgreSQL and MySQL have a "
        f"real async driver wired up (see tenant_engine_registry.py's module docstring for why SQL Server doesn't)."
    )


async def build_engine_and_sessionmaker(row) -> tuple[AsyncEngine, async_sessionmaker]:
    """Public building block — builds a REAL async engine + sessionmaker
    for a given TenantDatabase row, independent of `row.is_active` and
    independent of `_cache`. Used internally by get_sessionmaker() (for
    the request-routing path, cached, only once is_active=True) and
    directly by tenant_migration_service.py (Phase 4), which needs a real
    connection to the TARGET database well before cutover flips
    is_active — to provision its schema, copy rows into it, and verify
    counts, all while the org's live traffic still runs against the
    source. Each call opens a fresh engine (a migration runs once per
    org, not on a hot path, so there's no cache to maintain here)."""
    secret_values = {
        key: crypto.decrypt_secret(token)
        for key, token in (row.encrypted_secrets or {}).items()
    }
    url = _build_url(row.connection_type, row.config, secret_values)
    engine = create_async_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def get_sessionmaker(db, organization_id: int | None) -> async_sessionmaker:
    """`db` is a live AsyncSession (from the request's own get_db()) used
    only to look up the TenantDatabase row — cheap, and already open for
    every request via get_current_organization's own dependency chain.

    An org with no individual active TenantDatabase row (the overwhelming
    common case) doesn't fall straight to AsyncSessionLocal — it cascades
    to the Seller's platform-wide row (organization_id=None) first
    (Phase 6's platform-level migration: once the Seller cuts the whole
    platform over to their own database, every org without its own
    individual override should transparently start routing there too,
    with zero code changes anywhere else). Only if THAT is also
    unconfigured/inactive does it fall back to AsyncSessionLocal — which
    is exactly what happens today for organization_id=None itself, so
    this recursion bottoms out in one extra step, never loops.

    The cascade recursion happens OUTSIDE `_lock` deliberately —
    `asyncio.Lock` is not reentrant, so a real org's call recursing into
    get_sessionmaker(db, None) while still holding `_lock` from its own
    call would deadlock this task against itself forever (and since
    nearly every request path takes this same lock, that one stuck task
    would eventually wedge the whole process). Only the "this key's own
    active row" fast path stays fully lock-protected against duplicate
    engine builds; the cascade fallback re-acquires the lock just to
    write the final result into `_cache`."""
    if organization_id in _cache:
        return _cache[organization_id]
    async with _lock:
        if organization_id in _cache:
            return _cache[organization_id]
        row = await tenant_database_service.get_row(db, organization_id)
        if row is not None and row.is_active:
            _, sessionmaker = await build_engine_and_sessionmaker(row)
            _cache[organization_id] = sessionmaker
            return sessionmaker
        if organization_id is None:
            sessionmaker = AsyncSessionLocal
            _cache[organization_id] = sessionmaker
            return sessionmaker
    sessionmaker = await get_sessionmaker(db, None)
    async with _lock:
        _cache[organization_id] = sessionmaker
    return sessionmaker


async def get_background_sessionmaker(organization_id: int | None) -> async_sessionmaker:
    """For background tasks (execution/*.py, monitoring/service.py,
    document_service.py, reject_service.py — fired via asyncio.create_task
    from an HTTP handler, long after that request's own session has
    closed) that have no live request-scoped session to reuse for the
    TenantDatabase lookup. On a cache hit (the common case, after the
    first call for a given org) this costs nothing extra — same as
    get_tenant_db(). On a miss, opens one short-lived platform-DB session
    just for that lookup, exactly like get_row() already assumes."""
    if organization_id in _cache:
        return _cache[organization_id]
    async with AsyncSessionLocal() as db:
        return await get_sessionmaker(db, organization_id)


def evict(organization_id: int | None) -> None:
    """Invalidate a cached sessionmaker — called whenever a
    TenantDatabase row's config/status changes (tenant_database_service)
    so a stale engine (old credentials, or a since-flipped is_active)
    never keeps answering. Disposing the underlying engine isn't done
    here deliberately: any requests already holding a session from it
    finish naturally; the next lookup just builds a fresh one."""
    _cache.pop(organization_id, None)


def evict_all() -> None:
    """Invalidate every cached sessionmaker — called once after a
    platform-level (organization_id=None) cutover completes, instead of
    a plain evict(None). A single evict(None) isn't enough here: any org
    without its own individual override cascaded through
    get_sessionmaker() and cached the *fallback* sessionmaker by
    identity (either the old AsyncSessionLocal, or nothing yet at all),
    keyed under its OWN organization_id — not under the None key that
    evict(None) clears. Once the Seller's platform row flips to active,
    every one of those cached fallback entries is stale and must be
    dropped too, so the next request for that org recomputes the
    cascade and picks up the new platform engine."""
    _cache.clear()


def mark_migrating(organization_id: int | None) -> None:
    """Called by tenant_migration_service.py right as it flips
    TenantDatabase.status to MIGRATING for the CUTTING_OVER step."""
    _migrating.add(organization_id)


def clear_migrating(organization_id: int | None) -> None:
    """Called once cutover finishes (success or failure) — the pause is
    only ever meant to last as long as that one step actually runs."""
    _migrating.discard(organization_id)


def is_migrating(organization_id: int | None) -> bool:
    return organization_id in _migrating
