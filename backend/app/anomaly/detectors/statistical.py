"""
Statistical detectors — Z-score, Modified Z-score (median/MAD), IQR, and
Percentile-based. Each scans a real column's values via
RowSource.iter_rows(), bounded by DEFAULT_SCAN_CAP, and flags individual
values against a `reference_stats` dict (the comparison basis anomaly/
engine.py — added in AD3 — resolves per column: the approved baseline's
stats when one exists, else the current run's own stats, so a job with
no baseline yet still gets same-run outlier detection instead of silently
skipping statistical checks).

Z-score and IQR read their thresholds straight from `reference_stats`
(mean/std_dev, p25/p75) since anomaly/profiler.py already computes those
via a single bounded sample — no need to re-scan for those two. Modified
Z-score and Percentile need a value distribution profiler.py doesn't keep
(MAD; arbitrary percentile cutoffs), so those two take their own single
bounded sample pass before flagging — still capped at DEFAULT_SCAN_CAP,
still never more than one full pass over that capped sample.
"""
from __future__ import annotations

from app.anomaly.detectors.base import AnomalyHit, DEFAULT_SCAN_CAP, coerce_float, resolve_identifier

DEFAULT_PARAMS = {
    "Z_SCORE": {"threshold": 3.0},
    "MODIFIED_Z_SCORE": {"threshold": 3.5},
    "IQR": {"multiplier": 1.5},
    "PERCENTILE": {"lower_percentile": 1.0, "upper_percentile": 99.0},
}


def _iter_numeric(row_source, column: str, id_column: str | None, cap: int):
    """Yields (position, row, float_value) for up to `cap` non-null,
    numeric-coercible values — the shared scan loop every detector below
    reuses instead of duplicating iter_rows()/cap bookkeeping."""
    fetch_cols = [column] if not id_column or id_column == column else [column, id_column]
    position = 0
    for chunk in row_source.iter_rows(fetch_cols, chunk_size=5000):
        for row in chunk:
            position += 1
            fv = coerce_float(row.get(column))
            if fv is not None:
                yield position, row, fv
        if position >= cap:
            return


def detect_zscore(
    row_source, column: str, reference_stats: dict, *,
    id_column: str | None = None, threshold: float = 3.0, cap: int = DEFAULT_SCAN_CAP,
) -> list[AnomalyHit]:
    mean = reference_stats.get("mean")
    std = reference_stats.get("std_dev")
    if mean is None or not std:
        return []  # no usable reference (e.g. all-null column, or std_dev==0) — nothing to compare against
    lower, upper = mean - threshold * std, mean + threshold * std
    hits: list[AnomalyHit] = []
    for position, row, fv in _iter_numeric(row_source, column, id_column, cap):
        z = (fv - mean) / std
        if abs(z) >= threshold:
            ident, is_pos = resolve_identifier(row, id_column, position)
            hits.append(AnomalyHit(
                anomaly_type="Z_SCORE", record_identifier=ident, value=str(fv),
                expected_range=f"[{lower:.6g}, {upper:.6g}]",
                raw_score=abs(z), identifier_is_position=is_pos,
            ))
    return hits


def detect_modified_zscore(
    row_source, column: str, reference_stats: dict, *,
    id_column: str | None = None, threshold: float = 3.5, cap: int = DEFAULT_SCAN_CAP,
) -> list[AnomalyHit]:
    median = reference_stats.get("median")
    if median is None:
        return []
    collected = list(_iter_numeric(row_source, column, id_column, cap))
    if not collected:
        return []
    deviations = sorted(abs(fv - median) for _, _, fv in collected)
    mid = len(deviations) // 2
    mad = deviations[mid] if len(deviations) % 2 else (deviations[mid - 1] + deviations[mid]) / 2
    if mad == 0:
        # Every sampled value equals the median — modified z-score is
        # undefined (division by zero) for any deviation. Rather than
        # flag every non-median value as infinitely anomalous, skip: a
        # near-constant column is better reported via a distribution
        # detector (AD3), not this one.
        return []
    hits: list[AnomalyHit] = []
    for position, row, fv in collected:
        mz = 0.6745 * (fv - median) / mad
        if abs(mz) >= threshold:
            ident, is_pos = resolve_identifier(row, id_column, position)
            span = threshold * mad / 0.6745
            hits.append(AnomalyHit(
                anomaly_type="MODIFIED_Z_SCORE", record_identifier=ident, value=str(fv),
                expected_range=f"[{median - span:.6g}, {median + span:.6g}]",
                raw_score=abs(mz), identifier_is_position=is_pos,
            ))
    return hits


