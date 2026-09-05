"""
BaseBulkWriter — the target-side counterpart to BaseConnector's read
methods (Phase 6). One instance wraps a single target connector for the
duration of a load run.

write_batch() is the only required method: given a resolved
schema/table, a fixed column order, and a batch of dict rows (already
transformed — see mapping/mapper.py's build_preview_sql, which is what
produces these rows via a live SELECT against the source), it writes
that batch to the target and returns how many rows were written. Never
buffers more than one batch — the load worker (execution/load_worker.py)
controls batch size via chunk_size, same bounded-memory principle as the
extraction side.

key_columns, when given, requests an UPSERT (insert-or-update on those
columns) instead of a plain append — real, vendor-native SQL per writer,
not a fetch-then-compare emulation. A writer that hasn't implemented
UPSERT raises ConnectorUnsupportedError rather than silently downgrading
to APPEND (which would duplicate rows) or silently ignoring conflicts.
"""
from __future__ import annotations

import abc
import datetime
import decimal

import pandas as pd
from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, Float, MetaData, String, Table, Text, inspect, text

from app.connectors.base import ColumnInfo, scrub_secrets


def normalize_identifier(name: str) -> str:
    """Lowercases a table/column/key-column name this platform is about to
    CREATE or WRITE TO (never applied to identifiers reflected FROM an
    existing external table — those keep whatever real case the source
    system already gave them).

    Why: SQLAlchemy's DDL compiler (and connectors/database/base.py's own
    quote_identifier()) auto-double-quotes any identifier that isn't
    already all-lowercase — e.g. a NetSuite field 'CustomerID' becomes the
    literal Postgres identifier "CustomerID", which Postgres then treats
    as case-SENSITIVE forever after (unquoted `customerid`/`CUSTOMERID`
    references no longer match it — "relation does not exist"). Real bug,
    found live: 'table and column name should not be case sensitive'.

    Normalizing to lowercase here — the single choke point every table/
    column name this platform creates passes through (ensure_table*,
    ensure_unique_key, write_batch_with_fallback all call this
    independently on the same original name strings, so they agree
    without needing to pass a resolved name back and forth) — means the
    physical identifier is always a plain lowercase name no dialect ever
    needs to quote, so it's reachable with ANY casing a user later types
    (Postgres/MySQL/SQL Server all fold/compare unquoted identifiers
    case-insensitively when nothing was ever quoted to begin with)."""
    return (name or "").strip().lower()


def write_batch_with_fallback(
    writer: "BaseBulkWriter", schema: str | None, table: str, columns: list[str], rows: list[dict],
    key_columns: list[str] | None = None,
) -> tuple[int, list[dict]]:
    """Real bulk-then-row-by-row fallback (KK2). Tries the writer's normal
    write_batch() — the fast bulk path (executemany/execute_values/a real
    array-bind MERGE) — for the WHOLE batch first, exactly like before.
    Only if that fails does it retry ONE ROW AT A TIME, so every genuinely
    good row in the batch still lands instead of the whole batch failing
    because of one bad one (a constraint violation, a type mismatch, a
    truncation) — and each row that still fails on its own comes back with
    its own real database error message (never fabricated), for the
    caller to persist as a RejectedRecord (see app/services/reject_service.py)
    so a user can actually see and fix it.

    Circuit breaker: if EVERY row in a multi-row batch fails individually
    too, that's almost certainly a systemic problem (wrong target schema,
    a missing column, a dead connection) rather than N genuinely bad
    records — re-raises the original batch-level exception in that case
    instead of silently manufacturing N "rejected records" that would
    just be noise, not something a user could meaningfully fix one at a
    time.

    Returns (written_count, rejects) where rejects is
    [{"row": dict, "error": str}, ...] for rows that failed individually."""
    if not rows:
        return 0, []
    # Normalize table/column identifiers exactly as ensure_table*()/
    # ensure_unique_key() below independently do on the same original
    # name strings (see normalize_identifier()'s docstring) — so a batch
    # write always targets the same lowercase physical table/columns the
    # create step just made, regardless of what case the source data's
    # own field names happen to be in.
    norm_table = normalize_identifier(table)
    col_map = {c: normalize_identifier(c) for c in columns}
    norm_columns = [col_map[c] for c in columns]
    norm_key_columns = [col_map.get(k, normalize_identifier(k)) for k in key_columns] if key_columns else key_columns
    rows = [{col_map.get(k, normalize_identifier(k)): v for k, v in row.items()} for row in rows]
    table, columns, key_columns = norm_table, norm_columns, norm_key_columns
    try:
        written = writer.write_batch(schema, table, columns, rows, key_columns)
        return written, []
    except Exception as batch_exc:
        written = 0
        rejects: list[dict] = []
        for row in rows:
            try:
                written += writer.write_batch(schema, table, columns, [row], key_columns)
            except Exception as row_exc:
                rejects.append({"row": row, "error": scrub_secrets(str(row_exc), writer.connector.secrets)})
        if len(rejects) == len(rows) and len(rows) > 1:
            raise batch_exc
        return written, rejects


