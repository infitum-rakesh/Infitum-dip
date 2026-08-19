"""
NetSuiteSuiteQlConnector — real ODBC connectivity to NetSuite via Oracle's
own SuiteAnalytics Connect ODBC driver (SuiteQL), using pyodbc directly.

Why pyodbc directly instead of SqlAlchemyDatabaseConnector (see database/
base.py): every other DATABASE-category connector in this codebase talks
through a SQLAlchemy dialect, but NetSuite has no maintained public
SQLAlchemy dialect. SuiteAnalytics Connect's own ODBC driver DOES expose
the standard ODBC catalog functions (SQLTables/SQLColumns), which pyodbc
surfaces as cursor.tables()/cursor.columns() — enough to implement real
schema/object/column browsing and bounded preview without inventing a
custom SQL-catalog dialect of our own.

Connection shape (see connection_service.py's NETSUITE_SUITEQL field
schema): `dsn` (a Data Source Name that must already be registered on
this server's odbc.ini/System DSN — pointed at NetSuite's driver, per
Oracle's own setup docs), `username`/`password`, `account`, optional
`role_id`. The driver's own connection string accepts NetSuite-specific
CustomProperties (AccountID, RoleID, ...) alongside the standard
DSN/UID/PWD triad, which is exactly what "using env DSN, id pwd" (the
build spec) describes.

Genuinely real, but NOT live-verified: this environment has no NetSuite
account, no SuiteAnalytics Connect ODBC driver installed, and no DSN
configured — pyodbc.connect() will make a real connection attempt and
fail honestly with a real driver-not-found/DSN-not-found error until a
user configures an actual DSN pointed at their own NetSuite account,
exactly the way a real deployment's first Test Connection would behave
before that setup step. See connectors/registry.py's docstring for the
same disclosed-but-real precedent (Snowflake, Oracle before Docker).

SuiteQL's dialect: Oracle-derived, read-mostly (no DDL in practice), and
has no portable LIMIT clause pyodbc can rely on the same way across
driver versions — like SqlAlchemyDatabaseConnector's own preview_object(),
this bounds every read via cursor.fetchmany(n), never a dialect-specific
LIMIT/TOP/FETCH clause baked into the SQL text.
"""
from __future__ import annotations

import time

from app.connectors.base import (
    BaseConnector,
    ChunkResult,
    ColumnInfo,
    ConnectionTestResult,
    ConnectorError,
    ConnectorValidationError,
    ObjectInfo,
    PreviewResult,
    SqlRunResult,
    SqlStream,
)

try:
    import pyodbc
except ImportError:  # pragma: no cover - pyodbc is in requirements.txt; guarded for import-time safety only
    pyodbc = None

_ALLOWED_PREVIEW_PREFIXES = ("select", "with")


