"""
engine — the four real reconciliation comparison types.

COUNT and CHECKSUM stream in bounded chunks (via a RowSource — either a
real DB connector's own open_sql_stream, the exact same primitive
execution/worker.py's QUERY mode and Phase 6's load_worker.py already use,
or a real LocalFileConnector's open_row_stream for an uploaded file — see
RowSource below) and work at any object size. CHECKSUM combines each row's
SHA-256 hash with XOR — commutative and associative, so row order (which
can differ between two different database engines, or a file whose rows
just aren't in the same order as the DB it's being compared against) never
affects the result; two datasets with the same rows in any order produce
the same checksum, and any real difference in content changes it.

KEY_MATCH and FIELD_LEVEL need a real, correct diff — every source row's
key (and, for FIELD_LEVEL, its compare-column values) has to be
compared against every target row's, which means holding both sides in
memory at once. MAX_RECONCILE_ROWS is the real, disclosed limit that
implies: past it, this refuses outright rather than silently sampling or
truncating, which would report a wrong count and is exactly the kind of
faked correctness this build avoids elsewhere.
"""
from __future__ import annotations

import hashlib

MAX_RECONCILE_ROWS = 200_000


class ReconciliationEngineError(Exception):
    """Bad request, or the comparison genuinely can't be done as asked."""


class RowSource:
    """Abstracts over a real DB connector+object OR a real uploaded file —
    every aggregate primitive below (count/checksum/key-map) is written
    once against this interface and doesn't care which kind of source is
    actually behind it. Two concrete implementations below; nothing else
    in this module ever touches a connector directly."""

    def count(self) -> int:
        raise NotImplementedError

    def get_columns(self) -> list[str]:
        raise NotImplementedError

    def iter_rows(self, columns: list[str], chunk_size: int = 5000):
        """Yields list[dict] chunks — each dict has at least `columns`."""
        raise NotImplementedError

    def describe(self) -> str:
        """Human-readable identity for error messages, e.g. "'orders'" or
        "uploaded file 'orders.csv'"."""
        raise NotImplementedError


class ConnectorRowSource(RowSource):
    """A real DB (or SFTP/API/etc.) connection + schema + object — reads
    via real SQL, exactly as this module always has."""

    def __init__(self, connector, schema: str | None, object_name: str):
        self.connector = connector
        self.schema = schema
        self.object_name = object_name

    def count(self) -> int:
        base = self.connector.quote_object(self.object_name, self.schema)
        result = self.connector.preview_sql(f"SELECT COUNT(*) AS n FROM {base}", {}, 1)
        return int(result.rows[0][0] or 0)

    def get_columns(self) -> list[str]:
        return [c.name for c in self.connector.get_columns(self.object_name, self.schema)]

    def iter_rows(self, columns: list[str], chunk_size: int = 5000):
        quoted_cols = ", ".join(self.connector.quote_identifier(c) for c in columns)
        base = self.connector.quote_object(self.object_name, self.schema)
        stream = self.connector.open_sql_stream(f"SELECT {quoted_cols} FROM {base}", {})
        try:
            while True:
                rows = stream.fetch_chunk(chunk_size)
                if not rows:
                    break
                yield rows
        finally:
            stream.close()

    def describe(self) -> str:
        return f"'{self.object_name}'"


class FileRowSource(RowSource):
    """A real uploaded file, read via a real LocalFileConnector pointed at
    that upload's own sandboxed directory (services/uploaded_file_service.py
    builds the connector) — the same connectors/files/ streaming readers
    SFTP/Local pipeline sources already use. File connectors don't support
    SQL SELECT lists, so column projection happens in Python here instead
    of in the query, unlike ConnectorRowSource."""

    def __init__(self, connector, object_name: str):
        self.connector = connector
        self.object_name = object_name

    def count(self) -> int:
        n = 0
        stream = self.connector.open_row_stream(self.object_name)
        try:
            while True:
                rows = stream.fetch_chunk(5000)
                if not rows:
                    break
                n += len(rows)
        finally:
            stream.close()
        return n

    def get_columns(self) -> list[str]:
        return [c.name for c in self.connector.get_columns(self.object_name)]

    def iter_rows(self, columns: list[str], chunk_size: int = 5000):
        stream = self.connector.open_row_stream(self.object_name)
        try:
            while True:
                rows = stream.fetch_chunk(chunk_size)
                if not rows:
                    break
                yield [{c: r.get(c) for c in columns} for r in rows]
        finally:
            stream.close()

    def describe(self) -> str:
        return f"uploaded file '{self.object_name}'"


def compare_counts(source: RowSource) -> int:
    return source.count()


def _row_hash(values) -> int:
    parts = ["\x00NULL\x00" if v is None else str(v) for v in values]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8", errors="replace")).digest()
    return int.from_bytes(digest[:8], "big")


