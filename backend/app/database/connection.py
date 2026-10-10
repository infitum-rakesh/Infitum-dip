import json

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()

# Railway (and most managed Postgres providers) hand out a bare
# postgres://.../postgresql://... DATABASE_URL with no driver suffix,
# but create_async_engine() needs an async-capable DBAPI in the scheme —
# asyncpg (already in requirements.txt) rather than the sync psycopg2
# that bare "postgresql://" would otherwise resolve to. Normalize here
# so any Postgres DATABASE_URL just works without hand-editing the env
# var to add "+asyncpg" (and because psycopg2-binary is also installed
# for Alembic-less tooling, SQLAlchemy would silently pick that sync
# driver instead of erroring, then fail opaquely on the first await).
_database_url = settings.database_url
if _database_url.startswith("postgres://"):
    _database_url = "postgresql+asyncpg://" + _database_url[len("postgres://"):]
elif _database_url.startswith("postgresql://"):
    _database_url = "postgresql+asyncpg://" + _database_url[len("postgresql://"):]

# Multilingual Document Processing — real bug found live during repository
# analysis (Phase 1/2 of the multilingual build): SQLAlchemy's JSON column
# type falls back to plain json.dumps() when no json_serializer is given
# to create_async_engine(), and json.dumps()'s own default is
# ensure_ascii=True — so every JSON-typed column in this app (Document.
# ocr_result/extraction_result/timeline/ai_raw_response, ApInvoice.
# decision_reasons/ai_raw_response, ApInvoiceLine.evidence, etc.) was
# writing Kannada/Tamil/Hindi (or any non-ASCII) text as \uXXXX escape
# sequences instead of real UTF-8 glyphs. This never corrupted data (it
# round-trips correctly through json.loads()/the ORM), but it broke any
# raw-SQL/LIKE/full-text read of those columns, and would have shown
# escape sequences instead of glyphs anywhere a JSON column's raw text
# got displayed without being re-parsed first. SQLite itself is UTF-8
# native (confirmed live via `PRAGMA encoding` + a real Kannada round-
# trip through the sqlite3 CLI) — this was purely an unconfigured
# application-layer default, not a database limitation.
def json_serializer(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


_is_sqlite = settings.database_url.startswith("sqlite")

# NullPool for SQLite: a pooled aiosqlite connection's background worker
# thread is bound to whichever asyncio event loop was running when the
# connection was opened. A long-lived pool that hands the same physical
# connection to a LATER call running on a DIFFERENT loop (e.g. this
# project's own test suite, which runs each test function in its own
# short-lived event loop against one shared engine — see
# backend/tests/conftest.py) breaks with "RuntimeError: Event loop is
# closed" the moment that stale connection's thread tries to signal back
# to the loop that created it. NullPool opens a fresh connection per
# checkout and fully closes it on checkin, so no connection ever
# outlives the loop it was created on — this is SQLAlchemy's own
# documented fix for this exact aiosqlite failure mode. Real production
# traffic runs one persistent event loop for the process lifetime so this
# never bites there; the only cost is a (cheap, local-file) connection
# open per session instead of a pooled reuse.
engine = create_async_engine(
    _database_url,
    echo=settings.app_debug,
    future=True,
    json_serializer=json_serializer,
    poolclass=NullPool if _is_sqlite else None,
)

# WAL mode lets readers and a writer proceed concurrently instead of
# blocking on SQLite's default single-writer file lock, and busy_timeout
# makes a writer that does momentarily collide retry for up to 15s instead
# of failing immediately with "database is locked". Needed as of the
# Phase 3 execution engine (execution/worker.py), which is the first part
# of this codebase to issue genuinely concurrent DB writes — multiple
# partition workers checkpointing progress at the same time. Raised from
# 5000 to 15000 for Document Intelligence bulk upload (document_service.py
# start_processing()), which fires N full pipeline runs as concurrent
# background tasks that each commit once at the end — observed live with
# just 3 concurrent documents still occasionally exceeding 5s. Even at
# 15s this can still genuinely collide under heavier load, which is why
# process_document()/​_process_in_background() also recover from a broken
# session instead of assuming busy_timeout alone makes this impossible.
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class PlatformBase(DeclarativeBase):
    """Identity + routing tables — Users, Organizations, Sellers, Roles,
    SSO config, sessions, and the TenantDatabase routing config itself.
    Always lives on this shared platform database, never switches — the
    app must resolve who's asking (and which org they belong to) before it
    can know which database to route the rest of the request to. See
    database/tenant_routing.py + database/tenant_engine_registry.py."""


class TenantBase(DeclarativeBase):
    """Org business data — Documents, Pipelines, Validation/Reconciliation,
    Connections, SQL Workspace, Monitoring, etc. Lives here (this same
    shared database) until an org's TenantDatabase.is_active flips True
    (Bring-Your-Own-Database migration/cutover — a later phase), at which
    point database/tenant_routing.get_tenant_db() routes that org's
    requests to their own external database instead. Every model on this
    base whose organization_id/owner_id/created_by-style column used to
    point at a PlatformBase table (organizations.id, users.id) keeps the
    column but drops the SQL-level ForeignKey wrapper — a literal FK
    constraint can't span two separate physical databases once an org
    migrates, the same precedent Organization.seller_id (models/
    organization.py) already established for the Seller boundary."""


async def get_db() -> AsyncSession:  # type: ignore[return]
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Create all tables (runs on startup). Both metadata objects are
    created against this same engine — as of Phase 2 of the Bring-Your-
    Own-Database feature, no org has actually cut over to a separate
    physical database yet (TenantDatabase.is_active is still False for
    everyone), so PlatformBase and TenantBase tables continue to live
    together in one file exactly as the single `Base` used to produce."""
    async with engine.begin() as conn:
        await conn.run_sync(PlatformBase.metadata.create_all)
        await conn.run_sync(TenantBase.metadata.create_all)
