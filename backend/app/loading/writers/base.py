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

import pandas as pd
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, MetaData, Table, Text, inspect, text


def _sa_type_for_dtype(dtype):
    """Maps a pandas dtype to a generic SQLAlchemy column type — used by
    ensure_table() below to build a CREATE TABLE from the real DataFrame
    being written, not a fabricated/fixed schema. Text (not a length-
    bound VARCHAR) is the default for anything not obviously numeric/
    boolean/datetime: it's the one string type that's valid DDL on every
    dialect this codebase supports (MySQL requires a length on VARCHAR;
    Postgres/SQL Server don't need one for TEXT), so it never truncates
    real data or fails to compile depending on target vendor."""
    if pd.api.types.is_bool_dtype(dtype):
        return Boolean()
    if pd.api.types.is_integer_dtype(dtype):
        return BigInteger()
    if pd.api.types.is_float_dtype(dtype):
        return Float()
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return DateTime()
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
        self.connector.truncate_object(table, schema)

    def table_exists(self, schema: str | None, table: str) -> bool:
        """Real reflection via SQLAlchemy's inspect() — same mechanism the
        connector's own schema/object browsing already uses (connectors/
        database/base.py), not a guess."""
        engine = self.connector.engine(pool_size=1)
        return inspect(engine).has_table(table, schema=schema)

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
        key_set = set(key_columns or [])
        columns = [
            Column(str(c), _sa_type_for_dtype(df[c].dtype), primary_key=(str(c) in key_set))
            for c in df.columns
        ]
        Table(table, metadata, *columns)
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
