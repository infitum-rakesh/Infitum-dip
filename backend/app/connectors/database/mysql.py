"""
MySqlConnector — verified against a live mysql:8.0 instance during Phase 1
development.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import URL

from app.connectors.database.base import SqlAlchemyDatabaseConnector


class MySqlConnector(SqlAlchemyDatabaseConnector):
    connection_type = "MYSQL"
    default_port = 3306
    version_query = "SELECT VERSION()"
    explain_prefix = "EXPLAIN"  # real MySQL syntax — returns a genuine multi-column result (id/select_type/table/type/...)
    explain_mode = "table"
    # Query timeout + cancellation (History/Export/Security phase).
    # supports_query_timeout is True, but disclose the real MySQL
    # limitation honestly: MAX_EXECUTION_TIME only ever aborts read
    # (SELECT) statements — MySQL has no server-side equivalent for
    # INSERT/UPDATE/DELETE/DDL, so a timeout set here is a genuine no-op
    # for those (never silently pretended to cut them off). Cancellation
    # via KILL QUERY works for any in-flight statement, reads or writes.
    supports_query_timeout = True
    supports_query_cancel = True

    def get_session_id(self, conn) -> str | None:
        return str(conn.execute(text("SELECT CONNECTION_ID()")).scalar())

    def apply_statement_timeout(self, conn, seconds: int) -> None:
        conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME={max(1, int(seconds)) * 1000}"))

    def cancel_session(self, session_id: str) -> None:
        engine = self.engine(pool_size=1)
        with engine.connect() as conn:
            conn.execute(text(f"KILL QUERY {int(session_id)}"))
            conn.commit()

    def build_url(self) -> URL:
        cfg = self.config
        return URL.create(
            "mysql+pymysql",
            username=cfg.get("username"),
            password=self.secrets.get("password"),
            host=cfg.get("host"),
            port=int(cfg.get("port") or self.default_port),
            database=cfg.get("database"),
        )

    def connect_args(self) -> dict:
        args = {"connect_timeout": 10}
        if self.config.get("ssl_mode") == "Require":
            args["ssl"] = {}  # PyMySQL: non-empty dict enables SSL with default context
        return args

    def excluded_schemas(self) -> set[str]:
        return {"information_schema", "performance_schema", "mysql", "sys"}
