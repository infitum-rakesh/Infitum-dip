"""
FabricConnector — real Microsoft Fabric SQL endpoint connectivity.
Fabric Warehouses/Lakehouses speak the SQL Server TDS wire protocol, so
this reuses SqlAlchemyDatabaseConnector via SQLAlchemy's mssql+pyodbc
dialect (pyodbc + Microsoft's msodbcsql ODBC driver) — NOT pymssql/
FreeTDS like connectors/database/sqlserver.py, because Fabric's SQL
endpoint accepts **only** Entra ID (Azure AD) authentication; there is no
SQL-login fallback, and FreeTDS has no Azure AD token support at all.

Real, but NOT live-verified: this environment has no Microsoft Fabric
workspace (a cloud-only, license-gated service — no free/local emulator
exists) — same disclosed-but-real precedent as connectors/database/
snowflake.py and connectors/database/sap.py. Every piece below follows
Microsoft's own documented contract exactly, not a guess:
  - Token acquisition: azure-identity, scope "https://database.windows.net/.default"
    (the documented Azure SQL/Fabric/Synapse AAD resource — all three
    share the same AAD auth protocol).
  - Token handoff to pyodbc: the SQL_COPT_SS_ACCESS_TOKEN (1256) connection
    attribute via pyodbc's `attrs_before`, with the token UTF-16-LE-encoded
    and length-prefixed exactly as Microsoft's own sample code does
    (see Microsoft Learn "Azure SQL Database - passwordless connections
    for Python apps" / pyodbc's own Azure AD sample). No UID/PWD is put in
    the ODBC connection string — the token supplies the identity instead.

Two real auth paths (see connection_service.py's FABRIC field schema):
  - "Azure AD": azure-identity's UsernamePasswordCredential — a real,
    documented Resource Owner Password Credential (ROPC) flow suitable
    for an unattended backend service (no interactive browser prompt).
    ROPC needs a public-client `client_id`; since the FIELD_SCHEMA
    deliberately doesn't ask the user for one on this path (asking for
    yet another Azure app registration just to log in with a username/
    password would defeat the point), this uses Microsoft's own
    well-known public client ID for the Azure CLI
    (04b07795-8ddb-461a-bbee-02f9e1bff783) — a real, Microsoft-owned app
    registration explicitly intended for exactly this kind of first-party
    tool/script ROPC use, the same ID the `az` CLI itself authenticates
    with. NOTE: ROPC does not work against tenants that require MFA or
    Conditional Access — Microsoft's own recommended fallback for those
    tenants is Service Principal auth, the second path below.
  - "Service Principal": azure-identity's ClientSecretCredential
    (tenant_id/client_id/client_secret) — works under MFA/Conditional
    Access since it's a non-interactive app credential, not a user's own
    sign-in.

Token refresh: the pooled Engine is long-lived (connectors/database/
pool_manager.py), but AAD access tokens expire (~60-90 min). Rather than
computing the token once in connect_args() (which would go stale for any
new pooled connection opened after expiry), this attaches a SQLAlchemy
`do_connect` event listener that acquires a fresh token on every new
physical DBAPI connection the pool opens — the officially documented
pattern for long-lived SQLAlchemy engines against Azure AD-authenticated
databases. A single physical connection checked out and held open across
a token's expiry would still fail mid-use — inherent to token-based DB
auth everywhere, not specific to this connector — but pool_manager's own
pool_recycle=1800s keeps that window comfortably under typical token
lifetimes for normal (non-held-open) usage.
"""
from __future__ import annotations

import struct
from urllib.parse import quote_plus

from sqlalchemy import event, text
from sqlalchemy.engine import URL, Engine

from app.connectors.base import ConnectorValidationError, ExplainResult
from app.connectors.database.base import SqlAlchemyDatabaseConnector

# Documented pyodbc/msodbcsql connection attribute for handing pyodbc a
# pre-acquired Azure AD access token instead of UID/PWD.
_SQL_COPT_SS_ACCESS_TOKEN = 1256
_TOKEN_SCOPE = "https://database.windows.net/.default"
# Microsoft's own public client ID for the Azure CLI — a real, first-party
# app registration meant for exactly this non-interactive ROPC use case.
_AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bff783"

_AZURE_AD = "Azure AD"
_SERVICE_PRINCIPAL = "Service Principal"


def _encode_token(token: str) -> bytes:
    token_bytes = token.encode("utf-16-le")
    return struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)