def _sa_type_for_column_info(col: ColumnInfo):
    """Maps a real SOURCE ColumnInfo (get_columns() — reflected SQL types,
    or Salesforce field describe()) to a properly-sized target column
    type. Used by ensure_table_from_columns() below, the KEY_RANGE Run
    Load path's table-creation step, which streams dict rows straight
    from a chunked reader and never builds a pandas DataFrame at all (so
    _sa_type_for_dtype below, which needs one, doesn't apply there).

    This is the fix for a real bulk-load performance problem: a blanket
    Text() for every non-numeric column (the DataFrame path below, kept
    as-is for its own callers) compiles to CLOB on Oracle — CLOB columns
    are dramatically slower to bulk-insert than a properly sized
    VARCHAR2, can't be indexed/compared normally, and bloat storage. Most
    real source fields (Salesforce Name/Email/Phone/picklists, a SQL
    VARCHAR column, ...) have a real, modest known length; sizing the
    target column from it turns those into genuine indexable VARCHAR2/
    VARCHAR instead. Only a string with no known length, or one longer
    than a real VARCHAR2 can hold (Oracle's un-extended ceiling is 4000
    bytes; comfortably true of every other dialect here too), falls back
    to Text()/CLOB — the same safe type as before, never a truncation."""
    dt = (col.data_type or "").lower()
    if "bool" in dt:
        return Boolean()
    if "point" not in dt and any(t in dt for t in ("int", "long", "smallint", "bigint")):
        return BigInteger()
    if any(t in dt for t in ("double", "float", "decimal", "numeric", "currency", "percent")):
        return Float()
    if "datetime" in dt or "timestamp" in dt:
        return DateTime()
    if dt == "date":
        return Date()
    # Everything else is string-ish (string/varchar/text/picklist/email/
    # phone/id/reference/textarea/...) — size it from the real max_length
    # when we have one small enough for a real VARCHAR2 column.
    if col.max_length and 0 < col.max_length <= 4000:
        return String(col.max_length)
    return Text()


def _sa_type_for_dtype(series: "pd.Series"):
    """Maps a real DataFrame column to a generic SQLAlchemy column type —
    used by ensure_table() below to build a CREATE TABLE from the actual
    data being written, not a fabricated/fixed schema.

    Takes the Series (not just its .dtype) because a numeric source
    column frequently arrives as pandas' catch-all 'object' dtype rather
    than a native int64/float64 one: DB-API drivers commonly return
    NUMERIC/DECIMAL columns as Python decimal.Decimal (psycopg2 for
    Postgres NUMERIC, pyodbc for NetSuite's SuiteQL 'NUMBER' columns,
    cx_Oracle for NUMBER, ...) precisely to avoid float rounding error,
    and a Decimal-valued column's dtype is 'object', not a numeric dtype
    — so the old dtype-only check sent every such column straight to the
    Text()/CLOB fallback below regardless of it being genuinely numeric.
    This is the real root cause behind "NetSuite-derived integer columns
    become CLOB": NetSuite's NUMBER columns arrive as Decimal objects,
    exactly like a Postgres NUMERIC source column would. Sampling the
    actual non-null values (not the dtype) tells int/decimal/date apart
    correctly for that whole class of source, not just NetSuite.

    Text (not a length-bound VARCHAR) is the default for anything not
    confidently numeric/boolean/datetime: it's the one string type valid
    as DDL on every dialect this codebase supports (MySQL requires a
    length on VARCHAR; Postgres/SQL Server don't need one for TEXT), so
    it never truncates real data or fails to compile on any target."""
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return Boolean()
    if pd.api.types.is_integer_dtype(dtype):
        return BigInteger()
    if pd.api.types.is_float_dtype(dtype):
        return Float()
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return DateTime()
    if dtype == object:
        sample = series.dropna()
        if len(sample):
            values = sample.head(200)
            if all(isinstance(v, bool) for v in values):
                return Boolean()
            if all(isinstance(v, (int, decimal.Decimal)) and not isinstance(v, bool) for v in values):
                # Decimal(p, 0) / plain int -> a real integer column;
                # Decimal(p, s>0) -> a real decimal column. Mixed sign of
                # scale across the sampled rows still safely widens to
                # Float rather than truncating any row's fractional part.
                all_whole = all(
                    (v == int(v)) if isinstance(v, decimal.Decimal) else True
                    for v in values
                )
                return BigInteger() if all_whole else Float()
            if all(isinstance(v, float) for v in values):
                return Float()
            if all(isinstance(v, datetime.datetime) for v in values):
                return DateTime()
            if all(isinstance(v, datetime.date) and not isinstance(v, datetime.datetime) for v in values):
                return Date()
    return Text()


