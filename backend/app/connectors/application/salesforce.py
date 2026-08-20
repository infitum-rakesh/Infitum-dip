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
preview_sql()/run_sql() (SQL Workspace's custom-SQL surface); building a
SOQL-aware SQL editor is real, separate scope this task doesn't cover.
BaseConnector's own default ("does not support custom SQL ...") surfaces
that honestly rather than silently mistranslating SOQL as SQL.

open_sql_stream() IS implemented, though — real Salesforce Bulk API 2.0
(simple-salesforce's own bulk2 module, not hand-rolled HTTP), the
mechanism Salesforce itself documents for exactly this shape of read: an
open-ended, potentially huge result set (hundreds of thousands to
hundreds of millions of rows), as opposed to the REST Query API
_run_soql() below uses for interactive preview/small bounded reads (hard
2,000-row page cap, one API call per page — fine for a preview, a real
risk of exhausting an org's daily API call allotment at genuine
large-volume scale). See _SalesforceBulkSqlStream below: submits one real
async Bulk API 2.0 query job, waits for it, then pages results back via
Salesforce's own real locator mechanism — this is what makes Salesforce a
first-class source for execution/load_worker.py's Run Load (Column
Mapping → target) and execution/worker.py's QUERY-mode Run Extraction,
not just the TABLE key-range path below.

Real automatic fallback: not every org has Bulk API 2.0 enabled (some
Salesforce editions/permission sets restrict it), and a Bulk job can fail
for reasons that have nothing to do with the query itself (org-side job
queue issues, a transient timeout waiting for JobComplete). Rather than
hard-failing the whole read, open_sql_stream() catches a Bulk API 2.0
failure and falls back to _SalesforceRestSqlStream — the same real REST
Query API _run_soql() uses (query()/query_more()), just wrapped as a
genuinely chunked SqlStream instead of accumulating every row in memory
first. Slower and more API-call-hungry than Bulk API 2.0 at real scale,
but a real, working read instead of a hard failure — and the fallback is
logged (print), never silent.

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

import csv
import io
import re
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
    from simple_salesforce import Salesforce
    from simple_salesforce.exceptions import SalesforceAuthenticationFailed, SalesforceError
except ImportError:  # pragma: no cover - simple-salesforce is in requirements.txt; guarded for import-time safety only
    Salesforce = None
    SalesforceError = Exception
    SalesforceAuthenticationFailed = Exception

_NON_QUERYABLE_COLUMN_TYPES = {"address", "location", "base64"}
_MAX_PAGE_SIZE = 2000  # Salesforce REST query API's own hard per-request cap
_BULK_PAGE_SIZE = 50_000  # Bulk API 2.0's own default/recommended page size per HTTP round trip


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


def _parse_soql_select_fields(soql: str) -> list[str]:
    """Best-effort fallback: pulls the field list straight out of a
    `SELECT a, b, c FROM ...` SOQL string. Only used when a Bulk API 2.0
    query genuinely returns zero pages (see _SalesforceBulkSqlStream) so a
    caller still gets a correctly-headered empty result instead of an
    empty column list — real Bulk API 2.0 responses normally include a
    header row even for a zero-row match, so this is a rarely-hit safety
    net, not the primary source of truth for columns."""
    m = re.match(r"\s*SELECT\s+(.+?)\s+FROM\s", soql, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    return [f.strip().split(" ")[-1] for f in m.group(1).split(",") if f.strip()]


class _SalesforceBulkSqlStream(SqlStream):
    """Wraps simple-salesforce's bulk2 query() generator — which submits a
    real Bulk API 2.0 query job and pages results back via Salesforce's
    own real locator mechanism — behind the SqlStream contract every
    other chunked-read caller in this codebase already expects
    (open_sql_stream()/fetch_chunk()/close(), see connectors/base.py).

    Bulk API 2.0's own page size (_BULK_PAGE_SIZE, one real HTTP round
    trip per page) is decoupled from the caller's requested chunk size:
    fetch_chunk(size) buffers rows across as many real pages as it takes
    and slices off exactly `size` at a time, so a caller's own
    checkpoint/staging/batch granularity is free to differ from Salesforce's
    page size."""

    def __init__(self, page_generator):
        self._pages = page_generator
        self._buffer: list[dict] = []
        self._columns: list[str] = []
        self._exhausted = False
        self._pull_page()  # prime — columns are known as soon as the stream opens, matching the SqlStream contract

    def _pull_page(self) -> bool:
        if self._exhausted:
            return False
        try:
            csv_text = next(self._pages)
        except StopIteration:
            self._exhausted = True
            return False
        reader = csv.DictReader(io.StringIO(csv_text))
        if not self._columns:
            self._columns = list(reader.fieldnames or [])
        self._buffer.extend(dict(row) for row in reader)
        return True

    @property
    def columns(self) -> list[str]:
        return self._columns

    def fetch_chunk(self, size: int) -> list[dict]:
        while len(self._buffer) < size and self._pull_page():
            pass
        rows, self._buffer = self._buffer[:size], self._buffer[size:]
        return rows

    def close(self) -> None:
        # Bulk API 2.0 jobs auto-expire on Salesforce's own side; the
        # underlying HTTP session is this connector's long-lived one, not
        # opened per-stream — nothing real to release here.
        self._buffer = []


class _SalesforceRestSqlStream(SqlStream):
    """Real REST Query API stream — the fallback open_sql_stream() uses
    when Bulk API 2.0 fails (see module docstring). Wraps sf.query()/
    query_more() (the exact same real pagination _run_soql() uses) behind
    the SqlStream contract, buffering across as many 2,000-row REST pages
    as it takes to satisfy a caller's own fetch_chunk(size) — same
    decoupling _SalesforceBulkSqlStream does for Bulk API 2.0 pages."""

    def __init__(self, sf, soql: str):
        self._sf = sf
        self._soql = soql
        self._buffer: list[dict] = []
        self._columns: list[str] = []
        self._done = False
        self._next_url: str | None = None
        self._issued_first_query = False
        self._pull_page()  # prime — columns known as soon as the stream opens

    def _pull_page(self) -> bool:
        if self._done:
            return False
        if not self._issued_first_query:
            self._issued_first_query = True
            result = self._sf.query(self._soql)
        else:
            result = self._sf.query_more(self._next_url, identifier_is_url=True)
        records = [_strip_attributes(r) for r in result.get("records", [])]
        if records and not self._columns:
            self._columns = list(records[0].keys())
        self._buffer.extend(records)
        self._done = result.get("done", True)
        if not self._done:
            self._next_url = result["nextRecordsUrl"]
        return True

    @property
    def columns(self) -> list[str]:
        return self._columns

    def fetch_chunk(self, size: int) -> list[dict]:
        while len(self._buffer) < size and self._pull_page():
            pass
        rows, self._buffer = self._buffer[:size], self._buffer[size:]
        return rows

    def close(self) -> None:
        self._buffer = []


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
                # Real per-field length from Salesforce's own describe() —
                # meaningful for string/textarea/picklist/email/phone/id/
                # reference types (Salesforce sets it for every one of
                # those); 0/absent for genuinely non-string types (int,
                # boolean, date, ...), which we normalize to None here so a
                # bulk target-table builder doesn't try to size a numeric
                # column off it.
                max_length=(int(f["length"]) if f.get("length") else None),
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

    # ── Bulk API 2.0 streaming (Run Load / QUERY-mode Run Extraction /
    # Run Pipeline customSql — anywhere open_sql_stream() is the real,
    # genuinely-unbounded chunked-read primitive) — see module docstring
    # and _SalesforceBulkSqlStream above for why this is a SEPARATE real
    # mechanism from _run_soql()'s REST Query API pagination, not just
    # this same call reused with a bigger loop. ─────────────────────────

    def open_sql_stream(self, sql: str, parameters: dict) -> SqlStream:
        stripped = (sql or "").strip()
        if not stripped:
            raise ConnectorValidationError("SOQL text is empty.")
        # Named-parameter substitution — SOQL has no client-side bind-
        # parameter API the way SQLAlchemy's text() does, so each :name
        # token is resolved to a real, escaped SOQL literal (_soql_literal
        # above) before submission, the same discipline read_key_range_chunk
        # already applies to its own WHERE clause values.
        for key, value in (parameters or {}).items():
            stripped = re.sub(rf":{re.escape(key)}\b", lambda _m, v=value: _soql_literal(v), stripped)
        sf = self._client()
        try:
            page_generator = sf.bulk2.Query.query(stripped, max_records=_BULK_PAGE_SIZE)
            stream = _SalesforceBulkSqlStream(page_generator)
        except Exception as bulk_exc:  # job creation, polling, or first-page fetch can all fail — SalesforceError, SalesforceOperationError, or a bare network error
            # Real automatic fallback (not a hard failure): some orgs don't
            # have Bulk API 2.0 enabled at all, and a Bulk job can fail for
            # reasons unrelated to the query itself (org-side queue issues,
            # a timeout waiting for JobComplete). Retry the SAME read via
            # the REST Query API's own real pagination instead of giving up
            # — slower and more API-call-hungry at real scale, but a real,
            # working read. Logged, never silent.
            print(f"[salesforce] Bulk API 2.0 query failed, falling back to REST Query API: {self._scrub(str(bulk_exc))}")
            try:
                stream = _SalesforceRestSqlStream(sf, stripped)
            except Exception as rest_exc:
                raise ConnectorError(self._scrub(
                    f"Salesforce query failed via both Bulk API 2.0 ({bulk_exc}) and the REST Query API fallback ({rest_exc})."
                )) from rest_exc
        if not stream.columns:
            stream._columns = _parse_soql_select_fields(stripped)
        return stream

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
