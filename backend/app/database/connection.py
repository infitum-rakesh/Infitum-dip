from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=settings.app_debug,
    future=True,
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


class Base(DeclarativeBase):
    pass


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
    """Create all tables (runs on startup)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
