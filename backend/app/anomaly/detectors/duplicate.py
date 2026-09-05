"""
Duplicate-record detection — flags rows whose key (a single column, or a
composite of several — AnomalyJob.detection_config[column]["key_columns"])
repeats a value already seen earlier in the scan. The FIRST occurrence of
a key is never flagged (it's the original, not the duplicate); every
occurrence after it is.

Bounded the same way every other detector is: at most DEFAULT_SCAN_CAP
rows scanned, at most `max_hits` duplicate rows reported as
AnomalyRecord-worthy evidence (a table with a systemically broken key
could have hundreds of thousands of duplicates — reporting all of them
isn't useful, the column-level `duplicate_count`/`duplicate_pct` already
carries the magnitude; see anomaly/engine.py's status rollup, which reads
the count, not the hit list length)."""
from __future__ import annotations

from app.anomaly.detectors.base import AnomalyHit, DEFAULT_SCAN_CAP, resolve_identifier

DEFAULT_PARAMS = {"DUPLICATE": {"key_columns": None, "max_hits": 200}}


def detect_duplicate(
    row_source, column: str, *,
    id_column: str | None = None, key_columns: list[str] | None = None, max_hits: int = 200,
    cap: int = DEFAULT_SCAN_CAP,
) -> list[AnomalyHit]:
    keys = key_columns or [column]
    fetch_cols = list(dict.fromkeys(keys + ([id_column] if id_column else [])))  # de-dupe, preserve order
    seen_keys: dict[tuple, int] = {}
    hits: list[AnomalyHit] = []
    position = 0
    for chunk in row_source.iter_rows(fetch_cols, chunk_size=5000):
        for row in chunk:
            position += 1
            key_values = tuple(row.get(k) for k in keys)
            if any(v is None for v in key_values):
                continue  # a null key isn't a meaningful duplicate — see NULL_RATE for missing-value anomalies
            occurrence = seen_keys.get(key_values, 0) + 1
            seen_keys[key_values] = occurrence
            if occurrence > 1:
                ident, is_pos = resolve_identifier(row, id_column, position)
                hits.append(AnomalyHit(
                    anomaly_type="DUPLICATE", record_identifier=ident,
                    value=", ".join(str(v) for v in key_values),
                    expected_range="unique", raw_score=float(occurrence),
                    identifier_is_position=is_pos,
                ))
                if len(hits) >= max_hits:
                    return hits
        if position >= cap:
            break
    return hits


DETECTORS = {"DUPLICATE": detect_duplicate}
