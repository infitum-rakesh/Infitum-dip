"""
scd.py — real SCD Type 1 / Type 2 merge logic for database targets, used
by execution/pipeline_job_executor.py's target node when loadType is
'delta'. Both strategies operate through the same connector/writer pair
every other database write in this codebase already uses (connectors/
database/base.py's pooled engine, loading/writers/*'s dialect-aware
quoting) — no separate merge dialect needed.

SCD1 (overwrite in place, no history): delegates the actual write to
BulkWriter.write_batch(key_columns=...) — Postgres' ON CONFLICT, MySQL's
ON DUPLICATE KEY UPDATE and SQL Server's MERGE (see loading/writers/*.py)
already implement a real, vendor-native upsert; this module's job is
just (a) making sure a real unique constraint exists on the key columns
so those statements have something to conflict against, and (b) change
detection — skipping the write entirely for rows that haven't actually
changed, per the row-hash comparison below.

SCD2 (versioned history): no single SQL statement does this on any of
the three dialects this codebase supports, so it's hand-rolled as one
real transaction: close the current version of every changed key (set
effective_end_date + is_current=false), then insert a fresh current
version for every new-or-changed key. Both statements run on the same
connection inside one engine.begin() block — a crash between them can
never leave two "current" rows (or zero) for the same business key.

Change detection (both strategies): a deterministic SHA-256 hash over
the tracked non-key columns, NULL-normalized and stringified so type
formatting differences (e.g. 42 vs "42") don't manufacture a spurious
"change". Existing target rows are read back (bounded to the same
SOURCE_ROW_CAP the rest of this engine already enforces — see module
docstring on pipeline_job_executor.py) and compared in pandas; only rows
whose hash differs, or whose key is new, are ever written.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import Boolean, DateTime, bindparam, inspect, text

FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
_SELECT_CHUNK = 1000   # keys per SELECT ... WHERE key IN (...) round trip
_WRITE_CHUNK = 500     # rows per UPDATE/INSERT statement


class ScdError(Exception):
    """Raised for real, disclosed SCD configuration/data problems — never
    silently degrades to a different (wrong) load behavior."""


def _normalize(value) -> str:
    if value is None:
        return "\x00NULL\x00"
    if isinstance(value, float) and pd.isna(value):
        return "\x00NULL\x00"
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def row_hash(row: dict, columns: list[str]) -> str:
    """Deterministic hash over `columns` in the given order — callers
    must pass the SAME column order for both source and target sides or
    the comparison is meaningless (see _hash_series below, which always
    sorts columns once and reuses that order for both)."""
    joined = "\x1f".join(_normalize(row.get(c)) for c in columns)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _hash_series(df: pd.DataFrame, columns: list[str]) -> pd.Series:
    return df.apply(lambda r: row_hash(r.to_dict(), columns), axis=1)


def _key_tuple(row: dict, key_columns: list[str]) -> tuple:
    return tuple(row.get(k) for k in key_columns)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _fetch_existing(connector, schema, table, key_columns, change_columns, keys, *, extra_columns=None):
    """Reads back existing target rows matching the given key tuples,
    chunked so a large key set never becomes one enormous IN clause.
    Returns a DataFrame with key_columns + change_columns (+ extra_columns
    if given, e.g. SCD2's current-flag), or an empty DataFrame with those
    columns if the table has no matching rows / doesn't exist yet."""
    engine = connector.engine(pool_size=2)
    insp = inspect(engine)
    if not insp.has_table(table, schema=schema):
        return pd.DataFrame(columns=[*key_columns, *change_columns, *(extra_columns or [])])

    select_cols = list(dict.fromkeys([*key_columns, *change_columns, *(extra_columns or [])]))
    quoted_table = connector.quote_object(table, schema)
    quoted_select = ", ".join(connector.quote_identifier(c) for c in select_cols)

    frames = []
    for chunk in _chunks(keys, _SELECT_CHUNK):
        if len(key_columns) == 1:
            k = key_columns[0]
            stmt = text(f"SELECT {quoted_select} FROM {quoted_table} WHERE {connector.quote_identifier(k)} IN :vals") \
                .bindparams(bindparam("vals", expanding=True))
            params = {"vals": [t[0] for t in chunk]}
        else:
            # Composite keys: SQL Server has no portable row-value IN, so
            # build a chunked OR-of-ANDs instead — works identically on
            # every dialect this codebase supports.
            or_clauses = []
            params = {}
            for i, t in enumerate(chunk):
                and_parts = []
                for j, k in enumerate(key_columns):
                    pname = f"k{i}_{j}"
                    and_parts.append(f"{connector.quote_identifier(k)} = :{pname}")
                    params[pname] = t[j]
                or_clauses.append("(" + " AND ".join(and_parts) + ")")
            stmt = text(f"SELECT {quoted_select} FROM {quoted_table} WHERE " + " OR ".join(or_clauses))
        with engine.connect() as cxn:
            result = cxn.execute(stmt, params)
            rows = [dict(r._mapping) for r in result]
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        return pd.DataFrame(columns=select_cols)
    return pd.concat(frames, ignore_index=True)


def scd1_merge(connector, writer, schema, table, key_columns, df, change_columns=None) -> dict:
    """Real SCD Type 1: rows with an unchanged hash are skipped entirely;
    changed/new rows go through writer.write_batch(key_columns=...) —
    a genuine vendor-native upsert, not an emulated read-modify-write."""
    if not key_columns:
        raise ScdError("SCD Type 1 requires at least one business key column.")
    missing_keys = [k for k in key_columns if k not in df.columns]
    if missing_keys:
        raise ScdError(f"Business key column(s) not present in the pipeline output: {', '.join(missing_keys)}.")

    change_cols = change_columns or [c for c in df.columns if c not in key_columns]
    change_cols = [c for c in change_cols if c not in key_columns]  # keys are never "changed attributes"
    if not change_cols:
        raise ScdError("No non-key columns to compare — every column is part of the business key.")

    writer.ensure_table(schema, table, df, key_columns=key_columns)
    writer.ensure_unique_key(schema, table, key_columns)

    keys = [_key_tuple(r, key_columns) for r in df.to_dict(orient="records")]
    existing = _fetch_existing(connector, schema, table, key_columns, change_cols, keys)

    df = df.copy()
    df["_src_hash"] = _hash_series(df, change_cols)
    if not existing.empty:
        existing = existing.copy()
        existing["_tgt_hash"] = _hash_series(existing, change_cols)
        existing_map = {
            _key_tuple(r, key_columns): r["_tgt_hash"]
            for r in existing.to_dict(orient="records")
        }
    else:
        existing_map = {}

    def _is_changed(row) -> bool:
        k = _key_tuple(row, key_columns)
        return existing_map.get(k) != row["_src_hash"]

    changed_mask = df.apply(_is_changed, axis=1)
    to_write = df[changed_mask].drop(columns=["_src_hash"])
    inserted = int(sum(1 for r in to_write.to_dict(orient="records") if _key_tuple(r, key_columns) not in existing_map))
    updated = len(to_write) - inserted
    unchanged = len(df) - len(to_write)

    written = 0
    if len(to_write):
        columns = list(to_write.columns)
        rows = to_write.to_dict(orient="records")
        for chunk in _chunks(rows, 5000):
            written += writer.write_batch(schema, table, columns, chunk, key_columns=key_columns)

    return {"inserted": inserted, "updated": updated, "unchanged": unchanged, "written": written}


def ensure_scd2_columns(connector, schema, table, start_col, end_col, current_col) -> list[str]:
    """Idempotent: adds any of the three SCD2 metadata columns that are
    missing from an already-existing table (via real reflection, not an
    assumption). A freshly-created table gets them as part of its
    initial DDL instead — see scd2_merge(). Column types are rendered via
    the connector's own SQLAlchemy dialect (dialect.type_compiler), not a
    hardcoded string — SQL Server has no BOOLEAN type (it's BIT) and no
    TIMESTAMP type in the SQL-standard sense, so a literal type name
    would silently be wrong there."""
    engine = connector.engine(pool_size=2)
    insp = inspect(engine)
    existing_cols = {c["name"] for c in insp.get_columns(table, schema=schema)}
    datetime_ddl = engine.dialect.type_compiler.process(DateTime())
    boolean_ddl = engine.dialect.type_compiler.process(Boolean())
    added = []
    quoted_table = connector.quote_object(table, schema)
    with engine.begin() as cxn:
        for name, ddl_type in ((start_col, datetime_ddl), (end_col, datetime_ddl), (current_col, boolean_ddl)):
            if name in existing_cols:
                continue
            quoted_col = connector.quote_identifier(name)
            cxn.execute(text(f"ALTER TABLE {quoted_table} ADD {quoted_col} {ddl_type}"))
            added.append(name)
    return added


def scd2_merge(
    connector, writer, schema, table, key_columns, df, *,
    change_columns=None, start_col="effective_start_date", end_col="effective_end_date", current_col="is_current",
) -> dict:
    """Real SCD Type 2: closes the currently-active version of every
    changed business key and inserts a fresh current version, inside one
    transaction — see module docstring for why both steps share a single
    engine.begin() block."""
    if not key_columns:
        raise ScdError("SCD Type 2 requires at least one business key column.")
    missing_keys = [k for k in key_columns if k not in df.columns]
    if missing_keys:
        raise ScdError(f"Business key column(s) not present in the pipeline output: {', '.join(missing_keys)}.")

    change_cols = change_columns or [c for c in df.columns if c not in key_columns]
    change_cols = [c for c in change_cols if c not in key_columns]
    if not change_cols:
        raise ScdError("No non-key columns to compare — every column is part of the business key.")

    engine = connector.engine(pool_size=2)
    insp = inspect(engine)
    table_existed = insp.has_table(table, schema=schema)

    if not table_existed:
        # Fresh table: include the SCD2 metadata columns in the initial
        # DDL, and prime start/end/current with real values matching the
        # very first version of every row (never left null).
        scaffold = df.copy()
        scaffold[start_col] = pd.Timestamp(datetime.now(timezone.utc))
        scaffold[end_col] = pd.Timestamp(FAR_FUTURE)
        scaffold[current_col] = True
        writer.ensure_table(schema, table, scaffold, key_columns=None)
    else:
        # An existing SCD2 table is NOT unique on key_columns (it holds
        # history — many rows can share a business key) so there's no
        # ensure_unique_key() call here, unlike SCD1's upsert path.
        ensure_scd2_columns(connector, schema, table, start_col, end_col, current_col)

    keys = [_key_tuple(r, key_columns) for r in df.to_dict(orient="records")]
    existing = _fetch_existing(
        connector, schema, table, key_columns, change_cols, keys, extra_columns=[current_col],
    )
    if not existing.empty:
        existing = existing[existing[current_col].astype(bool)].copy()
        existing["_tgt_hash"] = _hash_series(existing, change_cols) if len(existing) else []
        existing_map = {_key_tuple(r, key_columns): r["_tgt_hash"] for r in existing.to_dict(orient="records")}
    else:
        existing_map = {}

    df = df.copy()
    df["_src_hash"] = _hash_series(df, change_cols)
    df["_key"] = df.apply(lambda r: _key_tuple(r, key_columns), axis=1)
    # Keys whose current version needs closing: present in both the
    # existing (current) target rows and the incoming source, with a
    # different hash. A key only in existing_map (dropped from the
    # source feed) is left untouched — this engine doesn't propagate
    # deletes; a key only in the source is handled by to_insert below.
    src_hash_by_key = dict(zip(df["_key"], df["_src_hash"]))
    to_close_keys = [k for k, h in existing_map.items() if k in src_hash_by_key and src_hash_by_key[k] != h]
    changed_or_new_mask = df.apply(lambda r: existing_map.get(r["_key"]) != r["_src_hash"], axis=1)
    to_insert = df[changed_or_new_mask].drop(columns=["_src_hash", "_key"])
    unchanged = len(df) - len(to_insert)
    new_count = int(sum(1 for k in df.loc[changed_or_new_mask, "_key"] if k not in existing_map))
    changed_count = len(to_insert) - new_count

    now = datetime.now(timezone.utc)
    quoted_table = connector.quote_object(table, schema)
    quoted_end = connector.quote_identifier(end_col)
    quoted_current = connector.quote_identifier(current_col)

    with engine.begin() as cxn:
        for chunk in _chunks(to_close_keys, _WRITE_CHUNK):
            if not chunk:
                continue
            # Bound params for the boolean values (not literal TRUE/FALSE
            # in the SQL text) — SQL Server's T-SQL has no boolean literal
            # syntax at all (BIT columns compare against 0/1), and letting
            # SQLAlchemy's DBAPI layer render the value per-dialect is the
            # only way this stays portable across all three targets.
            or_clauses, params = [], {"now": now, "cur_true": True, "cur_false": False}
            for i, k in enumerate(chunk):
                and_parts = []
                for j, kc in enumerate(key_columns):
                    pname = f"k{i}_{j}"
                    and_parts.append(f"{connector.quote_identifier(kc)} = :{pname}")
                    params[pname] = k[j]
                or_clauses.append("(" + " AND ".join(and_parts) + ")")
            sql = (
                f"UPDATE {quoted_table} SET {quoted_end} = :now, {quoted_current} = :cur_false "
                f"WHERE {quoted_current} = :cur_true AND (" + " OR ".join(or_clauses) + ")"
            )
            cxn.execute(text(sql), params)

        if len(to_insert):
            to_insert = to_insert.copy()
            to_insert[start_col] = now
            to_insert[end_col] = FAR_FUTURE
            to_insert[current_col] = True
            columns = list(to_insert.columns)
            quoted_cols = ", ".join(connector.quote_identifier(c) for c in columns)
            rows = to_insert.to_dict(orient="records")
            for chunk in _chunks(rows, _WRITE_CHUNK):
                values_sql = ", ".join(
                    "(" + ", ".join(f":{c}_{i}" for c in columns) + ")" for i in range(len(chunk))
                )
                params = {f"{c}_{i}": row.get(c) for i, row in enumerate(chunk) for c in columns}
                cxn.execute(text(f"INSERT INTO {quoted_table} ({quoted_cols}) VALUES {values_sql}"), params)

    return {"new": new_count, "changed": changed_count, "unchanged": unchanged, "closed": len(to_close_keys)}
