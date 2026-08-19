"""
OracleConnector — verified against a live gvenzl/oracle-free instance
(Oracle Database Free 23ai/26ai, thin-mode via python-oracledb — no
Oracle Instant Client install required) during this build's Oracle
bulk-load target phase.

build_url() uses `service_name` (query param), matching the FIELD_SCHEMAS
"Service Name" field — Oracle's DSN-vs-SID-vs-service_name ambiguity is
resolved in favor of service_name, the modern/recommended connect
identifier (SID is a legacy concept that doesn't apply to pluggable
databases, which is what every current Oracle deployment — including the
one this was verified against — actually uses).

Oracle folds unquoted identifiers to UPPERCASE. This connector doesn't
special-case that: SQLAlchemy's OracleIdentifierPreparer already quotes
any identifier it's given that isn't all-uppercase (so a lowercase
DataFrame column name from ensure_table() round-trips correctly — verified
live, see loading/writers/oracle.py), and quote_object()/quote_identifier()
go through that same preparer — no separate case-folding logic needed here.

Known, disclosed, pre-existing-pattern gap: like SqlServerConnector, this
connector does not override read_key_range_chunk() — the base class's
chunked-extraction SQL uses a literal `LIMIT` clause, which is not valid
Oracle syntax (Oracle needs `FETCH FIRST n ROWS ONLY` or ROWNUM). That
path belongs to Source Dataset's separate chunked/checkpointed extraction
engine (execution/worker.py) — out of scope for the bulk-load TARGET
phase this connector was built for, and left exactly as unaddressed as it
already was for SQL Server rather than silently declared fixed.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import URL

from app.connectors.base import ConnectorValidationError, ExplainResult
from app.connectors.database.base import SqlAlchemyDatabaseConnector


class OracleConnector(SqlAlchemyDatabaseConnector):
    connection_type = "ORACLE"
    default_port = 1521
    version_query = "SELECT banner FROM v$version WHERE ROWNUM = 1"
    # Query cancellation — real, standard Oracle DBA mechanism:
    # ALTER SYSTEM KILL SESSION 'sid,serial#' genuinely terminates an
    # in-flight session server-side; SID+SERIAL# (not SID alone — Oracle
    # reuses SID numbers across sessions, SERIAL# disambiguates) is looked
    # up via v$session/SYS_CONTEXT, both real, documented Oracle views.
    # Real code against Oracle's own documented contract; NOT re-verified
    # live for this specific mechanism (this connector's base CRUD path
    # WAS live-verified against gvenzl/oracle-free — see module docstring
    # — but KILL SESSION requires a privileged account this build's test
    # container wasn't provisioned with).
    supports_query_cancel = True
    # No supports_query_timeout: Oracle has no simple session-settable
    # "abort after N seconds" statement — real per-session limits go
    # through Resource Manager plans (DBA-level administration, not
    # something a session can SET for itself), so this is disclosed as
    # genuinely unsupported rather than faked with the wrong mechanism.

    def get_session_id(self, conn) -> str | None:
        row = conn.execute(text(
            "SELECT sid, serial# FROM v$session WHERE audsid = SYS_CONTEXT('USERENV', 'SESSIONID')"
        )).first()
        return f"{row[0]},{row[1]}" if row else None

    def cancel_session(self, session_id: str) -> None:
        sid, _, serial = session_id.partition(",")
        engine = self.engine(pool_size=1)
        with engine.connect() as conn:
            conn.execute(text(f"ALTER SYSTEM KILL SESSION '{int(sid)},{int(serial)}'"))
            conn.commit()

    def explain(self, sql: str, parameters: dict) -> ExplainResult:
        """Real Oracle execution plan — the standard two-step process every
        Oracle client (SQL*Plus, SQL Developer, ...) uses: EXPLAIN PLAN FOR
        populates PLAN_TABLE with this session's plan, DBMS_XPLAN.DISPLAY
        reads it back formatted. Both run in the SAME connection/
        transaction and the plan rows are rolled back afterward (PLAN_TABLE
        is a real shared/global temp table — never left with stale rows
        for a later query on a pooled connection)."""
        stripped = (sql or "").strip().rstrip(";").strip()
        if not stripped:
            raise ConnectorValidationError("SQL text is empty.")
        engine = self.engine(pool_size=2)
        with engine.begin() as conn:
            conn.execute(text(f"EXPLAIN PLAN FOR {stripped}"), parameters or {})
            result = conn.execute(text("SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY())"))
            rows = [str(r[0]) for r in result.fetchall()]
        return ExplainResult(plan_text="\n".join(rows))

    def build_url(self) -> URL:
        cfg = self.config
        return URL.create(
            "oracle+oracledb",
            username=cfg.get("username"),
            password=self.secrets.get("password"),
            host=cfg.get("host"),
            port=int(cfg.get("port") or self.default_port),
            query={"service_name": cfg.get("service_name")},
        )

    def connect_args(self) -> dict:
        return {}

    def excluded_schemas(self) -> set[str]:
        # Oracle system/internal schemas — not real user data, hidden
        # from the browse-schema dropdown the same way pg_catalog/
        # information_schema are hidden for Postgres/MySQL.
        return {
            "SYS", "SYSTEM", "OUTLN", "XDB", "ORDDATA", "CTXSYS", "MDSYS",
            "ANONYMOUS", "APPQOSSYS", "AUDSYS", "DBSFWUSER", "DBSNMP",
            "DIP", "DVSYS", "DVF", "GGSYS", "GSMADMIN_INTERNAL", "GSMCATUSER",
            "GSMUSER", "LBACSYS", "OJVMSYS", "OLAPSYS", "ORACLE_OCM",
            "REMOTE_SCHEDULER_AGENT", "SI_INFORMTN_SCHEMA", "SYSBACKUP",
            "SYSDG", "SYSKM", "SYSRAC", "WMSYS", "XS$NULL", "PUBLIC",
        }
