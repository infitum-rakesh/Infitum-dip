"""
Shared contract every detector module (statistical.py, and the
null/format/categorical/duplicate/distribution/timeseries detectors
added in AD3) implements: a `detect_*` function that scans a bounded
sample of one column's values through RowSource.iter_rows() (the SAME
portable, bounded-memory abstraction anomaly/profiler.py's tier-2 sample
uses — never a full-table read) and returns a list of AnomalyHit, one per
anomalous value found.

record_identifier resolution: if the job configured an `id_column` (a
real primary/business key from AnomalyJob.columns), that column's value
becomes AnomalyRecord.record_identifier — a stable, meaningful reference
across runs. Otherwise every hit falls back to its row's 1-based position
within THIS scan ("row #N") — NOT a stable key (a later run's row #42
may be a different underlying record), and every detector that falls
back to it sets AnomalyHit.identifier_is_position=True so the UI can
label it honestly instead of implying it's a primary key.

Detectors are pure functions, not classes — matches this codebase's
function-registry convention (mirrors how validation/rules.py maps a
rule "type" string to an evaluator function) rather than inventing a new
class-hierarchy pattern for this one feature.
"""
from __future__ import annotations

from dataclasses import dataclass

# Bounded-memory scan ceiling — same value as anomaly/profiler.SAMPLE_CAP,
# duplicated (not imported) so detectors/ has no import dependency on
# profiler.py; both independently enforce "never read an unbounded table".
DEFAULT_SCAN_CAP = 100_000


@dataclass
class AnomalyHit:
    anomaly_type: str          # e.g. "Z_SCORE", "IQR", "MODIFIED_Z_SCORE", "PERCENTILE"
    record_identifier: str | None
    value: str | None
    expected_range: str | None
    raw_score: float
    identifier_is_position: bool = False


def resolve_identifier(row: dict, id_column: str | None, position: int) -> tuple[str | None, bool]:
    """Returns (identifier, identifier_is_position)."""
    if id_column and row.get(id_column) is not None:
        return str(row[id_column]), False
    return f"row #{position}", True


def coerce_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
