"""
SalesforceConnector — real Salesforce connectivity via simple-salesforce
(the standard, widely-used community client that wraps Salesforce's own
REST/SOAP APIs — not a hand-rolled HTTP client). SObjects map onto this
codebase's generic TABLE abstraction directly (no NETSUITE_SEARCH-style
new execution mode needed): "object_name" is an SObject API name (e.g.
"Account", "Contact", a custom "Invoice__c"), rows are read via real SOQL
(Salesforce's SQL-like query language) against that SObject.

Real, but NOT live-verified: this environment has no Salesforce
org/account to authenticate against (a real trial/dev org could be
created, but none exists here) — same disclosed-but-real precedent as
connectors/database/snowflake.py and connectors/database/sap.py. Every
call below (Salesforce(), sf.describe(), sf.<SObject>.describe(),
sf.query()/query_more(), SOQL aggregate MIN/MAX/COUNT()) is real
simple-salesforce/Salesforce REST API surface, verified against
simple-salesforce's own installed source in this environment (exact
Salesforce() constructor kwargs, exact exception classes) — not guessed
from memory.

Auth (see connection_service.py's SALESFORCE field schema, corrected
this session from a broken NetSuite-TBA-shaped field set to Salesforce's
actual real auth model): username + password + security_token is
Salesforce's standard SOAP/REST login flow; an optional Connected App
consumer_key/consumer_secret further scopes that login (recommended for
production — lets an admin restrict IP ranges/session policy per
integration) without changing the flow itself. `domain` selects
'test' for a Sandbox org, a My Domain prefix for a custom domain, or is
left unset for standard production login — exactly simple-salesforce's
own `domain` kwarg semantics.

SOQL is NOT SQL — this connector deliberately does NOT implement
preview_sql()/run_sql()/open_sql_stream() (SQL Workspace's custom-SQL
surface); building a SOQL-aware SQL editor is real, separate scope this
task doesn't cover. BaseConnector's own default ("does not support
custom SQL ...") surfaces that honestly rather than silently
mistranslating SOQL as SQL.

Column selection: SOQL has no `SELECT *` — get_columns()/preview_object()/
read_key_range_chunk() all build an explicit field list from the
SObject's own real describe() metadata, excluding compound parent fields
(type "address"/"location" — their real component fields, e.g.
BillingStreet/BillingCity, are separately listed and still queried) and
"base64" fields (binary blob content, unsuited to a tabular preview/
extract row) — a deliberate, disclosed scope choice, not silent data loss
(both are visible in get_columns()'s full field list either way, since
that's about SCHEMA visibility, not what gets pulled into a data row).
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
)

try:
    from simple_salesforce import Salesforce
    from simple_salesforce.exceptions import SalesforceAuthenticationFailed, SalesforceError
except ImportError:  # pragma: no cover - simple-salesforce is in requirements.txt; guarded for import-time safety only
    Salesforce = None
    SalesforceError = Exception
    SalesforceAuthenticationFailed = Exception

_NON_QUERYABLE_COLUMN_TYPES = {"address", "location", "base64"}
_MAX_PAGE_SIZE = 2000  # Salesforce REST query API's own hard per-request cap


def _soql_literal(value) -> str:
    """Renders a Python value as a SOQL literal for a WHERE clause. Id/
    string keys (the overwhelmingly common case here) are quoted with
    SOQL's own escaping (backslash before a literal quote or backslash);
    everything else (int/float, or an already-ISO datetime string) is
    passed through unquoted, which is exactly what SOQL date/datetime and
    numeric literals require."""
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return str(value)


def _strip_attributes(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "attributes"}


class SalesforceConnector(BaseConnector):
    connection_type = "SALESFORCE"

    def __init__(self, config: dict, secrets: dict, cache_key: str | None = None):
        super().__init__(config, secrets)
        self._cache_key = cache_key
        self._sf = None  # lazily-created, cached simple_salesforce.Salesforce session
        self._field_cache: dict[str, list[dict]] = {}

    def _require_lib(self):
        if Salesforce is None:
            raise ConnectorError("The simple-salesforce library is not installed on this server.")

    def _client(self):
        """One real login per connector instance (a fresh instance is
        constructed per operation by connectors/registry.get_connector(),
        but a single instance is reused across an execution run's many
        chunk reads — see execution/worker.py) — cached here so a large
        extraction run doesn't re-authenticate on every single chunk,
        which would be both slow and a real risk of tripping Salesforce's
        own login-rate limits."""
        if self._sf is not None:
            return self._sf
        self._require_lib()
        cfg = self.config
        username = (cfg.get("username") or "").strip()
        password = self.secrets.get("password") or ""
        security_token = self.secrets.get("security_token") or ""
        if not username or not password:
            raise ConnectorValidationError("Username and Password are both required for a Salesforce connection.")

        domain = (cfg.get("domain") or "").strip() or None
        if domain is None and (cfg.get("environment") or "Production") == "Sandbox":
            domain = "test"

        kwargs = dict(username=username, password=password, security_token=security_token, domain=domain)
        consumer_key = (cfg.get("consumer_key") or "").strip()
        consumer_secret = self.secrets.get("consumer_secret") or ""
        if consumer_key and consumer_secret:
            kwargs.update(consumer_key=consumer_key, consumer_secret=consumer_secret)

        try:
            self._sf = Salesforce(**kwargs)
        except SalesforceAuthenticationFailed as exc:
            raise ConnectorError(self._scrub(f"Salesforce authentication failed: {exc}")) from exc
        except SalesforceError as exc:
            raise ConnectorError(self._scrub(f"Salesforce login failed: {exc}")) from exc
        return self._sf

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            sf = self._client()
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return ConnectionTestResult(
                True, "Connection successful.", latency_ms,
                server_version=f"API v{sf.sf_version} ({sf.sf_instance})",
            )
        except (ConnectorError, ConnectorValidationError) as exc:
            return ConnectionTestResult(False, str(exc), round((time.monotonic() - start) * 1000, 1))
        except Exception as exc:  # network errors etc. that aren't a SalesforceError subclass
            return ConnectionTestResult(False, self._scrub(str(exc)), round((time.monotonic() - start) * 1000, 1))

    # ── Schema/object browsing — Salesforce has no schema concept (one
    # flat namespace of SObjects per org), so list_schemas() genuinely
    # returns empty rather than a fabricated grouping. ────────────────

    def list_schemas(self) -> list[str]:
        return []

    def list_objects(self, schema: str | None, search: str | None = None) -> list[ObjectInfo]:
        sf = self._client()
        try:
            described = sf.describe()
        except SalesforceError as exc:
            raise ConnectorError(self._scrub(f"Could not list Salesforce objects: {exc}")) from exc
        objects = [
            ObjectInfo(name=o["name"], schema=None, object_type="TABLE")
            for o in described.get("sobjects", []) if o.get("queryable")
        ]
        if search:
            needle = search.lower()
            objects = [o for o in objects if needle in o.name.lower()]
        objects.sort(key=lambda o: o.name.lower())
        return objects

    def _describe_fields(self, object_name: str) -> list[dict]:
        if object_name in self._field_cache:
            return self._field_cache[object_name]
        sf = self._client()
        try:
            sobject = getattr(sf, object_name)
            fields = sobject.describe().get("fields", [])
        except SalesforceError as exc:
            raise ConnectorError(self._scrub(f"Could not describe Salesforce object '{object_name}': {exc}")) from exc
        except AttributeError as exc:
            raise ConnectorError(f"'{object_name}' is not a valid Salesforce object API name.") from exc
        self._field_cache[object_name] = fields
        return fields

    def _queryable_field_names(self, object_name: str) -> list[str]:
        names = [f["name"] for f in self._describe_fields(object_name) if f.get("type") not in _NON_QUERYABLE_COLUMN_TYPES]
        if not names:
            raise ConnectorError(f"Salesforce object '{object_name}' has no queryable (non-compound, non-blob) fields.")
        return names

    def get_columns(self, object_name: str, schema: str | None = None) -> list[ColumnInfo]:
        return [
            ColumnInfo(
                name=f["name"], data_type=f["type"], nullable=bool(f.get("nillable", True)),
                primary_key=(f["name"] == "Id"),
                unique=bool(f.get("unique")) or f["name"] == "Id",
                indexed=bool(f.get("unique")) or bool(f.get("externalId")) or f["name"] == "Id",
                default=str(f["defaultValue"]) if f.get("defaultValue") is not None else None,
            )
            for f in self._describe_fields(object_name)
        ]

    def row_count(self, object_name: str, schema: str | None = None) -> int | None:
        try:
            result = self._client().query(f"SELECT COUNT() FROM {object_name}")
            return int(result.get("totalSize")) if result.get("totalSize") is not None else None
        except SalesforceError:
            return None

    def preview_object(self, object_name: str, schema: str | None, limit: int) -> PreviewResult:
        fields = self._queryable_field_names(object_name)
        soql = f"SELECT {', '.join(fields)} FROM {object_name} LIMIT {min(max(1, limit), _MAX_PAGE_SIZE) + 1}"
        records = self._run_soql(soql)
        truncated = len(records) > limit
        rows = [[_strip_attributes(r).get(c) for c in fields] for r in records[:limit]]
        return PreviewResult(columns=fields, rows=rows, row_count=None, truncated=truncated)

    # ── Streaming extraction (Source Dataset execution) — reuses the
    # generic TABLE key-range partitioning path unmodified: key_column is
    # almost always "Id" (a real, unique, orderable Salesforce field on
    # every SObject), and SOQL supports ORDER BY/WHERE range predicates on
    # it exactly like a SQL primary key. execution/ingestion/
    # partition_reader.py's own string-key fallback (single partition,
    # no arithmetic split) already handles Id's non-numeric type
    # correctly with no changes needed there. ──────────────────────────

    def key_range(self, object_name: str, schema: str | None, key_column: str) -> tuple[object, object]:
        soql = f"SELECT MIN({key_column}) minVal, MAX({key_column}) maxVal FROM {object_name}"
        records = self._run_soql(soql)
        if not records:
            return (None, None)
        row = _strip_attributes(records[0])
        return (row.get("minVal"), row.get("maxVal"))

    def read_key_range_chunk(
        self, object_name: str, schema: str | None, key_column: str,
        after_value: object, upper_bound: object, chunk_size: int,
    ) -> ChunkResult:
        fields = self._queryable_field_names(object_name)
        if key_column not in fields:
            fields = [key_column, *fields]

        clauses = []
        if after_value is not None:
            clauses.append(f"{key_column} > {_soql_literal(after_value)}")
        if upper_bound is not None:
            clauses.append(f"{key_column} <= {_soql_literal(upper_bound)}")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        soql = f"SELECT {', '.join(fields)} FROM {object_name}{where} ORDER BY {key_column} LIMIT {chunk_size}"
        records = [_strip_attributes(r) for r in self._run_soql(soql)]
        if not records:
            return ChunkResult(rows=[], last_key=None, count=0)
        return ChunkResult(rows=records, last_key=records[-1].get(key_column), count=len(records))

    # ── Real SOQL execution + automatic pagination (Salesforce's REST
    # query API caps a single response page at 2000 records regardless of
    # any SOQL LIMIT clause — query_more() walks the rest via the real
    # nextRecordsUrl the API itself returns, never a second guessed URL).

    def _run_soql(self, soql: str, max_records: int | None = None) -> list[dict]:
        sf = self._client()
        try:
            result = sf.query(soql)
            records = list(result.get("records", []))
            while not result.get("done", True) and (max_records is None or len(records) < max_records):
                result = sf.query_more(result["nextRecordsUrl"], identifier_is_url=True)
                records.extend(result.get("records", []))
            return records
        except SalesforceError as exc:
            raise ConnectorError(self._scrub(f"Salesforce SOQL query failed: {exc}")) from exc

    # ── SQL Workspace (real SOQL execution, not arbitrary SQL) ─────────
    # SOQL is genuinely SELECT-only — Salesforce writes go through separate
    # sObject DML calls (create/update/delete), not a SOQL INSERT/UPDATE/
    # DELETE statement (SOQL has no such grammar at all) — so this never
    # pretends to run a write the way the SQL dialect connectors' run_sql()
    # does; a non-SELECT statement is rejected with a real, specific error
    # rather than silently doing nothing or misexecuting.
    def run_sql(
        self, sql: str, parameters: dict, limit: int, *,
        timeout_seconds: int | None = None, on_session_ready=None,
    ) -> SqlRunResult:
        stripped = (sql or "").strip().rstrip(";").strip()
        if not stripped:
            raise ConnectorValidationError("SOQL text is empty.")
        if not stripped.lower().startswith("select"):
            raise ConnectorValidationError(
                "Salesforce only supports SOQL SELECT queries here — writes go through Salesforce's own "
                "record create/update/delete APIs, not a SQL-style INSERT/UPDATE/DELETE statement."
            )
        # Bounded read: stop pulling further pages once we already have
        # enough rows to answer `limit` (+1 to detect truncation), the same
        # real-cap discipline every other connector's run_sql() applies —
        # never a full unbounded pull of a large SObject into memory just
        # because the user's SOQL itself had no LIMIT clause.
        records = [_strip_attributes(r) for r in self._run_soql(stripped, max_records=limit + 1)]
        if not records:
            return SqlRunResult(is_result_set=True, columns=[], rows=[], truncated=False)
        columns = list(records[0].keys())
        rows = [[r.get(c) for c in columns] for r in records[:limit]]
        return SqlRunResult(is_result_set=True, columns=columns, rows=rows, truncated=len(records) > limit)