def detect_iqr(
    row_source, column: str, reference_stats: dict, *,
    id_column: str | None = None, multiplier: float = 1.5, cap: int = DEFAULT_SCAN_CAP,
) -> list[AnomalyHit]:
    q1 = reference_stats.get("p25")
    q3 = reference_stats.get("p75")
    if q1 is None or q3 is None:
        return []
    iqr = q3 - q1
    if iqr == 0:
        return []  # degenerate distribution (>=75% of sampled values identical) — see modified-z-score's identical reasoning
    lower, upper = q1 - multiplier * iqr, q3 + multiplier * iqr
    hits: list[AnomalyHit] = []
    for position, row, fv in _iter_numeric(row_source, column, id_column, cap):
        if fv < lower or fv > upper:
            distance = (lower - fv) if fv < lower else (fv - upper)
            ident, is_pos = resolve_identifier(row, id_column, position)
            hits.append(AnomalyHit(
                anomaly_type="IQR", record_identifier=ident, value=str(fv),
                expected_range=f"[{lower:.6g}, {upper:.6g}]",
                raw_score=distance / iqr, identifier_is_position=is_pos,
            ))
    return hits


def detect_percentile(
    row_source, column: str, reference_stats: dict, *,
    id_column: str | None = None, lower_percentile: float = 1.0, upper_percentile: float = 99.0,
    cap: int = DEFAULT_SCAN_CAP,
) -> list[AnomalyHit]:
    """Unlike Z-score/IQR, this recomputes its own sorted sample rather
    than reusing reference_stats' fixed p1/p25/p50/p75/p99 — the whole
    point of this detector is CONFIGURABLE percentile cutoffs (spec: "with
    configurable thresholds"), which profiler.py's fixed percentile set
    can't serve for an arbitrary cutoff like p2/p98."""
    collected = list(_iter_numeric(row_source, column, id_column, cap))
    if len(collected) < 20:
        return []  # too small a sample for a percentile cutoff to be meaningful
    sorted_vals = sorted(fv for _, _, fv in collected)
    n = len(sorted_vals)

    def _pct(p: float) -> float:
        idx = min(n - 1, max(0, round(p / 100 * (n - 1))))
        return sorted_vals[idx]

    lower_bound, upper_bound = _pct(lower_percentile), _pct(upper_percentile)
    span = (upper_bound - lower_bound) or 1.0
    hits: list[AnomalyHit] = []
    for position, row, fv in collected:
        if fv < lower_bound or fv > upper_bound:
            distance = (lower_bound - fv) if fv < lower_bound else (fv - upper_bound)
            ident, is_pos = resolve_identifier(row, id_column, position)
            hits.append(AnomalyHit(
                anomaly_type="PERCENTILE", record_identifier=ident, value=str(fv),
                expected_range=f"[{lower_bound:.6g}, {upper_bound:.6g}] (p{lower_percentile:g}-p{upper_percentile:g})",
                raw_score=abs(distance) / span, identifier_is_position=is_pos,
            ))
    return hits


DETECTORS = {
    "Z_SCORE": detect_zscore,
    "MODIFIED_Z_SCORE": detect_modified_zscore,
    "IQR": detect_iqr,
    "PERCENTILE": detect_percentile,
}
