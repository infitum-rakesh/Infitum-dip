"""
Categorical anomaly detection — flags values in a string/low-cardinality
column that are either:
  1. UNSEEN — never appeared in the baseline's category_counts at all
     (e.g. a new, unexpected status code showing up), or
  2. RARE — sits below `rare_threshold_pct` of THIS RUN's own category
     distribution (e.g. a status code that's 0.3% of today's rows) —
     evaluated against the current run alone, so it fires even on a
     job's first-ever execution, before any baseline exists.

Both read anomaly/profiler.py's `category_counts` (see profiler.py's
_tier2_string_sample) — no separate scan of its own beyond what
profiling already did, since the category frequency table IS the
profiling output for a string column.
"""
from __future__ import annotations

from app.anomaly.detectors.base import AnomalyHit

DEFAULT_PARAMS = {"CATEGORICAL": {"rare_threshold_pct": 0.5, "flag_unseen": True, "flag_rare": True}}


def detect_categorical(
    column: str, current_stats: dict, baseline_stats: dict | None, *,
    rare_threshold_pct: float = 0.5, flag_unseen: bool = True, flag_rare: bool = True,
) -> list[AnomalyHit]:
    current_categories: dict = current_stats.get("category_counts") or {}
    if not current_categories:
        return []  # not a categorical-shaped column (e.g. no category_counts computed), or genuinely empty

    current_total = sum(current_categories.values()) or 1
    baseline_categories: dict = (baseline_stats or {}).get("category_counts") or {}

    hits: list[AnomalyHit] = []
    for value, count in current_categories.items():
        current_pct = count / current_total * 100
        is_unseen = bool(baseline_categories) and value not in baseline_categories
        is_rare = current_pct < rare_threshold_pct

        if flag_unseen and is_unseen:
            hits.append(AnomalyHit(
                anomaly_type="CATEGORICAL_UNSEEN", record_identifier=None, value=str(value),
                expected_range="in baseline category set", raw_score=current_pct,
            ))
        elif flag_rare and is_rare:
            # Unlike UNSEEN, RARE's math (current_pct < rare_threshold_pct)
            # never actually reads baseline_categories — it's a property
            # of THIS run's own distribution, not a baseline comparison —
            # so it correctly fires on a job's very first execution too
            # (a status code sitting at 0.6% of today's rows is rare
            # today, independent of whether history exists yet).
            hits.append(AnomalyHit(
                anomaly_type="CATEGORICAL_RARE", record_identifier=None, value=str(value),
                expected_range=f">= {rare_threshold_pct:g}% of rows",
                raw_score=rare_threshold_pct / current_pct if current_pct > 0 else 999.0,
            ))
    return hits


DETECTORS = {"CATEGORICAL": detect_categorical}
