"""
partition_reader — splits a [min_value, max_value] key range into N
contiguous partitions for parallel extraction.

Only numeric and datetime keys can be split arithmetically in a way
that's actually safe (evenly-spaced cut points that don't depend on
collation/locale). String keys fall back to a single partition rather
than guessing at lexicographic bisection — a wrong split there could
silently skip or duplicate rows, which is worse than just not
parallelizing. This mirrors the same "don't fake it" rule the connector
layer follows: an honest single partition, not a plausible-looking but
unverified split.

Partitions are contiguous and non-overlapping by construction: partition
i covers (start_i, end_i] and start_(i+1) == end_i, so every row with a
real key value lands in exactly one partition. range_start=None means
"unbounded low" (first partition includes the true minimum); range_end
=None means "unbounded high" (last partition includes the true maximum,
immune to any float rounding at the top edge).
"""
from __future__ import annotations

from datetime import datetime, timezone

SPLITTABLE_KEY_TYPES = {"int", "float", "datetime"}


def plan_partitions(min_value, max_value, key_type: str, partition_count: int) -> list[tuple]:
    """Returns a list of (range_start, range_end) tuples in the original
    key type (not float) — always at least one partition."""
    if min_value is None or max_value is None:
        return [(None, None)]  # empty table — one no-op partition
    if key_type not in SPLITTABLE_KEY_TYPES or partition_count <= 1:
        return [(None, None)]

    lo, hi = _to_float(min_value, key_type), _to_float(max_value, key_type)
    if hi <= lo:
        return [(None, None)]

    step = (hi - lo) / partition_count
    # Interior cut points only — N-1 of them split the range into N parts.
    cut_points = [_from_float(lo + step * i, key_type) for i in range(1, partition_count)]

    starts = [None] + cut_points
    ends = cut_points + [None]
    return list(zip(starts, ends))


def _to_float(value, key_type: str) -> float:
    if key_type == "datetime":
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return float(value)


def _from_float(value: float, key_type: str):
    if key_type == "datetime":
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if key_type == "int":
        return round(value)
    return value