class NetSuiteSuiteQlConnector(BaseConnector):
    connection_type = "NETSUITE_SUITEQL"

    def _connect(self):
        if pyodbc is None:
            raise ConnectorError("The pyodbc driver is not installed on this server.")
        cfg = self.config
        dsn = (cfg.get("dsn") or "").strip()
        if not dsn:
            raise ConnectorValidationError("No DSN configured for this NetSuite (SuiteQL) connection.")
        parts = [f"DSN={dsn}", f"UID={cfg.get('username') or ''}", f"PWD={self.secrets.get('password') or ''}"]
        # NetSuite's ODBC driver reads account/role via its own
        # CustomProperties connection-string extension rather than
        # separate DSN/UID/PWD-style keywords — see Oracle's
        # SuiteAnalytics Connect ODBC driver install guide.
        custom_props = []
        if cfg.get("account"):
            custom_props.append(f"AccountID={cfg['account']}")
        if cfg.get("role_id"):
            custom_props.append(f"RoleID={cfg['role_id']}")
        if custom_props:
            parts.append(f"CustomProperties={';'.join(custom_props)}")
        conn_str = ";".join(parts) + ";"
        try:
            return pyodbc.connect(conn_str, timeout=15, autocommit=True)
        except pyodbc.Error as exc:
            raise ConnectorError(self._scrub(f"NetSuite SuiteQL connection failed: {exc}")) from exc

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            conn = self._connect()
        except (ConnectorError, ConnectorValidationError) as exc:
            return ConnectionTestResult(False, str(exc), round((time.monotonic() - start) * 1000, 1))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM DUAL")
            cursor.fetchone()
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            version = None
            try:
                version = str(conn.getinfo(pyodbc.SQL_DBMS_VER))
            except Exception:
                pass
            return ConnectionTestResult(True, "Connection successful.", latency_ms, server_version=version)
        except pyodbc.Error as exc:
            return ConnectionTestResult(False, self._scrub(f"SuiteQL query failed: {exc}"), round((time.monotonic() - start) * 1000, 1))
        finally:
            conn.close()

    # ── Schema/object browsing — via pyodbc's own ODBC catalog functions,
    # not a hand-rolled query, so it works against whatever the installed
    # driver version actually exposes. ─────────────────────────────────

    def list_schemas(self) -> list[str]:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            schemas = sorted({row.table_schem for row in cursor.tables() if row.table_schem})
            return schemas
        except pyodbc.Error as exc:
            raise ConnectorError(self._scrub(f"Could not list schemas: {exc}")) from exc
        finally:
            conn.close()

    def list_objects(self, schema: str | None, search: str | None = None) -> list[ObjectInfo]:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            objects = []
            for row in cursor.tables(schema=schema, tableType="TABLE,VIEW"):
                obj_type = "VIEW" if (row.table_type or "").upper() == "VIEW" else "TABLE"
                objects.append(ObjectInfo(name=row.table_name, schema=row.table_schem, object_type=obj_type))
            if search:
                needle = search.lower()
                objects = [o for o in objects if needle in o.name.lower()]
            objects.sort(key=lambda o: (o.object_type, o.name.lower()))
            return objects
        except pyodbc.Error as exc:
            raise ConnectorError(self._scrub(f"Could not list tables/views: {exc}")) from exc
        finally:
            conn.close()

    def get_columns(self, object_name: str, schema: str | None = None) -> list[ColumnInfo]:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            pk_cols: set[str] = set()
            try:
                pk_cols = {row.column_name for row in cursor.primaryKeys(table=object_name, schema=schema)}
            except pyodbc.Error:
                pass  # not every NetSuite "table" (saved-search-backed views especially) exposes a PK
            columns = []
            for row in cursor.columns(table=object_name, schema=schema):
                columns.append(ColumnInfo(
                    name=row.column_name,
                    data_type=str(row.type_name or "unknown"),
                    nullable=bool(row.nullable),
                    primary_key=row.column_name in pk_cols,
                ))
            return columns
        except pyodbc.Error as exc:
            raise ConnectorError(self._scrub(f"Could not read columns for {object_name}: {exc}")) from exc
        finally:
            conn.close()

    def row_count(self, object_name: str, schema: str | None = None) -> int | None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            quoted = self._quote(object_name, schema)
            cursor.execute(f"SELECT COUNT(*) FROM {quoted}")
            row = cursor.fetchone()
            return int(row[0]) if row is not None else None
        except pyodbc.Error:
            return None  # a slow/unsupported COUNT(*) degrades the preview UI, doesn't break it
        finally:
            conn.close()

    def preview_object(self, object_name: str, schema: str | None, limit: int) -> PreviewResult:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            quoted = self._quote(object_name, schema)
            cursor.execute(f"SELECT * FROM {quoted}")
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(limit + 1)
            return PreviewResult(columns=columns, rows=[list(r) for r in rows[:limit]], row_count=None, truncated=len(rows) > limit)
        except pyodbc.Error as exc:
            raise ConnectorError(self._scrub(f"Preview of {object_name} failed: {exc}")) from exc
        finally:
            conn.close()

    def preview_sql(self, sql: str, parameters: dict, limit: int) -> PreviewResult:
        stripped = (sql or "").strip()
        if not stripped:
            raise ConnectorValidationError("SQL text is empty.")
        if stripped.split(None, 1)[0].lower() not in _ALLOWED_PREVIEW_PREFIXES:
            raise ConnectorValidationError("Only SELECT / WITH statements are allowed for preview.")
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(stripped, list((parameters or {}).values()))
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(limit + 1)
            return PreviewResult(columns=columns, rows=[list(r) for r in rows[:limit]], row_count=None, truncated=len(rows) > limit)
        except pyodbc.Error as exc:
            raise ConnectorError(self._scrub(f"SuiteQL query failed: {exc}")) from exc
        finally:
            conn.close()

    def run_sql(self, sql: str, parameters: dict, limit: int) -> SqlRunResult:
        # SuiteQL is a read-only query surface (NetSuite's own ODBC driver
        # does not support INSERT/UPDATE/DELETE/DDL over SuiteAnalytics
        # Connect — that's what SuiteScript/SOAP/REST record operations
        # are for) — SQL Workspace against this connection is genuinely
        # limited to SELECT/WITH, disclosed via this explicit guard rather
        # than a silent no-op or a fabricated affected-row count.
        stripped = (sql or "").strip().rstrip(";").strip()
        if not stripped:
            raise ConnectorValidationError("SQL text is empty.")
        if stripped.split(None, 1)[0].lower() not in _ALLOWED_PREVIEW_PREFIXES:
            raise ConnectorValidationError(
                "NetSuite SuiteQL (via SuiteAnalytics Connect) is read-only — only SELECT / WITH statements are supported."
            )
        preview = self.preview_sql(stripped, parameters, limit)
        return SqlRunResult(is_result_set=True, columns=preview.columns, rows=preview.rows, truncated=preview.truncated)

    # ── Streaming extraction ───────────────────────────────────────────

    def key_range(self, object_name: str, schema: str | None, key_column: str) -> tuple[object, object]:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            quoted_obj = self._quote(object_name, schema)
            quoted_col = self._quote_ident(key_column)
            cursor.execute(f"SELECT MIN({quoted_col}), MAX({quoted_col}) FROM {quoted_obj}")
            row = cursor.fetchone()
            return (row[0], row[1]) if row is not None else (None, None)
        except pyodbc.Error as exc:
            raise ConnectorError(self._scrub(f"Could not compute key range for {object_name}: {exc}")) from exc
        finally:
            conn.close()

    def read_key_range_chunk(
        self, object_name: str, schema: str | None, key_column: str,
        after_value: object, upper_bound: object, chunk_size: int,
    ) -> ChunkResult:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            quoted_obj = self._quote(object_name, schema)
            quoted_col = self._quote_ident(key_column)
            clauses, params = [], []
            if after_value is not None:
                clauses.append(f"{quoted_col} > ?")
                params.append(after_value)
            if upper_bound is not None:
                clauses.append(f"{quoted_col} <= ?")
                params.append(upper_bound)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            cursor.execute(f"SELECT * FROM {quoted_obj} {where} ORDER BY {quoted_col}", params)
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                return ChunkResult(rows=[], last_key=None, count=0)
            key_idx = columns.index(key_column)
            dict_rows = [dict(zip(columns, r)) for r in rows]
            return ChunkResult(rows=dict_rows, last_key=rows[-1][key_idx], count=len(rows))
        except pyodbc.Error as exc:
            raise ConnectorError(self._scrub(f"Chunked read of {object_name} failed: {exc}")) from exc
        finally:
            conn.close()

    def open_sql_stream(self, sql: str, parameters: dict) -> SqlStream:
        stripped = (sql or "").strip()
        if not stripped:
            raise ConnectorValidationError("SQL text is empty.")
        if stripped.split(None, 1)[0].lower() not in _ALLOWED_PREVIEW_PREFIXES:
            raise ConnectorValidationError("Only SELECT / WITH statements are allowed.")
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(stripped, list((parameters or {}).values()))
        except pyodbc.Error:
            conn.close()
            raise
        return _PyodbcSqlStream(conn, cursor)

    # ── Identifier quoting — NetSuite's driver accepts double-quoted
    # identifiers (standard SQL-92 style), same convention SuiteQL's own
    # documentation examples use. ───────────────────────────────────────

    def _quote_ident(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _quote(self, object_name: str, schema: str | None) -> str:
        if schema:
            return f"{self._quote_ident(schema)}.{self._quote_ident(object_name)}"
        return self._quote_ident(object_name)


class _PyodbcSqlStream(SqlStream):
    def __init__(self, conn, cursor):
        self._conn = conn
        self._cursor = cursor
        self._columns = [d[0] for d in cursor.description]

    def fetch_chunk(self, size: int) -> list[dict]:
        rows = self._cursor.fetchmany(size)
        return [dict(zip(self._columns, r)) for r in rows]

    def close(self) -> None:
        self._conn.close()