def compute_checksum(source: RowSource, columns: list[str], chunk_size: int = 5000) -> tuple[str, int]:
    """Returns (hex_checksum, row_count). Order-independent, bounded
    memory regardless of object size — never holds more than one chunk."""
    if not columns:
        raise ReconciliationEngineError("Checksum comparison needs at least one column.")
    acc = 0
    count = 0
    for rows in source.iter_rows(columns, chunk_size):
        for row in rows:
            acc ^= _row_hash(tuple(row.get(c) for c in columns))
            count += 1
    return format(acc, "016x"), count


def _fetch_key_value_map(source: RowSource, key_columns: list[str], compare_columns: list[str], chunk_size: int = 5000) -> dict:
    cols = list(key_columns) + [c for c in compare_columns if c not in key_columns]
    result: dict = {}
    for rows in source.iter_rows(cols, chunk_size):
        for row in rows:
            key = tuple(row.get(c) for c in key_columns)
            result[key] = tuple(row.get(c) for c in compare_columns)
            if len(result) > MAX_RECONCILE_ROWS:
                raise ReconciliationEngineError(
                    f"{source.describe()} has more than {MAX_RECONCILE_ROWS:,} rows — key/field-level "
                    "reconciliation isn't supported at this scale yet (Record Count and Checksum "
                    "comparisons still work at any scale)."
                )
    return result


def _normalize(v) -> str | None:
    return None if v is None else str(v).strip()


def missing_columns(source: RowSource, columns: list[str]) -> list[str]:
    """Which of `columns` don't actually exist on this source — a real
    describe/reflection call, not a guess. Comparing two differently-shaped
    objects (a common real case — e.g. a legacy table vs its replacement,
    or an uploaded file vs a DB table) means a picked key/compare column
    can exist on one side and not the other; without this check that only
    surfaces as an opaque 'column does not exist' error from whichever
    side happens to be read second."""
    if not columns:
        return []
    existing = set(source.get_columns())
    return [c for c in columns if c not in existing]


def require_matching_columns(source: RowSource, source_columns: list[str], target: RowSource, target_columns: list[str]) -> None:
    """Raises a clear, specific ReconciliationEngineError naming exactly
    which column(s) are missing from which side, instead of letting a
    mismatched-schema comparison fail later with a raw driver error.
    `source_columns`/`target_columns` are already resolved through any
    column mapping — positionally aligned (same logical field at the same
    index), not necessarily the same names."""
    missing_source = missing_columns(source, source_columns)
    missing_target = missing_columns(target, target_columns)
    if not missing_source and not missing_target:
        return
    parts = []
    if missing_source:
        parts.append(f"source {source.describe()} is missing column(s): {', '.join(missing_source)}")
    if missing_target:
        parts.append(f"target {target.describe()} is missing column(s): {', '.join(missing_target)}")
    raise ReconciliationEngineError(
        "Column mismatch — " + "; ".join(parts) + ". Reconciliation requires the same column "
        "name(s) on both sides, or an explicit Column Mapping in Setup & Configuration — update "
        "Key Column(s) / Columns to Compare / Column Mapping."
    )


def diff_keys_and_fields(
    source: RowSource, target: RowSource,
    key_columns: list[str], compare_columns: list[str],
    target_key_columns: list[str] | None = None, target_compare_columns: list[str] | None = None,
) -> dict:
    """Returns {source_count, target_count, matched_count, missing_keys,
    extra_keys (both sets of key-tuples), mismatches (list of
    {key, column, source_value, target_value})}.

    `target_key_columns`/`target_compare_columns` default to the source
    names (the "no mapping picked" case) but let the target side read
    different real column names for the same logical fields — a mapped
    column and its source counterpart line up by position, not by name,
    so the returned key-tuples and mismatch `column` labels (always the
    SOURCE name) stay directly comparable either way."""
    if not key_columns:
        raise ReconciliationEngineError("Key/field-level comparison needs at least one key column.")
    target_key_columns = target_key_columns if target_key_columns is not None else key_columns
    target_compare_columns = target_compare_columns if target_compare_columns is not None else compare_columns

    source_map = _fetch_key_value_map(source, key_columns, compare_columns)
    target_map = _fetch_key_value_map(target, target_key_columns, target_compare_columns)
    source_keys, target_keys = set(source_map), set(target_map)
    missing_keys = source_keys - target_keys
    extra_keys = target_keys - source_keys
    matched_keys = source_keys & target_keys

    mismatches = []
    for k in matched_keys:
        sv, tv = source_map[k], target_map[k]
        for i, col in enumerate(compare_columns):
            if _normalize(sv[i]) != _normalize(tv[i]):
                mismatches.append({"key": k, "column": col, "source_value": sv[i], "target_value": tv[i]})

    return {
        "source_count": len(source_keys), "target_count": len(target_keys),
        "matched_count": len(matched_keys), "missing_keys": missing_keys, "extra_keys": extra_keys,
        "mismatches": mismatches,
    }
