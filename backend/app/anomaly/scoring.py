"""
Anomaly scoring — every detector (see detectors/) produces a raw,
algorithm-specific score (a z-score, an IQR multiple, a percentile
distance...) that means something different depending on which method
produced it. This module normalizes any raw score into one comparable
0+ band, per the spec's default table:

    0–1   Normal
    1–3   Low
    3–5   Medium
    5–8   High
    >8    Critical

AnomalyRecord.score stores the normalized value; AnomalyRecord.raw_score
keeps the original algorithm's own number — never discarded, since a raw
z-score of 4.2 and a raw IQR-multiple of 4.2 aren't the same thing and a
user debugging a specific anomaly needs to see which.

The band boundaries and the SEVERITY mapping are both configurable per
job (AnomalyJob.threshold_config["score_bands"]) — DEFAULT_BANDS below is
only the fallback when a job doesn't override it.
"""
from __future__ import annotations

from app.models.anomaly import AnomalySeverity

# (upper_bound_exclusive, band_label) — the last entry's upper bound is
# effectively +inf (any score >= its lower neighbor's bound falls here).
DEFAULT_BANDS: list[tuple[float, str]] = [
    (1.0, "NORMAL"),
    (3.0, "LOW"),
    (5.0, "MEDIUM"),
    (8.0, "HIGH"),
    (float("inf"), "CRITICAL"),
]

# band label -> AnomalySeverity — NORMAL never becomes an AnomalyRecord at
# all (see detectors/base.py: a detector only emits a hit for values it
# considers anomalous in the first place), so this only needs to cover
# LOW..CRITICAL; NORMAL is mapped here purely for completeness/testability.
BAND_TO_SEVERITY: dict[str, AnomalySeverity] = {
    "NORMAL": AnomalySeverity.LOW,
    "LOW": AnomalySeverity.LOW,
    "MEDIUM": AnomalySeverity.MEDIUM,
    "HIGH": AnomalySeverity.HIGH,
    "CRITICAL": AnomalySeverity.CRITICAL,
}


def normalize_score(raw_score: float, bands: list[tuple[float, str]] | None = None) -> float:
    """Maps an arbitrary non-negative raw score onto the 0+ normalized
    scale. Deliberately simple and monotonic (not a rescale into a fixed
    max) — the raw score's own magnitude already carries meaning (a
    z-score of 12 IS more extreme than one of 9), so this just clamps
    negative/malformed input to 0 rather than inventing a ceiling."""
    return max(0.0, float(raw_score))


def band_for_score(score: float, bands: list[tuple[float, str]] | None = None) -> str:
    bands = bands or DEFAULT_BANDS
    for upper, label in bands:
        if score < upper:
            return label
    return bands[-1][1]


def severity_for_score(score: float, bands: list[tuple[float, str]] | None = None) -> AnomalySeverity:
    return BAND_TO_SEVERITY[band_for_score(score, bands)]


def bands_from_config(threshold_config: dict) -> list[tuple[float, str]]:
    """Reads a job's threshold_config["score_bands"] override, if
    present — shape: [{"upper": 1.0, "label": "NORMAL"}, ...], same order
    dependency as DEFAULT_BANDS (ascending upper bounds, last one treated
    as the ceiling regardless of its stated value). Falls back to
    DEFAULT_BANDS on anything malformed rather than raising — a bad
    config value shouldn't crash scoring for an otherwise-valid job."""
    raw = threshold_config.get("score_bands") if threshold_config else None
    if not raw:
        return DEFAULT_BANDS
    try:
        return [(float(b["upper"]), str(b["label"])) for b in raw]
    except (KeyError, TypeError, ValueError):
        return DEFAULT_BANDS
