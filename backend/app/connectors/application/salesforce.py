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

SOQL is NOT SQL, but run_sql() below IS implemented (SQL Workspace's
custom-SQL surface) — it runs real SOQL, not translated/pretend SQL,
including a real SELECT-only restriction (SOQL writes go through separate
sObject DML calls with no INSERT/UPDATE/DELETE grammar to translate).
This connector deliberately does NOT implement preview_sql() (arbitrary,
un-object-scoped custom SQL preview) or explain()/query
cancellation/timeout — those stay on BaseConnector's default
ConnectorUnsupportedError rather than a fabricated result. One real,
disclosed convenience run_sql() (and open_sql_stream() below) both apply:
SOQL has no `SELECT *`, so a bare "SELECT * FROM <Object>" is expanded at
run time into a real, explicit field list off that object's own
describe() metadata before ever reaching Salesforce's API — see
_expand_select_star() — so typing the same SQL habit a user would use
against any SQL-dialect connection just works here too, instead of
surfacing Salesforce's own confusing "unexpected token '*'" syntax fault.

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
    from simple_salesforce.bulk2 import Operation, ColumnDelimiter, LineEnding
    from simple_salesforce.exceptions import SalesforceAuthenticationFailed, SalesforceError
except ImportError:  # pragma: no cover - simple-salesforce is in requirements.txt; guarded for import-time safety only
    Salesforce = None
    Operation = ColumnDelimiter = LineEnding = None
    SalesforceError = Exception
    SalesforceAuthenticationFailed = Exception

_NON_QUERYABLE_COLUMN_TYPES = {"location", "base64"}
_MAX_PAGE_SIZE = 2000  # Salesforce REST query API's own hard per-request cap
_BULK_PAGE_SIZE = 50_000  # Bulk API 2.0's own default/recommended page size per HTTP round trip

# CPython's csv module caps a single field at 131,072 bytes by default
# (csv.field_size_limit()'s own stock value). Salesforce's Long Text Area /
# Rich Text Area fields (e.g. EmailMessage.HtmlBody/TextBody) can
# legitimately reach right around that size, so a Bulk API 2.0 CSV page
# containing one used to blow up mid-stream with the confusing
# "_csv.Error: field larger than field limit (131072)" — a Python CSV
# parsing limit, not a real Salesforce or Oracle error (it surfaced
# further up the pipeline as a misleading "[TARGET] insert failed", since
# the target was never actually reached for that chunk). Raised well
# above Salesforce's own maximum field sizes so no genuine value can hit
# this ceiling again; _pull_page() below also rejects-and-continues on
# any row that still fails to parse, as defense in depth.
try:
    csv.field_size_limit(50_000_000)
except OverflowError:  # pragma: no cover - only on platforms with a 32-bit C long
    csv.field_size_limit(2_147_483_647)


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


_SELECT_STAR_RE = re.compile(r"\bSELECT\s+\*\s+FROM\s+([A-Za-z_]\w*)", re.IGNORECASE)


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


_ISO_DATETIME_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

