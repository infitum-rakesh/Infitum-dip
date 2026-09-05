"""
SqlAlchemyDatabaseConnector — shared test/pool/introspection logic for
every relational database connector that talks through a SQLAlchemy
dialect. Concrete connectors (postgres.py, mysql.py, sqlserver.py) only
supply the dialect driver string, how to build a connection URL from a
Connection's config/secrets, and which built-in schemas to hide from the
browser — pooling, timing, error handling and all of schema/object/column
browsing + preview live here once via SQLAlchemy's generic `inspect()`
reflection API, which works identically across every dialect this
codebase supports.
"""
from __future__ import annotations

import time

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.connectors.base import (
    BaseConnector,
    ChunkResult,
    ColumnInfo,
    ConnectionTestResult,
    ConnectorError,
    ConnectorUnsupportedError,
    ConnectorValidationError,
    ExplainResult,
    ObjectInfo,
    PreviewResult,
    SqlRunResult,
    SqlStream,
)
from app.connectors.database.pool_manager import get_pooled_engine

_ALLOWED_PREVIEW_PREFIXES = ("select", "with")


class SqlAlchemyDatabaseConnector(BaseConnector):
    default_port: int | None = None
    version_query: str = "SELECT 1"
    # Explain Query support — None means "not verified for this dialect
    # yet, don't guess" (explain() then raises rather than fabricating a
    # plan). Set on the dialect subclass along with explain_mode:
    # "text" for a single line-oriented plan column (e.g. Postgres'
    # EXPLAIN), "table" for a genuine multi-column result (e.g. MySQL's).
    explain_prefix: str | None = None
    explain_mode: str = "table"

    def __init__(self, config: dict, secrets: dict, cache_key: str | None = None):
        super().__init__(config, secrets)
        # Must be the saved Connection's own id (see pool_manager.py
        # docstring for why field-derived keys are unsafe: two different
        # rows can share host/port/db/user with different passwords).
        # Falling back to a field-derived key only covers callers that
        # genuinely have no Connection row yet (none exist today).
        self._cache_key = cache_key or self._fallback_pool_key()

    def build_url(self):
        """Returns a sqlalchemy.engine.URL — never a pre-rendered string,
        so a plaintext password never exists as a standalone string that
        could be accidentally logged."""
        raise NotImplementedError

    def connect_args(self) -> dict:
        return {}

    def engine(self, pool_size: int = 5, pool_timeout: int = 10) -> Engine:
        return get_pooled_engine(
            cache_key=self._cache_key,
            url=self.build_url(),
            connect_args=self.connect_args(),
            pool_size=pool_size,
            pool_timeout=pool_timeout,
        )

    def _fallback_pool_key(self) -> str:
        return "fields:" + "|".join([
            self.connection_type,
            str(self.config.get("host") or self.config.get("account") or self.config.get("sql_endpoint") or ""),
            str(self.config.get("port") or ""),
            str(self.config.get("database") or self.config.get("service_name") or ""),
            str(self.config.get("username") or ""),
        ])

    def test_connection(self) -> ConnectionTestResult:
        start = time.monotonic()
        try:
            engine = self.engine(pool_size=1)
            with engine.connect() as conn:
                row = conn.execute(text(self.version_query)).first()
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            version = str(row[0]).splitlines()[0][:120] if row and row[0] is not None else None
            return ConnectionTestResult(success=True, message="Connection successful.", latency_ms=latency_ms, server_version=version)
        except SQLAlchemyError as exc:
            return ConnectionTestResult(success=False, message=self._scrub(_root_cause_message(exc)), latency_ms=round((time.monotonic() - start) * 1000, 1))
        except Exception as exc:  # driver raised something SQLAlchemy didn't wrap (e.g. a raw socket error at connect time)
            return ConnectionTestResult(success=False, message=self._scrub(str(exc)), latency_ms=round((time.monotonic() - start) * 1000, 1))

    # ── Schema/object browsing ──────────────────────────────────────

    def excluded_schemas(self) -> set[str]:
        """System/internal schemas hidden from the browser — override per
        dialect (see postgres.py/mysql.py/sqlserver.py)."""
        return set()

    def list_schemas(self) -> list[str]:
        try:
            insp = inspect(self.engine(pool_size=1))
            try:
                names = insp.get_schema_names()
            except NotImplementedError:
                return []
        except SQLAlchemyError as exc:
            raise ConnectorError(self._scrub(f"Could not list schemas: {_root_cause_message(exc)}")) from exc
        except ConnectorError:
            raise
        except Exception as exc:  # driver raised something SQLAlchemy didn't wrap (e.g. a raw socket/ODBC error)
            raise ConnectorError(self._scrub(f"Could not list schemas: {exc}")) from exc
        excluded = self.excluded_schemas()
        return sorted(n for n in names if n not in excluded)

    def list_objects(self, schema: str | None, search: str | None = None) -> list[ObjectInfo]:
        try:
            insp = inspect(self.engine(pool_size=1))
            tables = insp.get_table_names(schema=schema)
            try:
                views = insp.get_view_names(schema=schema)
            except NotImplementedError:
                views = []
        except SQLAlchemyError as exc:
            raise ConnectorError(self._scrub(f"Could not list tables/views: {_root_cause_message(exc)}")) from exc
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Could not list tables/views: {exc}")) from exc
        objects = [ObjectInfo(name=t, schema=schema, object_type="TABLE") for t in tables]
        objects += [ObjectInfo(name=v, schema=schema, object_type="VIEW") for v in views]
        if search:
            needle = search.lower()
            objects = [o for o in objects if needle in o.name.lower()]
        objects.sort(key=lambda o: (o.object_type, o.name.lower()))
        return objects

    def get_columns(self, object_name: str, schema: str | None = None) -> list[ColumnInfo]:
        try:
            insp = inspect(self.engine(pool_size=1))
            cols = insp.get_columns(object_name, schema=schema)
        except SQLAlchemyError as exc:
            raise ConnectorError(self._scrub(f"Could not read columns for {object_name}: {_root_cause_message(exc)}")) from exc
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Could not read columns for {object_name}: {exc}")) from exc

        pk_cols: set[str] = set()
        try:
            pk_cols = set(insp.get_pk_constraint(object_name, schema=schema).get("constrained_columns") or [])
        except Exception:
            pass

        unique_cols: set[str] = set()
        try:
            for uc in insp.get_unique_constraints(object_name, schema=schema):
                unique_cols.update(c for c in (uc.get("column_names") or []) if c)
        except Exception:
            pass

        indexed_cols: set[str] = set()
        try:
            for idx in insp.get_indexes(object_name, schema=schema):
                idx_cols = [c for c in (idx.get("column_names") or []) if c]
                indexed_cols.update(idx_cols)
                if idx.get("unique"):
                    unique_cols.update(idx_cols)
        except Exception:
            pass

        # Real foreign-key reflection (dialect-native, via SQLAlchemy's own
        # inspector) — powers the SQL Workspace database explorer's
        # relationship view. A column can only be one FK's local column at
        # a time in practice (composite FKs list multiple constrained_
        # columns; each is mapped to its corresponding referred_column by
        # position — real per SQLAlchemy's own documented dict shape).
        fk_by_col: dict[str, dict] = {}
        try:
            for fk in insp.get_foreign_keys(object_name, schema=schema):
                local_cols = fk.get("constrained_columns") or []
                ref_cols = fk.get("referred_columns") or []
                ref_table = fk.get("referred_table")
                ref_schema = fk.get("referred_schema")
                for i, local_col in enumerate(local_cols):
                    ref_col = ref_cols[i] if i < len(ref_cols) else (ref_cols[0] if ref_cols else None)
                    if ref_table and ref_col:
                        fk_by_col[local_col] = {"table": ref_table, "column": ref_col, "schema": ref_schema}
        except Exception:
            pass

        return [
            ColumnInfo(
                name=c["name"],
                data_type=str(c["type"]),
                nullable=bool(c.get("nullable", True)),
                primary_key=c["name"] in pk_cols,
                unique=c["name"] in unique_cols,
                indexed=c["name"] in indexed_cols,
                default=str(c["default"]) if c.get("default") is not None else None,
                foreign_key=fk_by_col.get(c["name"]),
                # SQLAlchemy's own reflected String/VARCHAR types carry a
                # real .length; anything else (INTEGER, unbounded TEXT, ...)
                # has none — genuine metadata, not a guess.
                max_length=getattr(c["type"], "length", None),
            )
            for c in cols
        ]

    def row_count(self, object_name: str, schema: str | None = None) -> int | None:
        """
        Exact COUNT(*) — fine for interactive browsing during design time,
        NOT how this platform reads row counts once ingestion runs at
        scale (a 100M-row COUNT(*) is exactly the kind of full-table scan
        the streaming/partitioned-read architecture in later phases is
        built to avoid). Returns None rather than raising if the count
        can't be produced, so a slow/locked count degrades the preview UI
        instead of breaking it.
        """
        engine = self.engine(pool_size=1)
        quoted = self._quote_object(object_name, schema, engine)
        try:
            with engine.connect() as conn:
                row = conn.execute(text(f"SELECT COUNT(*) FROM {quoted}")).first()
            return int(row[0]) if row is not None else None
        except SQLAlchemyError:
            return None

    def preview_object(self, object_name: str, schema: str | None, limit: int) -> PreviewResult:
        try:
            engine = self.engine(pool_size=1)
            quoted = self._quote_object(object_name, schema, engine)
            with engine.connect() as conn:
                result = conn.execute(text(f"SELECT * FROM {quoted}"))
                columns = list(result.keys())
                rows = result.fetchmany(limit + 1)  # +1 to detect truncation without a second COUNT(*)
        except SQLAlchemyError as exc:
            raise ConnectorError(self._scrub(f"Preview of {object_name} failed: {_root_cause_message(exc)}")) from exc
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(self._scrub(f"Preview of {object_name} failed: {exc}")) from exc
        return PreviewResult(columns=columns, rows=[list(r) for r in rows[:limit]], row_count=None, truncated=len(rows) > limit)

    def preview_sql(self, sql: str, parameters: dict, limit: int) -> PreviewResult:
        """
        Runs user-supplied SQL for preview only. Bind VALUES are always
        parameterized via SQLAlchemy's text().bindparams()-style execute —
        never string-concatenated (see api/connections.py request schema:
        `parameters` is a dict of named binds, e.g. `:last_run_date`).
        A statement-type guard blocks anything but SELECT/WITH — this is a
        preview surface, not a general SQL console, and the resulting rows
        are still bounded by `limit` via fetchmany() rather than a LIMIT
        clause rewrite (which would need per-dialect syntax).
        """
        stripped = (sql or "").strip()
        if not stripped:
            raise ConnectorValidationError("SQL text is empty.")
        first_word = stripped.split(None, 1)[0].lower()
        if first_word not in _ALLOWED_PREVIEW_PREFIXES:
            raise ConnectorValidationError("Only SELECT / WITH statements are allowed for preview.")

        engine = self.engine(pool_size=1)
        with engine.connect() as conn:
            result = conn.execute(text(stripped), parameters or {})
            columns = list(result.keys())
            rows = result.fetchmany(limit + 1)
        return PreviewResult(columns=columns, rows=[list(r) for r in rows[:limit]], row_count=None, truncated=len(rows) > limit)

    def run_sql(
        self, sql: str, parameters: dict, limit: int, *,
        timeout_seconds: int | None = None, on_session_ready=None,
    ) -> SqlRunResult:
        """
        Real SQL Workspace execution (Phase 9) — deliberately broader
        than preview_sql above: runs ONE statement of any kind (read or
        write) that the connection's own DB user is authorized to run,
        the same as any native SQL client would. Always runs inside
        engine.begin() — a real transaction that commits only if the
        statement succeeds and rolls back automatically on any
        exception, never left half-applied. (MySQL's own DDL is
        inherently non-transactional — CREATE/ALTER/DROP there commit
        immediately regardless of this wrapper; that's a real MySQL
        server characteristic, not something this code fakes around.)

        Whether the statement produced a result set (SELECT/WITH/SHOW/
        EXPLAIN/a row-returning stored-procedure call/...) is asked of
        the driver itself via CursorResult.returns_rows rather than
        guessed from the first keyword — correct for every statement
        shape, not just the ones on some hand-maintained prefix list.

        Only ONE statement per call — this does not attempt to split a
        `;`-separated script (a naive split would break on semicolons
        inside string literals/comments); a disclosed limitation, not a
        silent truncation. Same known `:name`-as-bind-parameter caveat
        as preview_sql for literal colons in the SQL text (e.g. a
        Postgres `::type` cast) — pre-existing, not new here.

        timeout_seconds and on_session_ready (History/Export/Security
        phase — query timeout + cancellation): on_session_ready, if
        given, is called with this connection's REAL session/backend
        identifier (see get_session_id()) as soon as it's known — BEFORE
        the real statement runs — so the caller can register it for
        cancellation while this call is still blocking. timeout_seconds
        applies a REAL server-side cutoff via apply_statement_timeout()
        only when supports_query_timeout is True for this dialect; a
        no-op default elsewhere, never a fabricated client-side timer
        that could let a write keep running after the caller thinks it
        was cut off.
        """
        stripped = (sql or "").strip().rstrip(";").strip()
        if not stripped:
            raise ConnectorValidationError("SQL text is empty.")

        engine = self.engine(pool_size=2)
        with engine.begin() as conn:
            if on_session_ready:
                on_session_ready(self.get_session_id(conn))
            if timeout_seconds and self.supports_query_timeout:
                self.apply_statement_timeout(conn, timeout_seconds)
            result = conn.execute(text(stripped), parameters or {})
            if result.returns_rows:
                columns = list(result.keys())
                rows = result.fetchmany(limit + 1)
                return SqlRunResult(is_result_set=True, columns=columns, rows=[list(r) for r in rows[:limit]], truncated=len(rows) > limit)
            rowcount = result.rowcount if (result.rowcount is not None and result.rowcount >= 0) else None
            return SqlRunResult(is_result_set=False, rowcount=rowcount)

    def get_session_id(self, conn) -> str | None:
        """Real DB-session/process identifier for `conn`, used by
        cancel_session() from a SEPARATE connection. None (the default)
        means this dialect doesn't have real cancellation wired — the
        run proceeds without being cancellable, never fakes an id."""
        return None

    def apply_statement_timeout(self, conn, seconds: int) -> None:
        """Sets a REAL server-side statement timeout on `conn` before the
        real statement runs. Only called when supports_query_timeout is
        True; the default here is an intentional no-op so a dialect that
        hasn't overridden it can never silently claim to honor a timeout
        it doesn't actually enforce."""
        return

    def cancel_session(self, session_id: str) -> None:
        raise ConnectorUnsupportedError(f"{self.connection_type} does not support query cancellation yet.")

    def explain(self, sql: str, parameters: dict) -> ExplainResult:
        """Real EXPLAIN, never a fabricated plan — see explain_prefix/
        explain_mode class attributes above. Plain EXPLAIN (not EXPLAIN
        ANALYZE) deliberately: ANALYZE actually executes the statement to
        gather real timings, which is unsafe for a write statement (an
        INSERT/UPDATE/DELETE would really run) — this only ever estimates,
        the same safety boundary preview_sql() draws by only allowing
        SELECT/WITH there, applied differently here since EXPLAIN itself
        is dialect-syntax-checked to accept the statement type."""
        if self.explain_prefix is None:
            raise ConnectorUnsupportedError(f"{self.connection_type} does not support Explain Query yet.")
        stripped = (sql or "").strip().rstrip(";").strip()
        if not stripped:
            raise ConnectorValidationError("SQL text is empty.")

        engine = self.engine(pool_size=2)
        with engine.connect() as conn:
            result = conn.execute(text(f"{self.explain_prefix} {stripped}"), parameters or {})
            columns = list(result.keys())
            rows = [list(r) for r in result.fetchall()]

        if self.explain_mode == "text":
            # Single line-oriented plan column (e.g. Postgres) — join into
            # one real plan block rather than a one-column grid.
            plan_text = "\n".join(str(r[0]) for r in rows)
            return ExplainResult(plan_text=plan_text)
        return ExplainResult(columns=columns, rows=rows)

    def _quote_object(self, object_name: str, schema: str | None, engine: Engine) -> str:
        """Identifiers (schema/table names from the browse dropdowns, not
        user-typed free SQL) can't be bind-parameterized — this is the
        correct way to safely quote them: SQLAlchemy's dialect-aware
        identifier preparer, not manual string concatenation."""
        preparer = engine.dialect.identifier_preparer
        if schema:
            return f"{preparer.quote(schema)}.{preparer.quote(object_name)}"
        return preparer.quote(object_name)

    # Public wrappers around the two quoting helpers above — used by
    # mapping/mapper.py's SQL builder (Phase 4), which needs dialect-aware
    # quoting but shouldn't reach into a leading-underscore method or hold
    # its own Engine reference.
    def quote_object(self, object_name: str, schema: str | None = None) -> str:
        return self._quote_object(object_name, schema, self.engine(pool_size=1))

    def quote_identifier(self, name: str) -> str:
        return self.engine(pool_size=1).dialect.identifier_preparer.quote(name)

    def truncate_object(self, object_name: str, schema: str | None = None) -> None:
        """TRUNCATE TABLE — identical syntax across Postgres/MySQL/SQL
        Server, so one shared implementation covers every registered
        database connector; used by the TRUNCATE_INSERT target-load write
        mode (Phase 6) before the first batch is written, never per-batch."""
        engine = self.engine(pool_size=2)
        quoted = self._quote_object(object_name, schema, engine)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {quoted}"))

    # ── Streaming extraction (Source Dataset execution, Phase 3) ──────
    # See execution/worker.py for how these two paths are used: key-range
    # for TABLE/VIEW (partitionable, checkpointed, resumable) and
    # open_sql_stream for arbitrary custom SQL (single stream, no natural
    # resume point). Neither ever issues OFFSET — see module docstring.

    def key_range(self, object_name: str, schema: str | None, key_column: str) -> tuple[object, object]:
        """MIN/MAX of key_column — the only full-table scan this path ever
        does, and only once per run (at partition-planning time), not once
        per chunk. For a 100M-row table this is still one index range scan,
        not a table scan, as long as key_column is indexed (true for any
        reasonable primary key)."""
        engine = self.engine(pool_size=1)
        quoted_obj = self._quote_object(object_name, schema, engine)
        quoted_col = engine.dialect.identifier_preparer.quote(key_column)
        with engine.connect() as conn:
            row = conn.execute(text(f"SELECT MIN({quoted_col}), MAX({quoted_col}) FROM {quoted_obj}")).first()
        return (row[0], row[1]) if row is not None else (None, None)

    def read_key_range_chunk(
        self, object_name: str, schema: str | None, key_column: str,
        after_value: object, upper_bound: object, chunk_size: int,
    ) -> ChunkResult:
        """
        The core streaming primitive: WHERE key > :after [AND key <= :upper]
        ORDER BY key LIMIT :chunk_size — a single indexed range seek plus a
        bounded fetch, never an OFFSET (which would force the database to
        walk and discard every prior row on every single chunk — the exact
        thing spec #43 rules out for 100M+ tables). after_value=None means
        "from the start of this partition's range" (no lower-bound clause).
        """
        # pool_size here only matters the first time this Connection's engine
        # is created (see pool_manager.get_pooled_engine — later calls reuse
        # the cached engine as-is); sized for several concurrent partition
        # workers sharing one Connection's pool.
        engine = self.engine(pool_size=8)
        quoted_obj = self._quote_object(object_name, schema, engine)
        quoted_col = engine.dialect.identifier_preparer.quote(key_column)

        clauses = []
        params: dict = {"chunk_size": chunk_size}
        if after_value is not None:
            clauses.append(f"{quoted_col} > :after_value")
            params["after_value"] = after_value
        if upper_bound is not None:
            clauses.append(f"{quoted_col} <= :upper_bound")
            params["upper_bound"] = upper_bound
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        sql = f"SELECT * FROM {quoted_obj} {where} ORDER BY {quoted_col} LIMIT :chunk_size"
        with engine.connect() as conn:
            result = conn.execute(text(sql), params)
            columns = list(result.keys())
            rows = result.fetchall()

        if not rows:
            return ChunkResult(rows=[], last_key=None, count=0)
        key_idx = columns.index(key_column)
        dict_rows = [dict(zip(columns, r)) for r in rows]
        return ChunkResult(rows=dict_rows, last_key=rows[-1][key_idx], count=len(rows))

    def build_select_all_sql(self, object_name: str, schema: str | None = None) -> str:
        return f"SELECT * FROM {self.quote_object(object_name, schema)}"

    def open_sql_stream(self, sql: str, parameters: dict, *, extraction_mode: str | None = None) -> SqlStream:
        # extraction_mode (BULK/AUTO/ROW_BY_ROW) is a Salesforce-only
        # concept (see SalesforceConnector.open_sql_stream) — every SQL
        # dialect connector already streams a real server-side cursor via
        # SQLAlchemy regardless of this setting, so it's accepted for
        # interface compatibility with the generic caller in
        # pipeline_job_executor.py and simply ignored here.
        stripped = (sql or "").strip()
        if not stripped:
            raise ConnectorValidationError("SQL text is empty.")
        first_word = stripped.split(None, 1)[0].lower()
        if first_word not in _ALLOWED_PREVIEW_PREFIXES:
            raise ConnectorValidationError("Only SELECT / WITH statements are allowed.")
        engine = self.engine(pool_size=2)
        conn = engine.connect()
        try:
            result = conn.execute(text(stripped), parameters or {})
        except Exception:
            conn.close()
            raise
        return _SqlAlchemySqlStream(conn, result)


class _SqlAlchemySqlStream(SqlStream):
    """One checked-out connection + open result cursor, fetched in bounded
    chunks via fetchmany() — never the full result set at once."""

    def __init__(self, conn, result):
        self._conn = conn
        self._result = result
        self._columns = list(result.keys())

    @property
    def columns(self) -> list[str]:
        return self._columns

    def fetch_chunk(self, size: int) -> list[dict]:
        rows = self._result.fetchmany(size)
        return [dict(zip(self._columns, r)) for r in rows]

    def close(self) -> None:
        self._conn.close()


def _root_cause_message(exc: Exception) -> str:
    """SQLAlchemy wraps the real DBAPI error as .orig — surface that
    message (e.g. psycopg2's) rather than SQLAlchemy's own wrapper text,
    it's what actually explains what went wrong."""
    orig = getattr(exc, "orig", None)
    return str(orig) if orig is not None else str(exc)
