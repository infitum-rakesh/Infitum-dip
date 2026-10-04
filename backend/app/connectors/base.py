"""
Connector interface — the seam every data-source/target integration in
this codebase implements.

Phase 1 (Connection Management) only needs test_connection(): given a
Connection's non-secret config and decrypted secret values, actually try
to reach the target and report what really happened. No connector may
return a fabricated success or a randomized outcome — see
ConnectorUnsupportedError below for the one case where a real attempt
genuinely can't be made yet.

Phase 2 (Source Dataset node) adds schema/object browsing, column
introspection and bounded preview — every method below a connector
doesn't genuinely implement raises ConnectorUnsupportedError rather than
returning empty/fake data, same rule as test_connection().

Phase 3 (streaming ingestion engine) adds real chunked/streaming reads:
key_range() + read_key_range_chunk() for key-range-paginated, partition-
able TABLE/VIEW extraction (never OFFSET — see connectors/database/base.py
module docstring), and open_sql_stream() for a single bounded-memory
streaming read over an arbitrary custom SELECT. Both are consumed by
app/execution/worker.py, never by browser code.

Phase 5 (File/SFTP connectors) adds file-oriented operations for STORAGE-
category connections: list_files() (pattern-matched directory listing),
open_read_stream() (a real binary stream — never the whole file in
memory), move_file() (archive-on-success / error-directory-on-failure per
spec), and file_checksum() (streamed SHA-256 for duplicate detection).
See connectors/sftp/client.py and connectors/files/local.py.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


def scrub_secrets(message: str, *secret_dicts: dict) -> str:
    """
    Free-function secret redaction for any error text about to be
    persisted or returned to a user — the same defense-in-depth
    BaseConnector._scrub() applies, usable anywhere a connector instance
    isn't directly at hand but its .secrets dict is (e.g. the execution
    engines in execution/, or reconciliation's two-connector run, which
    needs to scrub against BOTH source and target secrets at once).
    Real drivers generally don't echo passwords back in error messages
    (verified for psycopg2/PyMySQL/pymssql during Phase 1 testing), but
    this guarantees it even if a future driver's error format changes —
    never trust a third-party exception string by default when secrets
    are in scope. Every place across this codebase that turns a raw
    exception into a stored/returned error_message runs it through this
    (Phase 8 hardening) — worker.py, load_worker.py, pipeline_executor.py,
    load_executor.py, validation_service.py, reconciliation_service.py.
    """
    scrubbed = message
    for secrets in secret_dicts:
        for value in (secrets or {}).values():
            if value and isinstance(value, str) and len(value) >= 3:
                scrubbed = scrubbed.replace(value, "••••••••")
    # Cap length — some drivers append full stack-trace-like detail.
    first_line = scrubbed.splitlines()[0].strip() if scrubbed else scrubbed
    return first_line[:300]


class ConnectorError(Exception):
    """Base class for all connector-raised errors."""


class ConnectorUnsupportedError(ConnectorError):
    """
    Raised when a connection type has a defined interface (see
    connectors/database/oracle.py etc.) but no driver-backed implementation
    verified against a live instance yet.

    This is the ONLY acceptable way for a connector to avoid performing a
    real connection attempt — it must say so explicitly rather than
    returning a plausible-looking fake success/failure. See
    connectors/registry.py UNVERIFIED for the current list and why.
    """


class ConnectorValidationError(ConnectorError):
    """Raised for bad caller input (e.g. a non-SELECT custom SQL preview)
    — a 400, not a connectivity failure."""


@dataclass
class ConnectionTestResult:
    success: bool
    message: str
    latency_ms: float | None = None
    server_version: str | None = None


@dataclass
class ObjectInfo:
    name: str
    schema: str | None
    object_type: str  # "TABLE" | "VIEW"


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    primary_key: bool = False
    unique: bool = False
    indexed: bool = False
    default: str | None = None
    # {"table": str, "column": str, "schema": str | None} when this column
    # is a real foreign key (from the dialect's own reflected constraint —
    # see connectors/database/base.py's get_columns()); None otherwise.
    # Used by the SQL Workspace database explorer's relationship view.
    foreign_key: dict | None = None
    # Real max character length for a string-ish column, when the source
    # actually knows it — the reflected SQLAlchemy String/VARCHAR type's own
    # .length (database/base.py) or Salesforce field describe()'s own real
    # "length" (application/salesforce.py). None for non-string columns, or
    # for a string type with no fixed length (e.g. Postgres TEXT). Used by
    # loading/writers/base.py's ensure_table_from_columns() to create a
    # properly-sized VARCHAR2/VARCHAR target column instead of a blanket
    # CLOB/TEXT for every string field — the difference matters enormously
    # at large-row-count bulk-insert scale (see JJ1).
    max_length: int | None = None


@dataclass
class PreviewResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int | None = None  # exact count when cheaply available; None when not computed
    truncated: bool = False       # more rows exist beyond what was fetched for this preview


@dataclass
class ExplainResult:
    """Result of explain() (SQL Workspace's Explain Query) — two real
    shapes, never both faked into one: `plan_text` for dialects whose
    EXPLAIN returns a single line-oriented plan column (e.g. Postgres),
    `columns`/`rows` for dialects whose EXPLAIN returns a genuine multi-
    column result set (e.g. MySQL's id/select_type/table/type/... table).
    A connector sets exactly one shape based on what its dialect's own
    EXPLAIN actually returns — never invents columns that aren't real."""
    plan_text: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)


@dataclass
class SqlRunResult:
    """Result of run_sql() (Phase 9: SQL Workspace) — genuinely broader
    than PreviewResult: a read statement (SELECT/WITH/SHOW/EXPLAIN/...)
    populates columns/rows/truncated exactly like preview; a write
    statement (INSERT/UPDATE/DELETE/DDL) populates rowcount instead and
    leaves rows empty — is_result_set tells the caller which shape it is,
    never both faked into one."""
    is_result_set: bool
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False
    rowcount: int | None = None  # rows affected — only meaningful when is_result_set is False


@dataclass
class ChunkResult:
    rows: list[dict] = field(default_factory=list)  # column name -> value, one dict per row
    last_key: object | None = None                  # this chunk's max key value — the next checkpoint
    count: int = 0


@dataclass
class FileInfo:
    name: str               # filename only, relative to the connection's base_path
    path: str                # full path as the connector's own client understands it
    size: int
    modified_at: object | None = None  # datetime | None — not every backend reports this reliably
    is_directory: bool = False


class SqlStream(abc.ABC):
    """A single open, bounded-memory streaming cursor over a custom SELECT
    — see open_sql_stream(). Callers MUST call close() when done (or on
    error) to release the underlying connection back to the pool."""

    @property
    @abc.abstractmethod
    def columns(self) -> list[str]:
        """Column names, known as soon as the stream is opened — lets a
        caller build a correctly-headered empty result (e.g. an empty
        pandas DataFrame) even when the query matches zero rows, without
        needing a first fetch_chunk() call to find out."""
        raise NotImplementedError

    @abc.abstractmethod
    def fetch_chunk(self, size: int) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class BaseConnector(abc.ABC):
    """
    One instance wraps a single Connection's config for the duration of an
    operation. Secrets are passed in already decrypted by the caller
    (connection_service, which owns the encryption boundary) and are never
    persisted, logged, or included in any exception message this class
    lets escape — see _scrub() below.
    """

    connection_type: str = ""

    def __init__(self, config: dict, secrets: dict, cache_key: str | None = None):
        self.config = config or {}
        self.secrets = secrets or {}
        # Only meaningful to connectors that pool/cache a live session
        # (SqlAlchemyDatabaseConnector); accepted here too so every
        # registered connector class has one constructor signature — see
        # connectors/registry.py get_connector().
        self._cache_key = cache_key

    @abc.abstractmethod
    def test_connection(self) -> ConnectionTestResult:
        """
        Synchronous by design — every real driver used here (psycopg2,
        PyMySQL, pymssql, ...) is a blocking C extension with no async
        variant. Callers (connection_service) must run this via
        asyncio.to_thread() rather than await it directly.
        """
        raise NotImplementedError

    # ── Schema/object browsing (Source Dataset node, Phase 2) ─────────
    # All synchronous, same asyncio.to_thread() calling convention as
    # test_connection(). Default implementations are "not supported" —
    # SqlAlchemyDatabaseConnector overrides all of these generically via
    # SQLAlchemy reflection; a connector that can't really do this (no
    # live driver, or a non-relational source) should NOT override them
    # to return empty/fake results.

    def list_schemas(self) -> list[str]:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support schema browsing.")

    def list_objects(self, schema: str | None, search: str | None = None) -> list[ObjectInfo]:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support object browsing.")

    def get_columns(self, object_name: str, schema: str | None = None) -> list[ColumnInfo]:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support column introspection.")

    def row_count(self, object_name: str, schema: str | None = None) -> int | None:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support row counts.")

    def preview_object(self, object_name: str, schema: str | None, limit: int) -> PreviewResult:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support data preview.")

    def preview_sql(self, sql: str, parameters: dict, limit: int) -> PreviewResult:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support custom SQL preview.")

    # ── SQL Workspace (Phase 9) ────────────────────────────────────────
    # Deliberately more permissive than preview_sql above: a real SQL
    # Workspace runs whatever single statement the user writes — reads
    # AND writes — against a connection they're already authorized to
    # use, the same as any native SQL client would. See
    # connectors/database/base.py for the real implementation.

    # Real per-dialect capability — False means "not wired for this
    # connection type yet", never silently ignored (see
    # connectors/database/base.py's supported PostgreSQL/MySQL overrides
    # and cancel_session()/apply_statement_timeout() below).
    supports_query_timeout: bool = False
    supports_query_cancel: bool = False

    def run_sql(
        self, sql: str, parameters: dict, limit: int, *,
        timeout_seconds: int | None = None, on_session_ready=None,
    ) -> SqlRunResult:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support SQL Workspace execution.")

    def cancel_session(self, session_id: str) -> None:
        """Issues a REAL cancel against a running statement's session_id
        (the value on_session_ready received during run_sql) using a
        short-lived, separate connection — never touches the original
        in-flight connection object. Raises ConnectorUnsupportedError for
        any connector that hasn't got real cancellation wired (see
        supports_query_cancel) rather than pretending to succeed."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support query cancellation yet.")

    def explain(self, sql: str, parameters: dict) -> ExplainResult:
        """Real EXPLAIN via the dialect's own syntax — never a guessed or
        synthesized plan. Only connectors with a genuinely verified
        EXPLAIN syntax override this (see connectors/database/base.py);
        every other connector raises rather than fabricating a plan."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support Explain Query yet.")

    # ── Streaming extraction (Source Dataset execution, Phase 3) ──────

    def key_range(self, object_name: str, schema: str | None, key_column: str) -> tuple[object, object]:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support key-range partitioning.")

    def read_key_range_chunk(
        self, object_name: str, schema: str | None, key_column: str,
        after_value: object, upper_bound: object, chunk_size: int,
    ) -> ChunkResult:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support chunked extraction.")

    def open_sql_stream(
        self, sql: str, parameters: dict, *, extraction_mode: str | None = None,
        log_fn=None, cancel_check=None,
    ) -> SqlStream:
        # log_fn/cancel_check let the interleaved streaming fast path
        # (pipeline_job_executor._execute_streaming_chain) surface a
        # connector's own internal per-page progress and let a connector
        # whose single fetch_chunk() call can span several of ITS OWN
        # remote pages (e.g. Salesforce Bulk API) notice a mid-fetch
        # cancellation — see SalesforceConnector.open_sql_stream. Accepted
        # here (and in every subclass override) purely for interface
        # compatibility with that one generic caller, which always passes
        # them as keywords: real bug found live 2026-09-16 — the generic
        # SQL-dialect override and NetSuite SuiteQL's override didn't
        # accept these two kwargs, so `TypeError: open_sql_stream() got an
        # unexpected keyword argument 'log_fn'` killed the streaming fast
        # path for EVERY non-Salesforce source the instant it started
        # (falling through to the older, capped, full-buffering engine
        # instead) — exactly the kind of silent, connector-specific
        # regression the extraction_mode compatibility parameter above was
        # already added once to prevent.
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support streaming SQL extraction.")

    def build_select_all_sql(self, object_name: str, schema: str | None = None) -> str:
        """Returns a real, dialect-correct "read everything" SELECT for
        object_name — the statement execution/pipeline_job_executor.py's
        _run_source() hands to open_sql_stream() for a genuinely
        exhaustive, chunked read of a plain TABLE/object source (as
        opposed to a hand-written customSql source, which already has its
        own SELECT text). Deliberately separate from open_sql_stream()
        itself: building the statement text is dialect-specific (SQL
        needs identifier quoting via quote_object(); SOQL has no `SELECT
        *` and needs an explicit field list — see
        connectors/application/salesforce.py's override), while streaming
        the result is not. Raises ConnectorUnsupportedError for any
        connector that doesn't implement it — callers fall back to the
        older bounded preview_object() path for those, same as an
        unimplemented open_sql_stream() itself already does."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support building a SELECT-all statement.")

    # ── REST API extraction (API-category Source Dataset node) ────────

    def fetch_api(
        self, *, endpoint: str, method: str = "GET", query_params: dict | None = None,
        headers: dict | None = None, body: dict | None = None, json_path: str | None = None,
        pagination: dict | None = None, limit: int = 1000, timeout: float | None = None,
        retry_max: int = 2, rate_limit_delay: float = 0.0,
    ) -> PreviewResult:
        """Real, paginated HTTP fetch against one endpoint on this
        connection's base URL — see connectors/api/rest.py for the only
        real implementation (RestApiConnector). `pagination` describes how
        to walk multiple pages (offset/page/cursor/token style — see that
        module's docstring); `json_path` extracts the record list out of
        an enveloped response (e.g. "$.data.customers")."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support API data fetching.")

    # ── File operations (File/SFTP source, Phase 5) ───────────────────

    def list_files(self, pattern: str | None = None, path: str | None = None) -> list[FileInfo]:
        """`path`, when given, scopes the listing to a sub-folder/prefix
        under this connection's own configured root (base_path/share/
        container/bucket) — e.g. a DocumentScheduleConfig's source_path,
        letting several schedules share one Connection while each reading
        its own subfolder. None/'' means the connection's root itself,
        the pre-existing behavior every caller that doesn't pass it keeps
        unchanged."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support file listing.")

    def open_read_stream(self, file_path: str):
        """Returns a binary, file-like object supporting .read(size) and
        .close() — never the whole file loaded into memory. Duck-typed
        (paramiko's SFTPFile and Python's built-in file object both
        satisfy this) rather than a bespoke wrapper class."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support streaming file reads.")

    def move_file(self, file_path: str, dest_dir: str) -> str:
        """Moves a file into an archive/error subdirectory after
        processing — returns the new path. Never called until the
        outcome (success or failure) it's recording is already final."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support moving files.")

    def file_checksum(self, file_path: str) -> str:
        """Streamed SHA-256 — reads the file in bounded chunks through the
        hash, never loads it whole, even for a checksum."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support file checksums.")

    def write_file(self, file_path: str, data: bytes) -> int:
        """Real upload of a complete in-memory file to this connection —
        the write-side counterpart to open_read_stream() above. Returns
        the number of bytes written. See connectors/sftp/client.py for the
        only real implementation (SftpConnector); a Local File target
        writes straight to disk in execution/pipeline_job_executor.py
        without needing this method at all."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support file uploads.")

    # ── REST API loading (API-category Output Dataset target) ─────────

    def send_records(
        self, *, endpoint: str, method: str = "POST", query_params: dict | None = None,
        headers: dict | None = None, records: list[dict], records_key: str | None = None,
        batch_size: int = 500, timeout: float | None = None, retry_max: int = 2,
    ) -> dict:
        """Real, batched HTTP write of `records` to one endpoint on this
        connection's base URL — the write-side counterpart to fetch_api()
        above. See connectors/api/rest.py for the only real implementation
        (RestApiConnector). Every batch is a genuine HTTP request with the
        same retry-on-5xx behavior fetch_api() uses; a REST API has no
        cross-batch transaction, so a failure partway through means
        earlier batches already landed on the remote system — the raised
        error says how many records got through before the failure,
        never silently rolled back or retried past retry_max."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support API data writes.")

    # ── AI generation (AI Provider connections, Document Intelligence
    #    AI Extraction) ─────────────────────────────────────────────

    def generate(self, prompt: str, *, system: str | None = None, max_tokens: int = 2000) -> str:
        """Real, synchronous call to the connector's configured LLM —
        returns the raw text completion. Callers run this via
        asyncio.to_thread(), same convention as test_connection(). Only
        connectors/ai/* override this; every other connector keeps the
        default "not supported" behavior rather than faking a response."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support AI generation.")

    def supports_vision(self) -> bool:
        """True if this connection's CONFIGURED MODEL genuinely accepts
        image input for generate_vision() below — checked per-connector
        against that vendor's real list of vision-capable model names/
        prefixes, never assumed from the connection type alone (e.g. an
        Ollama connection running a text-only model does not support
        vision even though other Ollama models do). Default False; see
        services/document_service.py's OCR-failure vision fallback for
        the one real caller."""
        return False

    def generate_vision(
        self, prompt: str, *, image_b64: str, mime_type: str, system: str | None = None, max_tokens: int = 2000,
    ) -> str:
        """Real vision-model call — sends the actual image bytes (base64)
        alongside the prompt, for documents Tesseract genuinely couldn't
        read (see ocr/engine.py + document_service.py's vision-fallback
        OCR path). Raises ConnectorUnsupportedError when supports_vision()
        is False rather than silently falling back to a text-only call
        that would just hallucinate over an empty prompt."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support vision input.")

    # ── Cloud OCR (OCR Provider connections, Documents module) ────────
    # Only connectors/ocr/* override this — a dedicated document-OCR API
    # call (AWS Textract / Google Vision / Azure Document Intelligence),
    # NOT the same thing as generate_vision() above (a general-purpose
    # vision LLM asked to transcribe text as a fallback when Tesseract
    # finds nothing). See app/ocr/engine.py's run_ocr() for how the
    # organization's configured ocr_provider setting selects between
    # Tesseract (default, no connection needed) and one of these.

    def ocr(self, image_bytes: bytes, *, mime_type: str = "image/png") -> dict:
        """Real, synchronous call to the connector's OCR API for ONE page
        image. Callers run this via asyncio.to_thread(). Returns:
        {"full_text": str, "confidence": float | None,
         "regions": [{"text", "bbox": [x,y,w,h], "confidence"}]} — same
        per-page shape ocr/engine.py's Tesseract path produces (minus
        "page", which the caller stamps on afterward), so document_service
        can store either provider's result identically. confidence is
        None when the vendor's API genuinely doesn't return one — never
        fabricated. Only connectors/ocr/* override this."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support OCR.")

    # ── ERP invoice posting (Invoice Intelligence, Oracle ERP Cloud) ──
    # Only connectors/application/oracle_erp.py overrides these — a
    # dedicated Accounts Payable Invoices REST integration. Modeled after
    # the generate()/ocr() extension pattern above rather than a second,
    # parallel adapter hierarchy: an ERP connection IS a Connection like
    # any other (Test Connection, encrypted secrets, connector registry),
    # it just has ERP-shaped verbs instead of tabular ones. Idempotency
    # across retries is enforced at OUR layer (invoice_erp_service.py /
    # ApInvoiceErpTransaction.idempotency_key — see INV9), never assumed
    # to be guaranteed by the ERP platform itself; idempotency_key is
    # still passed through here so a genuinely idempotency-aware ERP
    # endpoint can also de-dupe on its own side.

    def submit_invoice(self, payload: dict, *, idempotency_key: str | None = None) -> dict:
        """Real POST of one AP invoice (header + lines) to the ERP's
        documented invoice-creation resource. Returns the ERP's own
        response body verbatim (includes its assigned invoice id/number
        and any links) — never a synthesized "success" shape. Raises
        ConnectorError with the real HTTP status/body on rejection."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support ERP invoice submission.")

    def get_invoice_status(self, erp_invoice_id: str) -> dict:
        """Real GET of one ERP-side invoice's current status fields
        (validation/approval/payment status, in the ERP's own field
        names) — used by reconciliation and status polling."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support ERP invoice status lookup.")

    def cancel_invoice(self, erp_invoice_id: str) -> dict:
        """Real cancel/void call against the ERP's documented custom
        action for this resource. Raises ConnectorError if the ERP
        refuses (e.g. already paid) rather than reporting a fake success."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support ERP invoice cancellation.")

    def health_check(self) -> ConnectionTestResult:
        """Lighter-weight liveness probe than test_connection() — same
        shape, but hits the ERP's own service-health/ping-style endpoint
        (when the ERP documents one) instead of the invoices resource
        itself. Default implementation just delegates to test_connection()
        so any connector may leave this unoverridden."""
        return self.test_connection()

    # ── Accounting integration (TallyPrime today; the same surface a
    # future SAP/Oracle/NetSuite *accounting* connector would implement) ──
    # Modeled after the ERP invoice-posting extension immediately above:
    # an accounting connection IS a Connection like any other (Test
    # Connection, encrypted secrets, connector registry) — it just has
    # accounting-shaped verbs instead of tabular ones. Every method
    # returns THIS codebase's own normalized shape (plain dicts/lists of
    # dicts with documented keys); a connector must parse/reshape its
    # vendor's raw wire format (Tally's XML, a future SAP/Oracle's JSON,
    # ...) internally and never hand raw vendor structures to a caller —
    # this is what keeps the reconciliation engine and canonical-model
    # builders genuinely source-agnostic (see reconciliation/engine.py's
    # own RowSource abstraction, the same principle one layer down).
    # Only connectors/application/tallyprime.py overrides these today.

    def list_companies(self) -> list[dict]:
        """Real list of companies/organizations available on this
        accounting instance: [{"name": str, "guid": str | None,
        "is_default": bool}, ...]. Never assume a single/default company —
        callers must let the user pick when more than one is returned."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support company discovery.")

    def get_company(self, name: str) -> dict:
        """Validates one specific company by name against this instance's
        real company list and returns its detail dict. Raises
        ConnectorError (not a silent empty dict) if it doesn't exist —
        callers must never guess or default a company identity."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support company lookup.")

    def get_vouchers(self, *, company: str, voucher_type: str | None = None, from_date=None,
                      to_date=None, party: str | None = None) -> dict:
        """Real voucher extraction for one company, filtered by type/date
        range/party. Returns {"vouchers": [...normalized rows...]}.

        No cursor/limit params: TallyPrime's own Collection-export TDL
        protocol has no server-side pagination — a Collection request
        returns its entire filtered result set in one response (this was
        assumed otherwise in an earlier draft of this interface and
        corrected once the real TDL protocol was implemented). A caller
        needing bounded-size extraction over a large date span must chunk
        by from_date/to_date itself and issue multiple calls — see
        services/tally_extraction_service.py, which does exactly that,
        bounded by settings.tally_max_concurrent_requests."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support voucher extraction.")

    def get_purchase_vouchers(self, *, company: str, **kwargs) -> dict:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support purchase voucher extraction.")

    def get_sales_vouchers(self, *, company: str, **kwargs) -> dict:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support sales voucher extraction.")

    def get_party_master(self, *, company: str, party_type: str | None = None, **kwargs) -> dict:
        """Supplier AND customer master data through one verb —
        party_type filters to "SUPPLIER"/"CUSTOMER" when the accounting
        system distinguishes them, or returns every party ledger when
        None. Deliberately not two separate get_supplier_master()/
        get_customer_master() methods: which parties are suppliers vs.
        customers is itself accounting-system-specific (Tally has no
        rigid party-type flag — it's inferred from the ledger's parent
        group), so forcing a two-method split here would push a Tally-
        specific assumption up into the generic connector contract."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support party master extraction.")

    def get_ledger(self, *, company: str, party: str, from_date=None, to_date=None) -> dict:
        """One party's real ledger entries (opening balance, every
        posting, closing balance) for the given period."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support ledger extraction.")

    def get_payments(self, *, company: str, from_date=None, to_date=None, party: str | None = None) -> dict:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support payment extraction.")

    def get_outstanding(self, *, company: str, party: str | None = None) -> dict:
        """Real outstanding/receivable-payable balances, as of now or as
        of a point in time the accounting system supports."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support outstanding-balance extraction.")

    def get_bill_allocations(self, *, company: str, party: str | None = None, **kwargs) -> dict:
        """Bill-wise payment allocation detail, where the accounting
        system exposes it (e.g. Tally's bill-wise details) — which
        specific invoice(s) a given payment was applied against."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support bill-wise allocation extraction.")

    def create_voucher(self, voucher: dict, *, company: str, idempotency_key: str | None = None) -> dict:
        """Real voucher creation. Only ever called after this codebase's
        own validation/duplicate-check/human-approval gate has passed —
        see services/tally/tally_write_service.py. Returns the
        accounting system's own response verbatim (never a synthesized
        success shape); raises ConnectorError on rejection."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support voucher creation.")

    def validate_voucher(self, voucher: dict, *, company: str) -> dict:
        """A dry-run/check call against the accounting system's own
        validation, where it exposes one, distinct from this codebase's
        own pre-write validation — both run, neither substitutes for the
        other."""
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support voucher validation.")

    def cancel_voucher(self, voucher_id: str, *, company: str) -> dict:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support voucher cancellation.")

    def _scrub(self, message: str) -> str:
        """Instance-method convenience wrapper around the module-level
        scrub_secrets() — see its docstring for the full rationale."""
        return scrub_secrets(message, self.secrets)