def _normalize_sfdc_value(val):
    if val == "" or val is None:
        return None
    if isinstance(val, str) and _ISO_DATETIME_Z_RE.match(val):
        return val[:-1] + "+0000"
    return val


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

    def __init__(self, page_generator, log_fn=None):
        self._pages = page_generator
        self._log_fn = log_fn
        self._buffer: list[dict] = []
        self._columns: list[str] = []
        self._exhausted = False
        self._page_num = 0
        self._total_rows = 0
        self._parse_errors: list[dict] = []
        self._pull_page()  # prime — columns are known as soon as the stream opens, matching the SqlStream contract

    def _pull_page(self) -> bool:
        if self._exhausted:
            return False
        try:
            csv_text = next(self._pages)
        except StopIteration:
            self._exhausted = True
            msg = f"Bulk API stream exhausted — {self._total_rows:,} total row(s) across {self._page_num} page(s)."
            print(f"[SFDC Bulk] {msg}", flush=True)
            if self._log_fn:
                try: self._log_fn("INFO", msg)
                except Exception: pass
            return False
        reader = csv.DictReader(io.StringIO(csv_text))
        if not self._columns:
            self._columns = list(reader.fieldnames or [])
        rows: list[dict] = []
        while True:
            try:
                raw_row = next(reader)
            except StopIteration:
                break
            except csv.Error as exc:
                # A single still-oversized/malformed CSV row (see the
                # field_size_limit note above) shouldn't abort the whole
                # page — reject just that row like any other bad record
                # and keep parsing the rest of the page. Drained by
                # pop_parse_errors() below and recorded as a real
                # RejectedRecord by the executor, same view/download UI
                # a target-write reject already uses.
                self._parse_errors.append({
                    "row": {"_source": "salesforce_bulk_csv", "_page": self._page_num + 1, "_csv_line": reader.line_num},
                    "error": f"Skipped during Salesforce Bulk API CSV parsing (page {self._page_num + 1}, line {reader.line_num}): {exc}",
                })
                msg = f"Bulk API page {self._page_num + 1}: skipped one unparseable row at CSV line {reader.line_num} ({exc})."
                print(f"[SFDC Bulk] {msg}", flush=True)
                if self._log_fn:
                    try: self._log_fn("WARN", msg)
                    except Exception: pass
                continue
            rows.append({k: _normalize_sfdc_value(v) for k, v in raw_row.items()})
        self._buffer.extend(rows)
        self._page_num += 1
        self._total_rows += len(rows)
        msg = f"Bulk API page {self._page_num}: downloaded {len(rows):,} row(s) (running total: {self._total_rows:,})."
        print(f"[SFDC Bulk] {msg}", flush=True)
        if self._log_fn:
            try: self._log_fn("INFO", msg)
            except Exception: pass
        return True

    @property
    def columns(self) -> list[str]:
        return self._columns

    def pop_parse_errors(self) -> list[dict]:
        """Drains and returns any rows skipped by _pull_page()'s per-row
        csv.Error handling since the last call — polled by the executor
        after each fetch_chunk() alongside normal target-write rejects."""
        errors, self._parse_errors = self._parse_errors, []
        return errors

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
        self._page_num = 0
        self._total_rows = 0
        self._total_size: int | None = None
        self._pull_page()  # prime — columns known as soon as the stream opens

    def _pull_page(self) -> bool:
        if self._done:
            return False
        if not self._issued_first_query:
            self._issued_first_query = True
            result = self._sf.query(self._soql)
            self._total_size = result.get("totalSize")
            if self._total_size is not None:
                print(f"[SFDC] REST Query API — total records to fetch: {self._total_size:,}", flush=True)
        else:
            result = self._sf.query_more(self._next_url, identifier_is_url=True)
        records = [_strip_attributes(r) for r in result.get("records", [])]
        if records and not self._columns:
            self._columns = list(records[0].keys())
        self._buffer.extend(records)
        self._page_num += 1
        self._total_rows += len(records)
        self._done = result.get("done", True)
        if not self._done:
            self._next_url = result["nextRecordsUrl"]
        of_total = f"/{self._total_size:,}" if self._total_size is not None else ""
        print(f"[SFDC] REST page {self._page_num}: fetched {len(records):,} row(s) (running total: {self._total_rows:,}{of_total}).", flush=True)
        if self._done:
            print(f"[SFDC] REST Query API stream exhausted — {self._total_rows:,} total row(s) across {self._page_num} page(s).", flush=True)
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

        print(f"[SFDC] Connecting to Salesforce ({domain or 'login'}) as {username}...", flush=True)
        try:
            self._sf = Salesforce(**kwargs)
        except SalesforceAuthenticationFailed as exc:
            print(f"[SFDC] Authentication failed: {self._scrub(str(exc))}", flush=True)
            raise ConnectorError(self._scrub(f"Salesforce authentication failed: {exc}")) from exc
        except SalesforceError as exc:
            print(f"[SFDC] Login failed: {self._scrub(str(exc))}", flush=True)
            raise ConnectorError(self._scrub(f"Salesforce login failed: {exc}")) from exc
        print(f"[SFDC] Connected successfully (API v{self._sf.sf_version}, instance {self._sf.sf_instance}).", flush=True)
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
        raw_fields = self._describe_fields(object_name)
        names: list[str] = []
        for f in raw_fields:
            ftype = f.get("type")
            if ftype in _NON_QUERYABLE_COLUMN_TYPES:
                continue
            if ftype == "address":
                # Expand compound address fields to their standard queryable scalar component fields
                base_prefix = f["name"].replace("Address", "")
                components = [c["name"] for c in raw_fields if c["name"].startswith(base_prefix) and c["name"] != f["name"] and c.get("type") not in _NON_QUERYABLE_COLUMN_TYPES]
                if components:
                    names.extend(components)
                else:
                    names.append(f["name"])
            else:
                names.append(f["name"])
        # Deduplicate while preserving order
        seen = set()
        dedup_names = []
        for n in names:
            if n not in seen:
                seen.add(n)
                dedup_names.append(n)
        if not dedup_names:
            raise ConnectorError(f"Salesforce object '{object_name}' has no queryable fields.")
        return dedup_names

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

    def build_select_all_sql(self, object_name: str, schema: str | None = None) -> str:
        """Real "read everything" SOQL for object_name, with NO LIMIT
        clause — unlike preview_object() above (which intentionally caps
        at _MAX_PAGE_SIZE for its own bounded-preview purpose), this is
        what execution/pipeline_job_executor.py's _run_source() hands to
        open_sql_stream() for a genuinely exhaustive extraction. SOQL has
        no `SELECT *`, so the field list comes from this SObject's own
        real describe() metadata, same field-selection rule
        _queryable_field_names() already applies everywhere else in this
        connector (excludes compound/blob fields — see module docstring)."""
        fields = self._queryable_field_names(object_name)
        return f"SELECT {', '.join(fields)} FROM {object_name}"

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

    def _expand_select_star(self, soql: str, log_fn=None) -> str:
        """SOQL has no `SELECT *` — Salesforce's real API rejects it
        outright with a syntax fault. Both SQL Workspace (run_sql, below)
        and the streaming/custom-SQL surface (open_sql_stream) accept
        freeform text a user typed by hand, so a user writing the SQL
        habit "SELECT * FROM Account" would otherwise always fail with a
        confusing Salesforce-side error instead of just working like
        every SQL-dialect connector's SELECT * already does. This expands
        it at run time into a real, explicit field list off the object's
        own describe() metadata (same _queryable_field_names() rule
        get_columns()/preview_object()/read_key_range_chunk() already use
        — excludes compound address/location fields and blob fields, same
        as everywhere else in this connector), then substitutes it back
        into the query text so WHERE/ORDER BY/LIMIT clauses the user wrote
        are preserved unchanged. A no-op (returns soql unchanged) for any
        query that isn't a bare `SELECT * FROM <Object>` shape — SOQL
        subqueries and multi-object joins aren't rewritten here, same
        disclosed narrow scope the reference ETL engine's own
        _expand_sfdc_select_star() uses."""
        match = _SELECT_STAR_RE.search(soql)
        if not match:
            return soql
        object_name = match.group(1)
        msg_expanding = f"Expanding SELECT * for {object_name}..."
        print(f"[SFDC] {msg_expanding}", flush=True)
        if log_fn:
            try: log_fn(msg_expanding)
            except Exception: pass
        try:
            fields = self._queryable_field_names(object_name)
        except ConnectorError as exc:
            print(f"[SFDC] Could not expand SELECT * for {object_name}: {exc}", flush=True)
            raise
        msg_expanded = f"Expanded to {len(fields)} fields"
        print(f"[SFDC] {msg_expanded}", flush=True)
        if log_fn:
            try: log_fn(msg_expanded)
            except Exception: pass
        field_list = ", ".join(fields)
        expanded = _SELECT_STAR_RE.sub(f"SELECT {field_list} FROM {object_name}", soql, count=1)
        return expanded

    # ── Bulk API 2.0 streaming (Run Load / QUERY-mode Run Extraction /
    # Run Pipeline customSql — anywhere open_sql_stream() is the real,
    # genuinely-unbounded chunked-read primitive) — see module docstring
    # and _SalesforceBulkSqlStream above for why this is a SEPARATE real
    # mechanism from _run_soql()'s REST Query API pagination, not just
    # this same call reused with a bigger loop. ─────────────────────────

    def open_sql_stream(self, sql: str, parameters: dict, *, extraction_mode: str | None = None, log_fn=None, cancel_check=None) -> SqlStream:
        """extraction_mode threads through the Source Dataset node's real
        "Load Type" setting (BULK / AUTO / ROW_BY_ROW — see
        SourceDatasetConfig.jsx, Salesforce-only field) so a user's
        explicit choice actually changes what happens here, instead of
        being saved into the pipeline definition and then silently
        ignored (the bug this fixes):
          BULK       — force real Bulk API 2.0; still falls back to the
                       REST Query API on a genuine job failure (an org
                       without Bulk API enabled, or a transient job-queue
                       issue) rather than hard-failing the whole
                       extraction, but logs it clearly as the user's own
                       BULK choice being unable to run, not a routine path.
          ROW_BY_ROW — skip Bulk API entirely and go straight to the REST
                       Query API's standard query()/query_more()
                       pagination — the right choice for small objects or
                       orgs where Bulk API 2.0 isn't available/desired.
          AUTO/None  — this connector's original behavior: try Bulk API
                       2.0 first, fall back to REST silently-but-logged on
                       any failure.
        """
        stripped = (sql or "").strip()
        if not stripped:
            raise ConnectorValidationError("SOQL text is empty.")
        
        def _emit(msg):
            print(f"[SFDC] {msg}", flush=True)
            if log_fn:
                try:
                    log_fn("INFO", msg)
                except Exception:
                    pass

        _emit("Connecting to source: SFDC")
        sf = self._client()
        _emit("Connected to Salesforce")

        stripped = self._expand_select_star(stripped, log_fn=_emit)
        for key, value in (parameters or {}).items():
            stripped = re.sub(rf":{re.escape(key)}\b", lambda _m, v=value: _soql_literal(v), stripped)

        match_obj = _SELECT_STAR_RE.search(sql) or re.search(r"\bFROM\s+([A-Za-z_]\w*)", sql, re.IGNORECASE)
        obj_name_for_log = match_obj.group(1) if match_obj else "object"
        _emit(f"Fetching SFDC metadata for {obj_name_for_log}...")

        mode = (extraction_mode or "AUTO").upper()

        if mode == "ROW_BY_ROW":
            _emit("Load Type = ROW_BY_ROW — using REST Query API pagination (Bulk API 2.0 not attempted).")
            stream = _SalesforceRestSqlStream(sf, stripped)
            if not stream.columns:
                stream._columns = _parse_soql_select_fields(stripped)
            return stream

        _emit("Using Bulk API 2.0 for extraction...")
        _emit(f"Creating Bulk 2.0 query job for {obj_name_for_log}...")
        try:
            res = sf.bulk2.Query._client.create_job(
                Operation.query,
                stripped,
                ColumnDelimiter.COMMA,
                LineEnding.LF,
            )
            sfdc_job_id = res.get("id")
            if sfdc_job_id:
                _emit(f"Bulk job created: {sfdc_job_id}")

            t0 = time.time()
            last_poll_log = 0
            # Cancellation-aware polling loop for Salesforce Bulk API 2.0 job
            while True:
                if cancel_check and cancel_check():
                    _emit(f"Cancellation requested! Aborting Salesforce Bulk API job {sfdc_job_id}...")
                    try:
                        sf.bulk2.Query._client.abort_job(sfdc_job_id, is_query=True)
                        _emit(f"Salesforce Bulk API job {sfdc_job_id} status: ABORTED")
                    except Exception as abort_err:
                        _emit(f"Could not abort Salesforce Bulk job {sfdc_job_id}: {abort_err}")
                    raise ConnectorError("Salesforce Bulk API extraction was aborted by user.")

                job_info = sf.bulk2.Query._client.get_job(sfdc_job_id, is_query=True)
                job_status = job_info.get("state")
                processed_count = job_info.get("numberRecordsProcessed")

                now = time.time()
                if now - last_poll_log >= 10:
                    last_poll_log = now
                    elapsed_s = int(now - t0)
                    proc_str = f" | Processed: {processed_count:,}" if processed_count is not None else ""
                    _emit(f"Bulk job: {str(job_status).lower()} | Elapsed: {elapsed_s}s | SFDC is preparing data{proc_str}...")

                if job_status in ("JobComplete", "Aborted", "Failed"):
                    if job_status != "JobComplete":
                        err_msg = job_info.get("errorMessage") or job_info
                        raise ConnectorError(f"Salesforce Bulk job failure ({job_status}): {err_msg}")
                    break
                time.sleep(2.0)

            elapsed = time.time() - t0
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            _emit(f"Bulk job completed in {time_str}!")

            def _page_gen():
                locator = "INIT"
                while locator:
                    if cancel_check and cancel_check():
                        _emit(f"Cancellation requested during page download! Stopping fetch.")
                        raise ConnectorError("Salesforce result download aborted by user.")
                    if locator == "INIT":
                        locator = ""
                    res_chunk = sf.bulk2.Query._client.get_query_results(sfdc_job_id, locator, _BULK_PAGE_SIZE)
                    locator = res_chunk.get("locator")
                    yield res_chunk["records"]

            _emit("Downloading bulk query results...")
            stream = _SalesforceBulkSqlStream(_page_gen(), log_fn=log_fn)
        except Exception as bulk_exc:
            if "aborted by user" in str(bulk_exc).lower():
                raise
            level = "BULK mode requested but Bulk API 2.0 failed" if mode == "BULK" else "Bulk API 2.0 query failed"
            _emit(f"{level}, falling back to REST Query API: {self._scrub(str(bulk_exc))}")
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
        # SOQL has no `SELECT *` and Salesforce's real API rejects it
        # outright — expand it to a real, explicit field list off the
        # object's own describe() metadata before ever reaching the API,
        # so writing "SELECT * FROM Account" in SQL Workspace just works
        # the way it does against every SQL-dialect connection, instead of
        # surfacing a confusing Salesforce-side syntax fault. See
        # _expand_select_star() above.
        stripped = self._expand_select_star(stripped)
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
