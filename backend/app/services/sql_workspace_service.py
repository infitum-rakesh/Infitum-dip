"""
sql_workspace_service — the API-facing layer for Phase 9's SQL Workspace.
Owns SqlWorkspaceQuery CRUD (saved query text) and the real, stateless
run_sql() that executes a query against a live connection and returns
results directly — same shape as mapping_service.run_preview(), just not
restricted to SELECT/WITH.
"""
from __future__ import annotations

import asyncio
import threading

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal

from app.connectors.base import ConnectorError, ConnectorUnsupportedError, ExplainResult, SqlRunResult
from app.models.connection import Connection, ConnectionEnvironment
from app.models.query_execution_log import SQL_TEXT_CAP, QueryExecutionLog
from app.models.sql_workspace import SqlWorkspaceQuery, SqlWorkspaceQueryFavorite
from app.models.user import User
from app.services import connection_service, sql_validator


class SqlWorkspaceServiceError(Exception):
    """Bad request — a referenced connection doesn't exist (or isn't
    owned by this org), or the SQL itself failed."""


class SqlPermissionDenied(SqlWorkspaceServiceError):
    """The caller's REAL, granular SQL Workspace permission set (see
    role_service.effective_sql_permissions) doesn't cover what this
    specific statement needs — mapped to 403, not 400, at the API layer."""


class SqlQueryNotOwned(SqlWorkspaceServiceError):
    """Only a saved query's own creator (or an elevated role) may edit,
    delete, or reshare it — mapped to 403 at the API layer. Being able to
    VIEW a shared query (everyone in the org, once is_shared=True) is a
    separate, more permissive check from being able to CHANGE it."""


class SqlRunNotCancellable(SqlWorkspaceServiceError):
    """No in-flight run matches the given client_run_id — already
    finished, never started, belongs to someone else, or the connector
    doesn't have real cancellation wired for this connection type (see
    connectors/database/base.py's supports_query_cancel). Mapped to 400
    — never a fabricated "cancelled" success for a run that wasn't."""


# ── In-flight run registry (query cancellation) ─────────────────────
# Process-local, in-memory — good enough for a single-process dev/small
# deployment; a multi-process deployment would need this in something
# shared (Redis, etc.) instead, a real scaling limitation disclosed here
# rather than silently assumed away. Guarded by a plain threading.Lock
# (not an asyncio.Lock) because entries are written from the WORKER
# THREAD running the blocking DB call (see run_sql's on_session_ready
# callback below), not just from the asyncio event loop.
_ACTIVE_RUNS: dict[str, dict] = {}
_ACTIVE_RUNS_LOCK = threading.Lock()

TIMEOUT_MIN_SECONDS = 5
TIMEOUT_MAX_SECONDS = 300


def _clamp_timeout(seconds: int | None) -> int | None:
    if seconds is None:
        return None
    return max(TIMEOUT_MIN_SECONDS, min(int(seconds), TIMEOUT_MAX_SECONDS))


def _register_run(client_run_id: str, *, connector, organization_id: int | None, user_id: int | None) -> None:
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS[client_run_id] = {
            "connector": connector, "organization_id": organization_id, "user_id": user_id, "session_id": None,
        }


def _set_run_session_id(client_run_id: str, session_id: str | None) -> None:
    with _ACTIVE_RUNS_LOCK:
        entry = _ACTIVE_RUNS.get(client_run_id)
        if entry is not None:
            entry["session_id"] = session_id


def _unregister_run(client_run_id: str | None) -> None:
    if not client_run_id:
        return
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS.pop(client_run_id, None)


async def cancel_run(*, client_run_id: str, organization_id: int | None, user_id: int | None, elevated: bool) -> None:
    """Issues a REAL cancel against whatever statement client_run_id is
    currently running — see connector.cancel_session(). Only the run's
    own owner (or an elevated role) may cancel it, same ownership
    convention as saved queries."""
    with _ACTIVE_RUNS_LOCK:
        entry = _ACTIVE_RUNS.get(client_run_id)
    if entry is None:
        raise SqlRunNotCancellable("No running query found for this tab — it may have already finished.")
    if not elevated and (entry["organization_id"] != organization_id or entry["user_id"] != user_id):
        raise SqlRunNotCancellable("No running query found for this tab — it may have already finished.")
    session_id = entry["session_id"]
    if session_id is None:
        raise SqlRunNotCancellable("This query hasn't started running yet — try again in a moment.")
    connector = entry["connector"]
    try:
        await asyncio.to_thread(connector.cancel_session, session_id)
    except ConnectorUnsupportedError as exc:
        raise SqlRunNotCancellable(str(exc)) from exc
    except Exception as exc:
        raise SqlWorkspaceServiceError(connector._scrub(str(exc))) from exc