class BaseBulkWriter(abc.ABC):
    def __init__(self, connector):
        self.connector = connector

    @abc.abstractmethod
    def write_batch(
        self, schema: str | None, table: str, columns: list[str], rows: list[dict],
        key_columns: list[str] | None = None,
    ) -> int:
        raise NotImplementedError

    def truncate(self, schema: str | None, table: str) -> None:
        """Default: delegate to the connector's shared TRUNCATE TABLE
        (connectors/database/base.py) — every registered database
        connector supports it identically. Override only if a future
        writer's target can't (none currently)."""
        self.connector.truncate_object(normalize_identifier(table), schema)

    def table_exists(self, schema: str | None, table: str) -> bool:
        """Real reflection via SQLAlchemy's inspect() — same mechanism the
        connector's own schema/object browsing already uses (connectors/
        database/base.py), not a guess."""
        engine = self.connector.engine(pool_size=1)
        return inspect(engine).has_table(normalize_identifier(table), schema=schema)

    def ensure_table(
        self, schema: str | None, table: str, df: "pd.DataFrame", *, recreate: bool = False,
        key_columns: list[str] | None = None,
    ) -> str:
        """Creates the target table from the DataFrame's real pandas
        dtypes if it doesn't already exist — generic across every
        SQLAlchemy-backed writer via reflection/DDL, so no per-vendor
        CREATE TABLE string is needed here or in postgres.py/mysql.py/
        sqlserver.py. With recreate=True, drops the existing table first
        (for when the source structure has genuinely changed since the
        table was first created) instead of leaving stale columns behind
        or failing on a column mismatch. Returns 'created' | 'recreated' |
        'exists' so the caller can log what actually happened.

        key_columns, when given, marks those columns PRIMARY KEY on a
        freshly-created table — required for SCD1/upsert loads: Postgres'
        ON CONFLICT, MySQL's ON DUPLICATE KEY UPDATE and SQL Server's
        MERGE...ON all need a real unique constraint on the match columns
        to work at all, not just application-level intent. Ignored when
        the table already exists (see ensure_unique_key() below for that
        case — added separately since altering an existing table needs
        its own real/duplicate-data error handling)."""
        if len(df.columns) == 0:
            raise ValueError(f"Cannot create table '{table}' — the incoming data has no columns.")
        # Normalize to lowercase — see normalize_identifier()'s docstring;
        # a mixed-case source column (e.g. NetSuite's 'CustomerID') would
        # otherwise get auto-quoted into a permanently case-sensitive
        # Postgres identifier. write_batch_with_fallback() normalizes the
        # same way independently, so the two always agree.
        table = normalize_identifier(table)
        key_set = {normalize_identifier(k) for k in (key_columns or [])}
        engine = self.connector.engine(pool_size=2)
        insp = inspect(engine)
        exists = insp.has_table(table, schema=schema)
        if exists and not recreate:
            return "exists"
        if exists and recreate:
            quoted = self.connector.quote_object(table, schema)
            with engine.begin() as cxn:
                cxn.execute(text(f"DROP TABLE {quoted}"))
        metadata = MetaData(schema=schema)
        columns = [
            Column(normalize_identifier(str(c)), _sa_type_for_dtype(df[c]), primary_key=(normalize_identifier(str(c)) in key_set))
            for c in df.columns
        ]
        Table(table, metadata, *columns)
        metadata.create_all(engine)
        return "recreated" if exists else "created"

    def ensure_table_from_columns(
        self, schema: str | None, table: str, columns: list[ColumnInfo], *, recreate: bool = False,
        key_columns: list[str] | None = None, target_column_names: dict[str, str] | None = None,
    ) -> str:
        """Same contract/return values as ensure_table() above, but builds
        the DDL from real SOURCE column metadata (a connector's own
        get_columns()) instead of a pandas DataFrame's dtypes — for the
        KEY_RANGE Run Load path (execution/load_worker.py), which streams
        dict rows straight from a chunked reader (read_key_range_chunk)
        and never materializes a DataFrame. See _sa_type_for_column_info
        above for why this matters: a properly-sized VARCHAR2 instead of
        blanket CLOB for every string column, at real bulk-load scale.

        target_column_names optionally renames each source column (keyed
        by its own name) to its mapped target column name; a source
        column absent from the map keeps its own name."""
        if not columns:
            raise ValueError(f"Cannot create table '{table}' — no source columns given.")
        # Normalize to lowercase — see normalize_identifier()'s docstring
        # and ensure_table() above (same fix, same reasoning). Applied to
        # both the source-side lookup key (c.name) and the resolved
        # target name, so target_column_names entries keyed by the
        # source's original casing still resolve correctly.
        table = normalize_identifier(table)
        key_set = {normalize_identifier(k) for k in (key_columns or [])}
        names = {k: normalize_identifier(v) for k, v in (target_column_names or {}).items()}
        engine = self.connector.engine(pool_size=2)
        insp = inspect(engine)
        exists = insp.has_table(table, schema=schema)
        if exists and not recreate:
            return "exists"
        if exists and recreate:
            quoted = self.connector.quote_object(table, schema)
            with engine.begin() as cxn:
                cxn.execute(text(f"DROP TABLE {quoted}"))
        metadata = MetaData(schema=schema)
        sa_columns = [
            Column(
                names.get(c.name, normalize_identifier(c.name)), _sa_type_for_column_info(c),
                primary_key=(names.get(c.name, normalize_identifier(c.name)) in key_set),
            )
            for c in columns
        ]
        Table(table, metadata, *sa_columns)
        metadata.create_all(engine)
        return "recreated" if exists else "created"

    def ensure_unique_key(self, schema: str | None, table: str, key_columns: list[str]) -> bool:
        """For a table that already existed before this run: checks
        whether key_columns are already covered by a primary key or
        unique constraint/index (via real reflection, not an assumption)
        and adds one if not. Returns True if a constraint was added.
        Raises the real database error (e.g. duplicate values already
        present) rather than swallowing it — an upsert silently running
        without real conflict detection would be worse than failing
        loudly here."""
        table = normalize_identifier(table)
        key_columns = [normalize_identifier(k) for k in key_columns]
        engine = self.connector.engine(pool_size=2)
        insp = inspect(engine)
        key_set = set(key_columns)
        pk_cols = set(insp.get_pk_constraint(table, schema=schema).get("constrained_columns") or [])
        if pk_cols == key_set:
            return False
        for uc in insp.get_unique_constraints(table, schema=schema):
            if set(uc.get("column_names") or []) == key_set:
                return False
        for idx in insp.get_indexes(table, schema=schema):
            if idx.get("unique") and set(idx.get("column_names") or []) == key_set:
                return False
        quoted_table = self.connector.quote_object(table, schema)
        quoted_cols = ", ".join(self.connector.quote_identifier(c) for c in key_columns)
        constraint_name = f"dip_uq_{table}_{'_'.join(key_columns)}"[:63]
        quoted_constraint = self.connector.quote_identifier(constraint_name)
        with engine.begin() as cxn:
            cxn.execute(text(f"ALTER TABLE {quoted_table} ADD CONSTRAINT {quoted_constraint} UNIQUE ({quoted_cols})"))
        return True
