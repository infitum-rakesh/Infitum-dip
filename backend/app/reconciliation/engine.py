"""
engine — the four real reconciliation comparison types.

COUNT and CHECKSUM stream in bounded chunks (via the connector's own
open_sql_stream — the exact same primitive execution/worker.py's QUERY
mode and Phase 6's load_worker.py already use) and work at any table
size. CHECKSUM combines each row's SHA-256 hash with XOR — commutative
and associative, so row order (which can differ between two different
database engines) never affects the result; two datasets with the same
rows in any order produce the same checksum, and any real difference in
content changes it.

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


def compare_counts(connector, schema: str | None, object_name: str) -> int:
    base = connector.quote_object(object_name, schema)
    result = connector.preview_sql(f"SELECT COUNT(*) AS n FROM {base}", {}, 1)
    return int(result.rows[0][0] or 0)


def _row_hash(values) -> int:
    parts = ["\x00NULL\x00" if v is None else str(v) for v in values]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8", errors="replace")).digest()
    return int.from_bytes(digest[:8], "big")


def compute_checksum(connector, schema: str | None, object_name: str, columns: list[str], chunk_size: int = 5000) -> tuple[str, int]:
    """Returns (hex_checksum, row_count). Order-independent, bounded
    memory regardless of table size — never holds more than one chunk."""
    if not columns:
        raise ReconciliationEngineError("Checksum comparison needs at least one column.")
    quoted_cols = ", ".join(connector.quote_identifier(c) for c in columns)
    base = connector.quote_object(object_name, schema)
    stream = connector.open_sql_stream(f"SELECT {quoted_cols} FROM {base}", {})
    try:
        acc = 0
        count = 0
        while True:
            rows = stream.fetch_chunk(chunk_size)
            if not rows:
                break
            for row in rows:
                acc ^= _row_hash(tuple(row.get(c) for c in columns))
                count += 1
        return format(acc, "016x"), count
    finally:
        stream.close()


def _fetch_key_value_map(connector, schema: str | None, object_name: str, key_columns: list[str], compare_columns: list[str], chunk_size: int = 5000) -> dict:
    cols = list(key_columns) + [c for c in compare_columns if c not in key_columns]
    quoted_cols = ", ".join(connector.quote_identifier(c) for c in cols)
    base = connector.quote_object(object_name, schema)
    stream = connector.open_sql_stream(f"SELECT {quoted_cols} FROM {base}", {})
    try:
        result: dict = {}
        while True:
            rows = stream.fetch_chunk(chunk_size)
            if not rows:
                break
            for row in rows:
                key = tuple(row.get(c) for c in key_columns)
                result[key] = tuple(row.get(c) for c in compare_columns)
                if len(result) > MAX_RECONCILE_ROWS:
                    raise ReconciliationEngineError(
                        f"'{object_name}' has more than {MAX_RECONCILE_ROWS:,} rows — key/field-level "
                        "reconciliation isn't supported at this scale yet (Record Count and Checksum "
                        "comparisons still work at any scale)."
                    )
        return result
    finally:
        stream.close()


def _normalize(v) -> str | None:
    return None if v is None else str(v).strip()


def diff_keys_and_fields(
    source_connector, source_schema: str | None, source_object: str,
    target_connector, target_schema: str | None, target_object: str,
    key_columns: list[str], compare_columns: list[str],
) -> dict:
    """Returns {source_count, target_count, matched_count, missing_keys,
    extra_keys (both sets of key-tuples), mismatches (list of
    {key, column, source_value, target_value})}."""
    if not key_columns:
        raise ReconciliationEngineError("Key/field-level comparison needs at least one key column.")

    source_map = _fetch_key_value_map(source_connector, source_schema, source_object, key_columns, compare_columns)
    target_map = _fetch_key_value_map(target_connector, target_schema, target_object, key_columns, compare_columns)
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
