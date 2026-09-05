"""
Distribution-shift detection — Population Stability Index (PSI) and
Jensen-Shannon (JS) divergence between a baseline distribution and the
current run's, for both numeric and categorical columns. Column-level
(one or two hits per column, never per-record — a distribution shift
isn't a property of any single value).

CATEGORICAL columns compare `category_counts` directly (see
anomaly/profiler.py's _tier2_string_sample — the category frequency
table IS the distribution).

NUMERIC columns reuse the baseline's own p1/p25/p50/p75/p99 (already
computed by anomaly/profiler.py, no extra profiling pass needed) as
FIXED bin edges — by definition of percentiles, the baseline population
always splits into those 6 bins at proportions ~[1%, 24%, 25%, 25%,
24%, 1%] regardless of the underlying data, so re-binning the CURRENT
run's values into those same edges and comparing against that fixed
expected shape is a real, computationally cheap histogram comparison —
one bounded stream over the column, not an unbounded one.

PSI convention (finance/ML industry standard): <0.1 no significant
shift, 0.1-0.25 moderate, >0.25 significant — default threshold 0.2.
JS divergence is in nats, bounded [0, ln(2)] ≈ [0, 0.693] — default
threshold 0.1 (a JS of 0 means identical distributions).
"""
from __future__ import annotations

import math

from app.anomaly.detectors.base import AnomalyHit, DEFAULT_SCAN_CAP, coerce_float

DEFAULT_PARAMS = {"DISTRIBUTION": {"psi_threshold": 0.2, "js_threshold": 0.1}}

_EPS = 1e-6
_NUMERIC_BIN_KEYS = ("p1", "p25", "p50", "p75", "p99")
_EXPECTED_BIN_PROPORTIONS = [0.01, 0.24, 0.25, 0.25, 0.24, 0.01]  # by definition of the percentiles used as edges


def _psi(expected: list[float], actual: list[float]) -> float:
    total = 0.0
    for e, a in zip(expected, actual):
        e, a = max(e, _EPS), max(a, _EPS)
        total += (a - e) * math.log(a / e)
    return total


def _jensen_shannon(p: list[float], q: list[float]) -> float:
    def _kl(x: list[float], y: list[float]) -> float:
        return sum(xi * math.log(xi / yi) for xi, yi in zip(x, y) if xi > 0)
    m = [(pi + qi) / 2 for pi, qi in zip(p, q)]
    m = [max(mi, _EPS) for mi in m]
    return 0.5 * _kl(p, m) + 0.5 * _kl(q, m)


def _bin_index(value: float, edges: list[float]) -> int:
    for i, edge in enumerate(edges):
        if value <= edge:
            return i
    return len(edges)


def detect_distribution_numeric(
    row_source, column: str, baseline_stats: dict, *,
    psi_threshold: float = 0.2, js_threshold: float = 0.1, cap: int = DEFAULT_SCAN_CAP,
) -> list[AnomalyHit]:
    edges = [baseline_stats.get(k) for k in _NUMERIC_BIN_KEYS]
    if any(e is None for e in edges) or any(edges[i] > edges[i + 1] for i in range(len(edges) - 1)):
        return []  # no usable baseline percentile edges yet (cold start, or a degenerate/all-equal baseline sample)

    bins = [0] * (len(edges) + 1)
    total = 0
    for chunk in row_source.iter_rows([column], chunk_size=5000):
        for row in chunk:
            fv = coerce_float(row.get(column))
            if fv is None:
                continue
            bins[_bin_index(fv, edges)] += 1
            total += 1
        if total >= cap:
            break
    if total == 0:
        return []

    current_props = [b / total for b in bins]
    return _score_distribution(current_props, _EXPECTED_BIN_PROPORTIONS, psi_threshold, js_threshold)


def detect_distribution_categorical(
    current_stats: dict, baseline_stats: dict | None, *,
    psi_threshold: float = 0.2, js_threshold: float = 0.1,
) -> list[AnomalyHit]:
    current_categories: dict = current_stats.get("category_counts") or {}
    baseline_categories: dict = (baseline_stats or {}).get("category_counts") or {}
    if not current_categories or not baseline_categories:
        return []

    keys = sorted(set(current_categories) | set(baseline_categories))
    current_total = sum(current_categories.values()) or 1
    baseline_total = sum(baseline_categories.values()) or 1
    current_props = [current_categories.get(k, 0) / current_total for k in keys]
    baseline_props = [baseline_categories.get(k, 0) / baseline_total for k in keys]

    return _score_distribution(current_props, baseline_props, psi_threshold, js_threshold)


def _score_distribution(actual: list[float], expected: list[float], psi_threshold: float, js_threshold: float) -> list[AnomalyHit]:
    psi = _psi(expected, actual)
    js = _jensen_shannon(expected, actual)
    hits: list[AnomalyHit] = []
    if psi >= psi_threshold:
        hits.append(AnomalyHit(
            anomaly_type="DISTRIBUTION_PSI", record_identifier=None, value=f"PSI={psi:.4f}",
            expected_range=f"PSI < {psi_threshold:g}", raw_score=psi,
        ))
    if js >= js_threshold:
        hits.append(AnomalyHit(
            anomaly_type="DISTRIBUTION_JS", record_identifier=None, value=f"JS={js:.4f}",
            expected_range=f"JS < {js_threshold:g}", raw_score=js,
        ))
    return hits


DETECTORS = {"DISTRIBUTION": None}  # dispatched by data type inside engine.py, not a single flat function — see engine.py
