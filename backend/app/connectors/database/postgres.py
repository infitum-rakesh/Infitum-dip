"""
PostgresConnector — verified against a live postgres:16-alpine instance
during Phase 1 development (see connectors/database/README.md for how to
re-run that verification).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import URL

from app.connectors.database.base import SqlAlchemyDatabaseConnector

_SSL_MODE = {"Disable": "disable", "Allow": "allow", "Require": "require", "Verify-Full": "verify-full"}


class PostgresConnector(SqlAlchemyDatabaseConnector):
    connection_type = "POSTGRESQL"
    default_port = 5432
    version_query = "SELECT version()"
    explain_prefix = "EXPLAIN"  # real Postgres syntax — returns one "QUERY PLAN" text column, one row per plan line
    explain_mode = "text"
    # Query timeout + cancellation (History/Export/Security phase) — both
    # real, standard Postgres mechanisms: statement_timeout aborts the
    # statement server-side (the DB itself raises, not a client-side
    # giveup that could leave a write still running), pg_cancel_backend
    # genuinely interrupts an in-flight backend process from a separate
    # connection.
    supports_query_timeout = True
    supports_query_cancel = True

    def get_session_id(self, conn) -> str | None:
        return str(conn.execute(text("SELECT pg_backend_pid()")).scalar())

    def apply_statement_timeout(self, conn, seconds: int) -> None:
        # SET doesn't reliably accept a bind parameter here across
        # drivers — safe as a literal since `seconds` is our own
        # already-clamped int, never user-supplied text.
        conn.execute(text(f"SET statement_timeout = {max(1, int(seconds)) * 1000}"))

    def cancel_session(self, session_id: str) -> None:
        engine = self.engine(pool_size=1)
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_cancel_backend(:pid)"), {"pid": int(session_id)})
            conn.commit()

    def build_url(self) -> URL:
        cfg = self.config
        return URL.create(
            "postgresql+psycopg2",
            username=cfg.get("username"),
            password=self.secrets.get("password"),
            host=cfg.get("host"),
            port=int(cfg.get("port") or self.default_port),
            database=cfg.get("database"),
            query={"sslmode": _SSL_MODE.get(cfg.get("ssl_mode"), "require")},
        )

    def connect_args(self) -> dict:
        return {"connect_timeout": 10}

    def excluded_schemas(self) -> set[str]:
        return {"information_schema", "pg_catalog", "pg_toast"}