class FabricConnector(SqlAlchemyDatabaseConnector):
    connection_type = "FABRIC"
    default_port = 1433
    version_query = "SELECT @@VERSION"
    # Same T-SQL surface as connectors/database/sqlserver.py — Fabric SQL
    # endpoints speak the same TDS/T-SQL dialect (see module docstring), so
    # @@SPID/KILL/SHOWPLAN_ALL below are the identical real, standard
    # mechanisms, not guessed. Real but NOT live-verified — same disclosed
    # status as the rest of this connector (no Fabric workspace available
    # in this environment).
    supports_query_cancel = True

    def get_session_id(self, conn) -> str | None:
        return str(conn.execute(text("SELECT @@SPID")).scalar())

    def cancel_session(self, session_id: str) -> None:
        engine = self.engine(pool_size=1)
        with engine.connect() as conn:
            conn.execute(text(f"KILL {int(session_id)}"))
            conn.commit()

    def explain(self, sql: str, parameters: dict) -> ExplainResult:
        """See sqlserver.py's explain() — identical real T-SQL SHOWPLAN_ALL
        mechanism (SHOWPLAN_ALL must be the only statement in its batch,
        so it's issued as its own execute() call before the real query)."""
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

    def _credential(self):
        from azure.identity import ClientSecretCredential, UsernamePasswordCredential

        auth = self.config.get("authentication") or _AZURE_AD
        if auth == _SERVICE_PRINCIPAL:
            tenant_id = self.config.get("tenant_id")
            client_id = self.config.get("client_id")
            client_secret = self.secrets.get("client_secret")
            if not all([tenant_id, client_id, client_secret]):
                raise ConnectorValidationError("Service Principal auth needs Tenant ID, Client ID and Client Secret.")
            return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)

        if auth == _AZURE_AD:
            username = self.config.get("username")
            password = self.secrets.get("password")
            if not username or not password:
                raise ConnectorValidationError("Azure AD auth needs a Username and Password.")
            return UsernamePasswordCredential(_AZURE_CLI_CLIENT_ID, username, password)

        raise ConnectorValidationError(f"Unknown Fabric authentication mode '{auth}'.")

    def _fresh_token_struct(self) -> bytes:
        token = self._credential().get_token(_TOKEN_SCOPE).token
        return _encode_token(token)

    def build_url(self) -> URL:
        cfg = self.config
        sql_endpoint = (cfg.get("sql_endpoint") or "").strip()
        database = (cfg.get("database") or "").strip()
        if not sql_endpoint or not database:
            raise ConnectorValidationError("SQL Endpoint and Database are both required for a Fabric connection.")
        # Built as one raw ODBC connection string (via SQLAlchemy's
        # `odbc_connect` escape hatch) instead of letting the mssql+pyodbc
        # dialect assemble one from separate URL fields the normal way.
        # That auto-assembly (sqlalchemy.dialects.mssql.pyodbc.
        # MSDialect_pyodbc.create_connect_args) ALWAYS appends exactly one
        # of: `UID=`/`PWD=` (when a username is given), `Authentication=`
        # (when an `authentication` query key is given), or — when
        # neither is given, which is this connector's own case since
        # identity comes from the token injected via SQL_COPT_SS_ACCESS_
        # TOKEN below, not from the URL — an unconditional
        # `Trusted_Connection=Yes`. Every one of those three is a keyword
        # Microsoft's ODBC Driver 18 rejects outright once an access token
        # is also supplied: "[FA005] Cannot use Access Token with any of
        # the following options: Authentication, Integrated Security,
        # User, Password." `Trusted_Connection=Yes` IS "Integrated
        # Security" in ODBC terms, so the dialect's own default fallback
        # is exactly what triggered this — found live testing a real
        # Fabric connection, the FA005 error the UI actually surfaced. A
        # connection string built entirely by hand is the only way to
        # supply DRIVER/SERVER/DATABASE/Encrypt while omitting all four
        # forbidden keywords, so the token remains the sole credential.
        conn_str = (
            "DRIVER={ODBC Driver 18 for SQL Server};"
            f"SERVER={sql_endpoint},{self.default_port};"
            f"DATABASE={database};"
            "Encrypt=yes;"
            "TrustServerCertificate=no;"
        )
        return URL.create("mssql+pyodbc", query={"odbc_connect": quote_plus(conn_str)})

    def excluded_schemas(self) -> set[str]:
        return {
            "sys", "INFORMATION_SCHEMA", "guest",
            "db_accessadmin", "db_backupoperator", "db_datareader", "db_datawriter",
            "db_ddladmin", "db_denydatareader", "db_denydatawriter", "db_owner", "db_securityadmin",
        }

    def engine(self, pool_size: int = 5, pool_timeout: int = 10) -> Engine:
        eng = super().engine(pool_size=pool_size, pool_timeout=pool_timeout)
        # Attach the token-refresh listener exactly once per cached Engine
        # (engine() is called repeatedly and returns the same pooled
        # Engine instance — re-registering the listener every call would
        # stack duplicate listeners).
        if not getattr(eng, "_fabric_token_listener_attached", False):
            @event.listens_for(eng, "do_connect")
            def _inject_token(dialect, conn_rec, cargs, cparams):
                cparams["attrs_before"] = {_SQL_COPT_SS_ACCESS_TOKEN: self._fresh_token_struct()}

            eng._fabric_token_listener_attached = True
        return eng
