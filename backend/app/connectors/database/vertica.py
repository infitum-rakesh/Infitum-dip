"""
VerticaConnector — real Vertica connectivity via sqlalchemy-vertica-python
(which wraps vertica-python, the vendor-adjacent open-source Vertica DB
client) — a genuine SQLAlchemy dialect, so this reuses
SqlAlchemyDatabaseConnector exactly like Postgres/MySQL/Oracle/Snowflake/
SAP do, getting real schema/object/column browsing, preview, SQL
Workspace execution and chunked key-range extraction for free.

Real, but NOT live-verified: this environment has no Vertica instance and
no free/local Vertica emulator exists (the official Vertica Community
Edition Docker image is a multi-GB, license-accepting download, not
something this sandbox can pull) — same disclosed-but-real precedent as
connectors/database/snowflake.py and sap.py. Every piece below follows
Vertica's own documented SQL reference exactly, not a guess:
  - Default port 5433, the documented Vertica default.
  - excluded_schemas: Vertica's real built-in system schemas (v_catalog,
    v_monitor, v_internal, v_txtindex — all Vertica-owned, never real
    user data).
  - Session identification/cancellation: v_monitor.current_session and
    CLOSE_SESSION()/INTERRUPT_STATEMENT() are real, documented Vertica
    system table/functions.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import URL

from app.connectors.database.base import SqlAlchemyDatabaseConnector


class VerticaConnector(SqlAlchemyDatabaseConnector):
    connection_type = "VERTICA"
    default_port = 5433
    version_query = "SELECT version()"
    # EXPLAIN is real, standard Vertica SQL — returns the plan as PLAN TEXT
    # lines (one column), the same "text" shape Postgres' EXPLAIN uses.
    explain_prefix = "EXPLAIN"
    explain_mode = "text"
    # Query cancellation — real, documented Vertica mechanism:
    # v_monitor.current_session's own session_id, CLOSE_SESSION() from a
    # separate connection genuinely terminates that session (the same
    # session-level granularity KILL/ALTER SYSTEM KILL SESSION use on the
    # other dialects here — Vertica's finer-grained INTERRUPT_STATEMENT()
    # needs a statement_id this codebase's cancel-before-you-know-the-
    # statement-id architecture can't supply, same reasoning as Snowflake's
    # SYSTEM$CANCEL_ALL_QUERIES choice over SYSTEM$CANCEL_QUERY).
    supports_query_cancel = True
    # No supports_query_timeout: Vertica's RUNTIMECAP is a resource-pool
    # configuration property (ALTER RESOURCE POOL ... RUNTIMECAP), not a
    # plain session-level SQL a query-running user can SET for themselves
    # without pool-admin privileges — disclosed as unsupported rather than
    # wired to something that needs administrative setup this connector
    # can't assume exists.

    def get_session_id(self, conn) -> str | None:
        row = conn.execute(text("SELECT session_id FROM v_monitor.current_session")).first()
        return str(row[0]) if row else None

    def cancel_session(self, session_id: str) -> None:
        engine = self.engine(pool_size=1)
        with engine.connect() as conn:
            conn.execute(text("SELECT CLOSE_SESSION(:sid)"), {"sid": session_id})
            conn.commit()

    def build_url(self) -> URL:
        cfg = self.config
        return URL.create(
            "vertica+vertica_python",
            username=cfg.get("username"),
            password=self.secrets.get("password"),
            host=cfg.get("host"),
            port=int(cfg.get("port") or self.default_port),
            database=cfg.get("database"),
        )

    def connect_args(self) -> dict:
        return {"connection_timeout": 10}

    def excluded_schemas(self) -> set[str]:
        return {"v_catalog", "v_monitor", "v_internal", "v_txtindex"}