async def _verify_connection_owned(db: AsyncSession, connection_id: int, organization_id: int | None) -> None:
    """A connection_id in a request can't be trusted as-is — verify it
    genuinely belongs to this org (organization_id is None only for a
    true platform-wide/super-admin context) — same Phase 8 pattern as
    validation_service/reconciliation_service."""
    if organization_id is None:
        return
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise SqlWorkspaceServiceError(f"Connection {connection_id} was not found.")


# ── Saved queries CRUD ───────────────────────────────────────────

def ensure_owner_or_elevated(row: SqlWorkspaceQuery, user_id: int | None, elevated: bool) -> None:
    """Real ownership check for edit/delete/reshare — a query being
    visible (VIEW, once shared) is not the same as being changeable.
    elevated is passed in from the API layer (SUPER_ADMIN/ORG_ADMIN —
    see api/sql_workspace.py's _ELEVATED_ROLES), not re-derived here."""
    if elevated:
        return
    if row.created_by != user_id:
        raise SqlQueryNotOwned("Only this query's owner can make that change.")


async def create_query(
    db: AsyncSession, *, organization_id: int | None, name: str, connection_id: int,
    schema_name: str | None, sql_text: str, is_shared: bool, created_by: int | None,
) -> SqlWorkspaceQuery:
    await _verify_connection_owned(db, connection_id, organization_id)
    row = SqlWorkspaceQuery(
        organization_id=organization_id, name=name, connection_id=connection_id,
        schema_name=schema_name, sql_text=sql_text, is_shared=is_shared,
        created_by=created_by, updated_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_query(
    db: AsyncSession, row: SqlWorkspaceQuery, *, name: str | None, sql_text: str | None,
    is_shared: bool | None, updated_by: int | None,
) -> SqlWorkspaceQuery:
    if name:
        row.name = name
    if sql_text is not None:
        row.sql_text = sql_text
    if is_shared is not None:
        row.is_shared = is_shared
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


async def delete_query(db: AsyncSession, row: SqlWorkspaceQuery) -> None:
    await db.execute(SqlWorkspaceQueryFavorite.__table__.delete().where(SqlWorkspaceQueryFavorite.query_id == row.id))
    await db.delete(row)
    await db.commit()


async def get_query(db: AsyncSession, query_id: int, organization_id: int | None) -> SqlWorkspaceQuery | None:
    stmt = select(SqlWorkspaceQuery).where(SqlWorkspaceQuery.id == query_id)
    if organization_id is not None:
        stmt = stmt.where(SqlWorkspaceQuery.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_queries(
    db: AsyncSession, organization_id: int | None, *, scope: str, user_id: int | None,
) -> tuple[list[SqlWorkspaceQuery], int]:
    """scope: "mine" (created_by == user_id, regardless of is_shared),
    "shared" (is_shared == True, org-wide — includes the caller's own
    shared queries too, same as any teammate would see), or "favorites"
    (joined through SqlWorkspaceQueryFavorite for this user). Any other
    value returns every query in the org (used by nothing in the UI
    today, kept as an honest fallback rather than silently defaulting to
    one scope)."""
    stmt = select(SqlWorkspaceQuery)
    if organization_id is not None:
        stmt = stmt.where(SqlWorkspaceQuery.organization_id == organization_id)

    if scope == "mine":
        stmt = stmt.where(SqlWorkspaceQuery.created_by == user_id)
    elif scope == "shared":
        stmt = stmt.where(SqlWorkspaceQuery.is_shared.is_(True))
    elif scope == "favorites":
        stmt = stmt.join(
            SqlWorkspaceQueryFavorite, SqlWorkspaceQueryFavorite.query_id == SqlWorkspaceQuery.id,
        ).where(SqlWorkspaceQueryFavorite.user_id == user_id)

    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.updated_at, reverse=True)
    return rows, len(rows)


async def favorited_query_ids(db: AsyncSession, user_id: int | None, query_ids: set[int]) -> set[int]:
    if not user_id or not query_ids:
        return set()
    result = await db.execute(
        select(SqlWorkspaceQueryFavorite.query_id).where(
            SqlWorkspaceQueryFavorite.user_id == user_id, SqlWorkspaceQueryFavorite.query_id.in_(query_ids),
        )
    )
    return {row[0] for row in result.all()}


async def toggle_favorite(db: AsyncSession, row: SqlWorkspaceQuery, user_id: int) -> bool:
    """Real toggle — only a query the caller can actually see (their own,
    or shared) can be favorited; the API layer checks that before calling
    this. Returns the new favorited state."""
    existing = await db.execute(
        select(SqlWorkspaceQueryFavorite).where(
            SqlWorkspaceQueryFavorite.user_id == user_id, SqlWorkspaceQueryFavorite.query_id == row.id,
        )
    )
    fav = existing.scalar_one_or_none()
    if fav:
        await db.delete(fav)
        await db.commit()
        return False
    db.add(SqlWorkspaceQueryFavorite(user_id=user_id, query_id=row.id))
    await db.commit()
    return True


async def user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


# ── Run ──────────────────────────────────────────────────────────

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
_VALID_SOURCES = {"MANUAL", "AI_GENERATED"}


async def _log_execution(
    db: AsyncSession, *, organization_id: int | None, connection_id: int, user_id: int | None,
    sql: str, source: str, status: str, row_count: int | None, duration_ms: float | None,
    error_message: str | None,
) -> None:
    """Real, append-only audit row for every run_sql() attempt — success
    or failure — see models/query_execution_log.py. Logging failures
    (e.g. a transient DB write issue) are swallowed rather than masking
    the original run_sql() outcome the caller actually needs to see —
    the query itself already succeeded or failed for real before this
    ever runs; a lost audit row is a real gap worth catching in review,
    but must never turn a successful query into a failed API response."""
    classification = sql_validator.classify_statement(sql)
    try:
        db.add(QueryExecutionLog(
            organization_id=organization_id, connection_id=connection_id, user_id=user_id,
            sql_text=(sql or "")[:SQL_TEXT_CAP],
            statement_command=classification["command"], statement_category=classification["category"],
            source=source if source in _VALID_SOURCES else "MANUAL",
            status=status, row_count=row_count, duration_ms=duration_ms, error_message=error_message,
        ))
        await db.commit()
    except Exception:
        await db.rollback()


async def _check_statement_permission(
    db: AsyncSession, *, organization_id: int | None, connection_id: int, user_id: int | None,
    sql: str, source: str, permissions: set[str], conn: Connection,
) -> None:
    """Real, statement-aware gate — classifies the ACTUAL sql text (not
    just "did they hit /run at all") via sql_validator, and separately
    checks the target connection's real environment. A denial here is
    itself logged as a failed execution attempt (status=ERROR) — a
    permission-denied attempt is genuinely audit-worthy, not a no-op that
    should leave no trace. Raises SqlPermissionDenied — never silently
    downgrades a write to a no-op or a read."""
    classification = sql_validator.classify_statement(sql)
    category = classification["category"]
    command = classification["command"]
    reason = None
    if category == "WRITE" and "DML" not in permissions:
        reason = f"Your SQL Workspace permissions don't allow running {command} statements (requires the DML grant)."
    elif category in ("DDL", "DCL") and "DDL" not in permissions:
        reason = f"Your SQL Workspace permissions don't allow running {command} statements (requires the DDL grant)."
    elif conn.environment == ConnectionEnvironment.PRODUCTION and "PRODUCTION_ACCESS" not in permissions:
        reason = "This connection is flagged Production — running SQL against it requires the Production Access grant."
    if reason:
        await _log_execution(
            db, organization_id=organization_id, connection_id=connection_id, user_id=user_id,
            sql=sql, source=source, status="ERROR", row_count=None, duration_ms=None, error_message=reason,
        )
        raise SqlPermissionDenied(reason)


async def run_sql(
    db: AsyncSession, *, organization_id: int | None, connection_id: int, user_id: int | None,
    sql: str, parameters: dict | None, limit: int | None, source: str = "MANUAL",
    permissions: set[str] | None = None, client_run_id: str | None = None, timeout_seconds: int | None = None,
) -> SqlRunResult:
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise SqlWorkspaceServiceError(f"Connection {connection_id} was not found.")
    if permissions is not None:
        await _check_statement_permission(
            db, organization_id=organization_id, connection_id=connection_id, user_id=user_id,
            sql=sql, source=source, permissions=permissions, conn=conn,
        )
    connector = connection_service.build_connector(conn)
    bounded_limit = max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))
    bounded_timeout = _clamp_timeout(timeout_seconds)

    if client_run_id:
        _register_run(client_run_id, connector=connector, organization_id=organization_id, user_id=user_id)

    def _on_session_ready(session_id):
        # Called from the WORKER THREAD, as soon as the real connection's
        # session/backend id is known — BEFORE the actual statement runs
        # — so a Cancel request arriving while this call is still
        # blocking has something real to cancel.
        if client_run_id:
            _set_run_session_id(client_run_id, session_id)

    def _run():
        return connector.run_sql(
            sql, parameters or {}, bounded_limit,
            timeout_seconds=bounded_timeout, on_session_ready=_on_session_ready,
        )

    start = asyncio.get_event_loop().time()
    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:  # a real DB error (bad SQL, missing table, a real cancellation, ...) or connector error — surfaced, not swallowed, but scrubbed
        duration_ms = round((asyncio.get_event_loop().time() - start) * 1000, 1)
        message = connector._scrub(str(exc))
        await _log_execution(
            db, organization_id=organization_id, connection_id=connection_id, user_id=user_id,
            sql=sql, source=source, status="ERROR", row_count=None, duration_ms=duration_ms, error_message=message,
        )
        raise SqlWorkspaceServiceError(message) from exc
    finally:
        _unregister_run(client_run_id)

    duration_ms = round((asyncio.get_event_loop().time() - start) * 1000, 1)
    row_count = len(result.rows) if result.is_result_set else result.rowcount
    await _log_execution(
        db, organization_id=organization_id, connection_id=connection_id, user_id=user_id,
        sql=sql, source=source, status="SUCCESS", row_count=row_count, duration_ms=duration_ms, error_message=None,
    )
    return result


