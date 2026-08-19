"""
SqlServerConnector — verified against a live mcr.microsoft.com/azure-sql-edge
instance (SQL Server wire-protocol compatible, runs on Apple Silicon) during
Phase 1 development, using pymssql (bundles FreeTDS — no system ODBC driver
manager/msodbcsql install required, which matters since this platform
doesn't assume ODBC is present on the host).

pymssql/FreeTDS only speaks SQL Server Authentication (username/password).
Windows Authentication and Azure AD both need a different driver
(pyodbc + msodbcsql + a Kerberos/ADAL setup) that isn't wired up here —
selecting either returns a clear unsupported result rather than a
confusing raw connection failure.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import URL

from app.connectors.base import ConnectionTestResult, ConnectorValidationError, ExplainResult
from app.connectors.database.base import SqlAlchemyDatabaseConnector

_SQL_AUTH = "SQL Server Authentication"


class SqlServerConnector(SqlAlchemyDatabaseConnector):
    connection_type = "SQL_SERVER"
    default_port = 1433
    version_query = "SELECT @@VERSION"
    # Query cancellation — real, standard T-SQL: @@SPID is the session's own
    # process id, KILL <spid> (from a separate connection/session — SQL
    # Server doesn't let a session KILL itself) genuinely aborts an
    # in-flight statement server-side. NOT live-verified against a real
    # SQL Server/Azure SQL instance for this specific mechanism (this
    # connector's base CRUD path was verified against azure-sql-edge during
    # Phase 1 — see module docstring above — but KILL wasn't exercised
    # then); real, standard, documented T-SQL regardless.
    supports_query_cancel = True
    # No supports_query_timeout: T-SQL has no session-settable "abort this
    # statement after N seconds" equivalent to Postgres' statement_timeout
    # (SET LOCK_TIMEOUT only bounds lock-wait time, not total execution) —
    # disclosed as genuinely unsupported rather than wired to something
    # that wouldn't actually enforce a full-statement timeout.

    def get_session_id(self, conn) -> str | None:
        return str(conn.execute(text("SELECT @@SPID")).scalar())

    def cancel_session(self, session_id: str) -> None:
        engine = self.engine(pool_size=1)
        with engine.connect() as conn:
            # KILL isn't parameterizable (no bind-parameter support for a
            # session id in this statement position) — session_id came from
            # our own get_session_id() above, a real @@SPID integer, never
            # user-typed text, so a literal int() cast is safe here.
            conn.execute(text(f"KILL {int(session_id)}"))
            conn.commit()

    def explain(self, sql: str, parameters: dict) -> ExplainResult:
        """Real T-SQL execution plan — SHOWPLAN_ALL must be the ONLY
        statement in its batch (a genuine SQL Server restriction), so this
        issues it as its own execute() call, then the real query text as a
        separate execute() on the SAME connection — with SHOWPLAN_ALL ON,
        SQL Server returns the plan instead of actually running the
        statement (safe for INSERT/UPDATE/DELETE, same guarantee EXPLAIN
        gives on every other dialect here). Always turns SHOWPLAN_ALL back
        off afterward, even on failure, so the connection (which the pool
        may reuse) never leaks session state into a later real query."""
        stripped = (sql or "").strip().rstrip(";").strip()
        if not stripped:
            raise ConnectorValidationError("SQL text is empty.")
        engine = self.engine(pool_size=2)
        with engine.connect() as conn:
            conn.execute(text("SET SHOWPLAN_ALL ON"))
            try:
                result = conn.execute(text(stripped), parameters or {})
                columns = list(result.keys())
                rows = [list(r) for r in result.fetchall()]
            finally:
                conn.execute(text("SET SHOWPLAN_ALL OFF"))
        return ExplainResult(columns=columns, rows=rows)

    def build_url(self) -> URL:
        cfg = self.config
        return URL.create(
            "mssql+pymssql",
            username=cfg.get("username"),
            password=self.secrets.get("password"),
            host=cfg.get("host"),
            port=int(cfg.get("port") or self.default_port),
            database=cfg.get("database"),
        )

    def connect_args(self) -> dict:
        return {"timeout": 10, "login_timeout": 10}

    def excluded_schemas(self) -> set[str]:
        return {
            "sys", "INFORMATION_SCHEMA", "guest",
            "db_accessadmin", "db_backupoperator", "db_datareader", "db_datawriter",
            "db_ddladmin", "db_denydatareader", "db_denydatawriter", "db_owner", "db_securityadmin",
        }

    def test_connection(self) -> ConnectionTestResult:
        auth = self.config.get("authentication", _SQL_AUTH)
        if auth != _SQL_AUTH:
            return ConnectionTestResult(
                success=False,
                message=f"'{auth}' is not supported by this connector — only {_SQL_AUTH} is implemented "
                        f"(pymssql/FreeTDS has no Windows/Azure AD auth support). Use a SQL login instead.",
            )
        return super().test_connection()
