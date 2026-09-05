"""
Null/missing-value anomaly detection — flags a COLUMN-LEVEL spike in
null rate versus baseline (e.g. a source system started dropping a
field), not individual null values themselves (a null cell has no
"expected range" to report per-record the way a numeric outlier does).

detect_null_rate() returns at most one AnomalyHit — a spike either
happened this run or it didn't — but still enumerates up to `cap` actual
null rows (by id_column, when configured) as AnomalyRecord-worthy
evidence so a user isn't left with just a percentage.
"""
from __future__ import annotations

from app.anomaly.detectors.base import AnomalyHit, DEFAULT_SCAN_CAP, resolve_identifier

DEFAULT_PARAMS = {"NULL_RATE": {"threshold_pct_points": 5.0, "min_ratio": 1.5}}


def _collect_null_rows(row_source, column: str, id_column: str | None, cap: int) -> list[tuple[int, dict]]:
    fetch_cols = [column] if not id_column or id_column == column else [column, id_column]
    out: list[tuple[int, dict]] = []
    position = 0
    for chunk in row_source.iter_rows(fetch_cols, chunk_size=5000):
        for row in chunk:
            position += 1
            if row.get(column) is None:
                out.append((position, row))
                if len(out) >= cap:
                    return out
    return out


def detect_null_rate(
    row_source, column: str, current_stats: dict, baseline_stats: dict | None, *,
    id_column: str | None = None, threshold_pct_points: float = 5.0, min_ratio: float = 1.5,
    cap: int = DEFAULT_SCAN_CAP,
) -> list[AnomalyHit]:
    """Flags a null-rate spike vs baseline. Without a baseline (first run,
    or a job with baseline_config disabled) there's nothing to compare
    against — an absolute null rate isn't itself an anomaly (many
    legitimate columns are sparsely populated by design), so this
    correctly returns [] rather than guessing a universal "too many
    nulls" threshold."""
    current_pct = current_stats.get("null_pct")
    if current_pct is None or not baseline_stats:
        return []
    baseline_pct = baseline_stats.get("null_pct")
    if baseline_pct is None:
        return []

    delta = current_pct - baseline_pct
    ratio = (current_pct / baseline_pct) if baseline_pct > 0 else (float("inf") if current_pct > 0 else 1.0)
    spiked = delta >= threshold_pct_points or (baseline_pct > 0 and ratio >= min_ratio)
    if not spiked:
        return []

    null_rows = _collect_null_rows(row_source, column, id_column, cap)
    evidence_ident = None
    if null_rows:
        first_pos, first_row = null_rows[0]
        evidence_ident, _ = resolve_identifier(first_row, id_column, first_pos)

    return [AnomalyHit(
        anomaly_type="NULL_RATE",
        record_identifier=evidence_ident,
        value=f"{current_pct:.2f}% null ({len(null_rows)} of first {cap} rows scanned)",
        expected_range=f"~{baseline_pct:.2f}% null (baseline)",
        raw_score=ratio if ratio != float("inf") else 999.0,
        identifier_is_position=not id_column,
    )]


DETECTORS = {"NULL_RATE": detect_null_rate}
