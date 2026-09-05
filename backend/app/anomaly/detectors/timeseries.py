"""
Time-series anomaly detection — buckets a real time column (optionally
paired with a numeric value column; without one, the "value" per bucket
is simply the row count, i.e. volume) into day/hour/week/month buckets
via one bounded stream (DEFAULT_SCAN_CAP rows, same as every other
detector), then evaluates whichever of the spec's seven sub-methods the
job configured against that real bucketed series:

  TREND          — whole-series linear-regression slope, flagged when its
                    magnitude relative to the series mean exceeds a
                    threshold (one hit describing the whole window, not
                    per-bucket — a trend is a property of the series).
  SPIKE / DROP    — a bucket's value jumps up/down more than
                    `rate_threshold_pct` versus the immediately prior
                    bucket (two directions of the same rate-of-change
                    check, reported under distinct anomaly_types so a
                    user can tell "surge" from "collapse" at a glance).
  RATE_OF_CHANGE  — same underlying check as SPIKE/DROP but reported
                    regardless of direction — enable this instead of (or
                    alongside) SPIKE/DROP when direction doesn't matter.
  MOVING_AVERAGE  — a bucket deviates from the trailing `window`-bucket
                    mean by more than `stddev_threshold` trailing
                    standard deviations (classic Bollinger-band style
                    check, using only buckets STRICTLY BEFORE the one
                    being evaluated — never leaks future data into a
                    "would we have flagged this at the time" check).
  ROLLING_STDDEV  — flags a bucket whose trailing rolling stddev itself
                    jumps versus the trailing rolling stddev one window
                    earlier (volatility-of-volatility — distinct signal
                    from MOVING_AVERAGE, which flags level shifts).
  SEASONAL        — groups buckets by weekday (day-granularity buckets
                    only) and flags a bucket that deviates from ITS OWN
                    weekday's historical mean — a real Monday-vs-Monday
                    comparison, not a same-period-last-year comparison
                    (which would need far more history than a bounded
                    scan can hold).
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from app.anomaly.detectors.base import AnomalyHit, DEFAULT_SCAN_CAP, coerce_float

DEFAULT_PARAMS = {
    "TIMESERIES": {
        "bucket": "day", "methods": ["SPIKE", "DROP", "MOVING_AVERAGE"],
        "rate_threshold_pct": 50.0, "window": 7, "stddev_threshold": 3.0, "trend_threshold_pct": 20.0,
    },
}


def _parse_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _bucket_key(dt: datetime, bucket: str) -> str:
    if bucket == "hour":
        return dt.strftime("%Y-%m-%d %H:00")
    if bucket == "week":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if bucket == "month":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y-%m-%d")  # default: day


def _aggregate(row_source, time_column: str, value_column: str | None, bucket: str, cap: int) -> dict[str, float]:
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    cols = [time_column] if not value_column or value_column == time_column else [time_column, value_column]
    seen = 0
    for chunk in row_source.iter_rows(cols, chunk_size=5000):
        for row in chunk:
            dt = _parse_dt(row.get(time_column))
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            key = _bucket_key(dt, bucket)
            counts[key] += 1
            if value_column:
                fv = coerce_float(row.get(value_column))
                if fv is not None:
                    sums[key] += fv
            seen += 1
        if seen >= cap:
            break
    if value_column:
        return {k: sums[k] / counts[k] for k in counts}  # mean value per bucket
    return dict(counts)  # row volume per bucket


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean, math.sqrt(variance)


def detect_timeseries(
    row_source, time_column: str, value_column: str | None = None, *,
    bucket: str = "day", methods: list[str] | None = None,
    rate_threshold_pct: float = 50.0, window: int = 7, stddev_threshold: float = 3.0,
    trend_threshold_pct: float = 20.0, cap: int = DEFAULT_SCAN_CAP,
) -> list[AnomalyHit]:
    methods = methods or ["SPIKE", "DROP", "MOVING_AVERAGE"]
    series_map = _aggregate(row_source, time_column, value_column, bucket, cap)
    if len(series_map) < 2:
        return []  # not enough buckets to compare anything against

    keys = sorted(series_map)  # ISO-shaped bucket keys sort chronologically as strings
    values = [series_map[k] for k in keys]
    hits: list[AnomalyHit] = []

    if "TREND" in methods and len(values) >= 3:
        n = len(values)
        xs = list(range(n))
        x_mean, y_mean = sum(xs) / n, sum(values) / n
        denom = sum((x - x_mean) ** 2 for x in xs) or 1.0
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / denom
        pct_change_over_series = (slope * (n - 1)) / y_mean * 100 if y_mean else 0.0
        if abs(pct_change_over_series) >= trend_threshold_pct:
            hits.append(AnomalyHit(
                anomaly_type="TREND", record_identifier=f"{keys[0]}..{keys[-1]}",
                value=f"{pct_change_over_series:+.1f}% over series", expected_range=f"within ±{trend_threshold_pct:g}%",
                raw_score=abs(pct_change_over_series) / trend_threshold_pct,
            ))

    if any(m in methods for m in ("SPIKE", "DROP", "RATE_OF_CHANGE")):
        for i in range(1, len(values)):
            prev, cur = values[i - 1], values[i]
            if prev == 0:
                continue
            pct_change = (cur - prev) / abs(prev) * 100
            if abs(pct_change) < rate_threshold_pct:
                continue
            score = abs(pct_change) / rate_threshold_pct
            if pct_change > 0 and "SPIKE" in methods:
                hits.append(AnomalyHit(anomaly_type="SPIKE", record_identifier=keys[i], value=f"{cur:g}", expected_range=f"~{prev:g} (prior bucket)", raw_score=score))
            elif pct_change < 0 and "DROP" in methods:
                hits.append(AnomalyHit(anomaly_type="DROP", record_identifier=keys[i], value=f"{cur:g}", expected_range=f"~{prev:g} (prior bucket)", raw_score=score))
            elif "RATE_OF_CHANGE" in methods:
                hits.append(AnomalyHit(anomaly_type="RATE_OF_CHANGE", record_identifier=keys[i], value=f"{pct_change:+.1f}%", expected_range=f"within ±{rate_threshold_pct:g}%", raw_score=score))

    if "MOVING_AVERAGE" in methods or "ROLLING_STDDEV" in methods:
        rolling_stds: list[float] = []
        for i in range(len(values)):
            history = values[max(0, i - window):i]
            if len(history) < max(2, window // 2):
                continue
            mean, std = _mean_std(history)
            rolling_stds.append(std)
            if "MOVING_AVERAGE" in methods and std > 0:
                z = abs(values[i] - mean) / std
                if z >= stddev_threshold:
                    hits.append(AnomalyHit(
                        anomaly_type="MOVING_AVERAGE", record_identifier=keys[i], value=f"{values[i]:g}",
                        expected_range=f"{mean - stddev_threshold * std:.3g} to {mean + stddev_threshold * std:.3g}",
                        raw_score=z,
                    ))
            if "ROLLING_STDDEV" in methods and len(rolling_stds) >= 2 and rolling_stds[-2] > 0:
                std_ratio = rolling_stds[-1] / rolling_stds[-2]
                if std_ratio >= 2.0 or std_ratio <= 0.5:
                    hits.append(AnomalyHit(
                        anomaly_type="ROLLING_STDDEV", record_identifier=keys[i],
                        value=f"stddev={rolling_stds[-1]:.3g}", expected_range=f"~{rolling_stds[-2]:.3g} (prior window)",
                        raw_score=max(std_ratio, 1 / std_ratio) if std_ratio else 999.0,
                    ))

    if "SEASONAL" in methods and bucket == "day":
        by_weekday: dict[int, list[float]] = defaultdict(list)
        for k, v in zip(keys, values):
            dt = datetime.strptime(k, "%Y-%m-%d")
            by_weekday[dt.weekday()].append(v)
        for idx, (k, v) in enumerate(zip(keys, values)):
            dt = datetime.strptime(k, "%Y-%m-%d")
            # Every OTHER bucket sharing this weekday, excluding this one
            # by position (not by value — two different days can
            # legitimately have the same value).
            history = [values[j] for j, kk in enumerate(keys) if j != idx and datetime.strptime(kk, "%Y-%m-%d").weekday() == dt.weekday()]
            if len(history) < 2:
                continue
            mean, std = _mean_std(history)
            if std > 0 and abs(v - mean) / std >= stddev_threshold:
                hits.append(AnomalyHit(
                    anomaly_type="SEASONAL", record_identifier=k, value=f"{v:g}",
                    expected_range=f"~{mean:.3g} (typical for this weekday)", raw_score=abs(v - mean) / std,
                ))

    return hits


DETECTORS = {"TIMESERIES": detect_timeseries}