# ── Explain ──────────────────────────────────────────────────────

async def explain_sql(
    db: AsyncSession, *, organization_id: int | None, connection_id: int,
    sql: str, parameters: dict | None,
) -> ExplainResult:
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise SqlWorkspaceServiceError(f"Connection {connection_id} was not found.")
    connector = connection_service.build_connector(conn)

    def _run():
        return connector.explain(sql, parameters or {})

    try:
        return await asyncio.to_thread(_run)
    except ConnectorError as exc:
        raise SqlWorkspaceServiceError(connector._scrub(str(exc))) from exc
    except Exception as exc:
        raise SqlWorkspaceServiceError(connector._scrub(str(exc))) from exc


# ── History ──────────────────────────────────────────────────────

HISTORY_PAGE_SIZE_MAX = 200


async def list_history(
    db: AsyncSession, *, organization_id: int | None, user_id: int | None,
    connection_id: int | None, status_filter: str | None, own_only: bool,
    page: int, page_size: int,
) -> tuple[list[QueryExecutionLog], int]:
    """Real query history — every row is a genuine past run_sql() attempt
    (see _log_execution above), never synthesized. own_only restricts to
    the caller's own executions (the default for non-elevated roles is
    enforced at the API layer, not here — this just honors whatever the
    caller asks for)."""
    bounded_size = max(1, min(page_size or 50, HISTORY_PAGE_SIZE_MAX))
    stmt = select(QueryExecutionLog)
    count_stmt = select(func.count()).select_from(QueryExecutionLog)
    if organization_id is not None:
        stmt = stmt.where(QueryExecutionLog.organization_id == organization_id)
        count_stmt = count_stmt.where(QueryExecutionLog.organization_id == organization_id)
    if connection_id is not None:
        stmt = stmt.where(QueryExecutionLog.connection_id == connection_id)
        count_stmt = count_stmt.where(QueryExecutionLog.connection_id == connection_id)
    if status_filter:
        stmt = stmt.where(QueryExecutionLog.status == status_filter)
        count_stmt = count_stmt.where(QueryExecutionLog.status == status_filter)
    if own_only:
        stmt = stmt.where(QueryExecutionLog.user_id == user_id)
        count_stmt = count_stmt.where(QueryExecutionLog.user_id == user_id)

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(desc(QueryExecutionLog.created_at)).offset(max(0, page - 1) * bounded_size).limit(bounded_size)
    rows = list((await db.execute(stmt)).scalars().all())
    return rows, total


async def connection_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    result = await db.execute(select(Connection.id, Connection.name).where(Connection.id.in_(ids)))
    return {row.id: row.name for row in result.all()}
