"""
Column profiler — computes the real per-column statistics AnomalyColumnResult.
baseline_stats/current_stats store, and every statistical detector (see
detectors/statistical.py) needs to evaluate a value.

Two-tier strategy, same "prefer the database, never transfer the whole
table" principle execution/worker.py and sql_export_executor.py already
follow:

TIER 1 (SQL aggregate, one round trip, exact, works at any row count):
count / null_count / distinct_count / min / max / mean / zero_count /
negative_count — every one of these is a plain COUNT/SUM(CASE...)/MIN/MAX/
AVG, which is portable across every SQL dialect this codebase's connector
matrix supports (no STDDEV/PERCENTILE_CONT needed) — same portable-aggregate
principle data_catalog_service._profile_sync() already established, just
extended with zero/negative counts. Attempted whenever the RowSource is a
real SQL connector (ConnectorRowSource whose connector implements
preview_sql — not every connector does, e.g. Salesforce/NetSuite SOAP).

TIER 2 (bounded sample, always available, honestly labeled): std_dev /
variance / median / quartiles / percentiles / IQR genuinely need to see
individual values, not just aggregates a handful of dialects expose
inconsistently (SQL Server STDEV vs Postgres/MySQL STDDEV_POP, and
PERCENTILE_CONT support varies even more). Rather than special-case each
dialect or silently approximate at unbounded scale, this always computes
those specific stats from a capped sample (SAMPLE_CAP values, taken via the
SAME bounded-memory RowSource.iter_rows() chunking every other streaming
path in this codebase uses — never the whole object) using Welford's
incremental algorithm for mean/variance (exact over the sample) and a
sorted-sample method for quartiles/percentiles. The result carries
`"sampled": True, "sample_size": N` whenever tier 2 ran on a partial
sample (i.e. the column has more than SAMPLE_CAP non-null values) — never
silently presented as an exact population statistic it isn't. String/date/
boolean profiling always goes through tier 2 (their stats aren't cheap
single-aggregate SQL across every dialect either — real, exact, and cheap
enough given SAMPLE_CAP, just not "one AVG() round trip" simple).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from app.connectors.base import ConnectorError, ConnectorUnsupportedError
from app.reconciliation.engine import ConnectorRowSource, RowSource

SAMPLE_CAP = 100_000  # bounded-memory ceiling for tier-2 stats — see module docstring
NUMERIC_TYPE_HINTS = ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL", "SERIAL", "MONEY", "NUMBER")
DATE_TYPE_HINTS = ("DATE", "TIME", "TIMESTAMP")
BOOLEAN_TYPE_HINTS = ("BOOL",)


def classify_type(data_type: str) -> str:
    """Maps a connector-reported SQL type string to one of the four
    profiling categories the spec calls for. Falls back to "string" —
    never raises on an unrecognized type, since a job's saved
    columns[].data_type is a snapshot taken at creation time and the
    source could legitimately report something this list doesn't
    anticipate (e.g. a vendor-specific type name)."""
    upper = (data_type or "").upper()
    if any(h in upper for h in BOOLEAN_TYPE_HINTS):
        return "boolean"
    if any(h in upper for h in DATE_TYPE_HINTS):
        return "date"
    if any(h in upper for h in NUMERIC_TYPE_HINTS):
        return "numeric"
    return "string"


def _sql_connector(row_source: RowSource):
    """Returns the connector if row_source is backed by a real SQL
    connection that genuinely supports arbitrary preview_sql (tier 1's
    precondition), else None. Never assumes — actually probes via a
    cheap SELECT 1, since preview_sql() unsupported is only reliably
    knowable by trying (ConnectorUnsupportedError vs a real dialect
    error are both possible failure shapes here)."""
    if not isinstance(row_source, ConnectorRowSource):
        return None
    return row_source.connector


def _tier1_sql_stats(connector, schema: str | None, object_name: str, columns: list[dict]) -> dict | None:
    """One aggregate SQL round trip covering every selected column —
    returns {column_name: {count, null_count, null_pct, distinct_count,
    min, max, mean, zero_count, negative_count}} for numeric columns and
    {column_name: {count, null_count, null_pct, distinct_count,
    empty_count}} for everything else, plus a "__total__" row count. None
    if this connector doesn't genuinely support ad-hoc SQL (e.g.
    Salesforce/NetSuite SOAP) — callers must fall back to tier 2 for
    everything in that case, including count itself."""
    quoted_table = connector.quote_object(object_name, schema)
    select_parts = ["COUNT(*) AS __total__"]
    kinds: dict[str, str] = {}
    for c in columns:
        name, kind = c["name"], classify_type(c.get("data_type", ""))
        kinds[name] = kind
        q = connector.quote_identifier(name)
        alias = f"c{len(kinds)}"  # positional alias — avoids re-deriving a name->column mapping through quoting rules
        select_parts.append(f"SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS {alias}_null")
        select_parts.append(f"COUNT(DISTINCT {q}) AS {alias}_distinct")
        if kind == "numeric":
            select_parts.append(f"MIN({q}) AS {alias}_min")
            select_parts.append(f"MAX({q}) AS {alias}_max")
            select_parts.append(f"AVG({q}) AS {alias}_avg")
            select_parts.append(f"SUM(CASE WHEN {q} = 0 THEN 1 ELSE 0 END) AS {alias}_zero")
            select_parts.append(f"SUM(CASE WHEN {q} < 0 THEN 1 ELSE 0 END) AS {alias}_neg")
        elif kind == "string":
            select_parts.append(f"SUM(CASE WHEN {q} = '' THEN 1 ELSE 0 END) AS {alias}_empty")
        elif kind == "date":
            select_parts.append(f"MIN({q}) AS {alias}_min")
            select_parts.append(f"MAX({q}) AS {alias}_max")

    sql = f"SELECT {', '.join(select_parts)} FROM {quoted_table}"
    try:
        result = connector.preview_sql(sql, {}, 1)
    except (ConnectorUnsupportedError, ConnectorError):
        return None
    if not result.rows:
        return None
    row = dict(zip(result.columns, result.rows[0]))
    total = row.get("__total__") or 0

    stats: dict[str, dict] = {"__total__": total}
    for i, c in enumerate(columns, start=1):
        name, kind = c["name"], kinds[c["name"]]
        alias = f"c{i}"
        null_n = row.get(f"{alias}_null") or 0
        distinct_n = row.get(f"{alias}_distinct") or 0
        entry = {
            "count": total, "null_count": null_n,
            "null_pct": round(null_n / total * 100, 3) if total else 0.0,
            "distinct_count": distinct_n,
        }
        if kind == "numeric":
            mn, mx, avg = row.get(f"{alias}_min"), row.get(f"{alias}_max"), row.get(f"{alias}_avg")
            entry.update({
                "min": float(mn) if mn is not None else None, "max": float(mx) if mx is not None else None,
                "mean": round(float(avg), 6) if avg is not None else None,
                "zero_count": row.get(f"{alias}_zero") or 0, "negative_count": row.get(f"{alias}_neg") or 0,
            })
        elif kind == "string":
            entry["empty_count"] = row.get(f"{alias}_empty") or 0
        elif kind == "date":
            # Real DB drivers (psycopg2 et al.) return live datetime/date
            # objects for these aggregates, not strings — and this dict
            # gets persisted straight into a JSON column (AnomalyColumnResult
            # .current_stats), which can't serialize a raw datetime. Tier 2's
            # date path already normalizes to isoformat (see below); tier 1
            # needs the same treatment.
            mn, mx = row.get(f"{alias}_min"), row.get(f"{alias}_max")
            entry["min"] = mn.isoformat() if hasattr(mn, "isoformat") else mn
            entry["max"] = mx.isoformat() if hasattr(mx, "isoformat") else mx
        stats[name] = entry
    return stats


class _Welford:
    """Incremental mean/variance — one pass, O(1) memory regardless of
    how many values are fed in. Standard numerically-stable algorithm
    (Welford 1962), the same one every streaming-stats library uses
    instead of the naive sum-of-squares formula (which loses precision
    at scale)."""
    __slots__ = ("n", "mean", "m2")

    def __init__(self):
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.m2 += delta * delta2

    @property
    def variance(self) -> float | None:
        return self.m2 / self.n if self.n > 1 else None

    @property
    def std_dev(self) -> float | None:
        v = self.variance
        return math.sqrt(v) if v is not None else None


def _percentiles(sorted_values: list[float], pcts: tuple[float, ...]) -> dict[str, float]:
    n = len(sorted_values)
    if n == 0:
        return {f"p{int(p * 100)}": None for p in pcts}
    out = {}
    for p in pcts:
        idx = min(n - 1, max(0, round(p * (n - 1))))
        out[f"p{int(p * 100)}"] = sorted_values[idx]
    return out


def _tier2_numeric_sample(row_source: RowSource, column: str, cap: int = SAMPLE_CAP) -> dict:
    """Streams up to `cap` non-null numeric values for one column via the
    same bounded RowSource.iter_rows() chunking every other real
    streaming path in this codebase uses, computing exact mean/variance/
    std_dev over what it actually saw (Welford's) plus quartile/
    percentile estimates from a sorted copy of that same sample. Stops as
    soon as `cap` values are collected — for a table with more rows than
    that, later rows are simply never read, which is the point (bounded
    memory AND bounded I/O, not just bounded memory over an unbounded
    read)."""
    w = _Welford()
    sample: list[float] = []
    seen = 0
    for chunk in row_source.iter_rows([column], chunk_size=5000):
        for r in chunk:
            v = r.get(column)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            w.add(fv)
            sample.append(fv)
            seen += 1
        if seen >= cap:
            break
    sample.sort()
    q1 = _percentiles(sample, (0.25,)).get("p25")
    q3 = _percentiles(sample, (0.75,)).get("p75")
    iqr = (q3 - q1) if (q1 is not None and q3 is not None) else None
    return {
        # "mean" was already being computed by Welford's below for
        # std_dev/variance but was never surfaced — a real, silent gap for
        # any non-SQL-connector source (uploaded files, DataFrameRowSource):
        # detect_zscore()/detect_modified_zscore() both read
        # reference_stats["mean"]/["median"] and quietly find zero hits
        # without it. Tier 1 (a real SQL connector) already supplies an
        # exact "mean" via AVG(); this tier-2 value is the same exact-over-
        # the-sample guarantee std_dev/median already carry, not an
        # approximation on top of them.
        "mean": w.mean if w.n > 0 else None,
        "std_dev": w.std_dev, "variance": w.variance,
        "median": _percentiles(sample, (0.5,)).get("p50"),
        **_percentiles(sample, (0.01, 0.25, 0.5, 0.75, 0.99)),
        "iqr": iqr,
        "sampled": seen >= cap,
        "sample_size": seen,
    }


def _tier2_string_sample(row_source: RowSource, column: str, cap: int = SAMPLE_CAP, category_cap: int = 1000) -> dict:
    """`category_counts` is the same value->count tally duplicate_pct
    always needed, now also exposed for detectors/categorical.py and
    detectors/distribution.py (a string column's "distribution" IS its
    category frequency table). Capped at `category_cap` DISTINCT keys —
    not a cap on rows scanned (that's `cap`/SAMPLE_CAP already) — so a
    free-text or GUID-like column doesn't balloon current_stats' JSON
    into thousands of one-off keys. A value first seen after the cap is
    full is still counted toward `seen`/lengths/duplicate detection isn't
    retroactively updated for it, so `categories_truncated=True` honestly
    flags that category_counts (and duplicate_pct, for very-high-
    cardinality columns) may be incomplete beyond the cap — never
    silently presented as exhaustive."""
    lengths: list[int] = []
    duplicates_seen: dict[str, int] = {}
    categories_truncated = False
    seen = 0
    for chunk in row_source.iter_rows([column], chunk_size=5000):
        for r in chunk:
            v = r.get(column)
            if v is None:
                continue
            s = str(v)
            lengths.append(len(s))
            if s in duplicates_seen:
                duplicates_seen[s] += 1
            elif len(duplicates_seen) < category_cap:
                duplicates_seen[s] = 1
            else:
                categories_truncated = True
            seen += 1
        if seen >= cap:
            break
    dup_count = sum(n - 1 for n in duplicates_seen.values() if n > 1)
    return {
        "min_length": min(lengths) if lengths else None,
        "max_length": max(lengths) if lengths else None,
        "avg_length": round(sum(lengths) / len(lengths), 2) if lengths else None,
        "duplicate_pct": round(dup_count / seen * 100, 2) if seen else 0.0,
        "category_counts": duplicates_seen,
        "categories_truncated": categories_truncated,
        "sampled": seen >= cap,
        "sample_size": seen,
    }


def _tier2_date_sample(row_source: RowSource, column: str, cap: int = SAMPLE_CAP) -> dict:
    now = datetime.now(timezone.utc)
    future_count = 0
    mn = mx = None
    seen = 0
    for chunk in row_source.iter_rows([column], chunk_size=5000):
        for r in chunk:
            v = r.get(column)
            if v is None:
                continue
            dt = v if isinstance(v, datetime) else None
            if dt is None:
                try:
                    dt = datetime.fromisoformat(str(v))
                except ValueError:
                    continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > now:
                future_count += 1
            mn = dt if mn is None or dt < mn else mn
            mx = dt if mx is None or dt > mx else mx
            seen += 1
        if seen >= cap:
            break
    return {
        "min": mn.isoformat() if mn else None, "max": mx.isoformat() if mx else None,
        "future_date_count": future_count, "sampled": seen >= cap, "sample_size": seen,
    }


def _tier2_boolean_sample(row_source: RowSource, column: str, cap: int = SAMPLE_CAP) -> dict:
    true_count = false_count = null_count = 0
    seen = 0
    for chunk in row_source.iter_rows([column], chunk_size=5000):
        for r in chunk:
            v = r.get(column)
            if v is None:
                null_count += 1
            elif v in (True, 1, "1", "true", "True", "TRUE", "t", "T"):
                true_count += 1
            elif v in (False, 0, "0", "false", "False", "FALSE", "f", "F"):
                false_count += 1
            seen += 1
        if seen >= cap:
            break
    return {"true_count": true_count, "false_count": false_count, "null_count": null_count, "sampled": seen >= cap, "sample_size": seen}


def profile_columns(row_source: RowSource, schema: str | None, object_name: str, columns: list[dict]) -> dict[str, dict]:
    """Main entry point — returns {column_name: stats_dict} for every
    column in `columns` (list of {"name", "data_type"}), plus a
    "__total__" key with the object's real row count. See module
    docstring for the tier-1/tier-2 strategy."""
    connector = _sql_connector(row_source)
    tier1 = _tier1_sql_stats(connector, schema, object_name, columns) if connector is not None else None

    total = tier1["__total__"] if tier1 else row_source.count()
    out: dict[str, dict] = {"__total__": total}
    for c in columns:
        name, kind = c["name"], classify_type(c.get("data_type", ""))
        entry = dict(tier1[name]) if tier1 and name in tier1 else {"count": total, "null_count": None, "null_pct": None, "distinct_count": None}
        entry["data_type"] = kind

        if kind == "numeric":
            entry.update(_tier2_numeric_sample(row_source, name))
        elif kind == "string":
            entry.update(_tier2_string_sample(row_source, name))
        elif kind == "date":
            date_sample = _tier2_date_sample(row_source, name)
            # Prefer tier 1's exact min/max (real aggregate) over the
            # sample's when tier 1 ran — only fall back to the sample's
            # min/max when there was no SQL fast path at all.
            if "min" in entry and entry.get("min") is not None:
                date_sample.pop("min", None); date_sample.pop("max", None)
            entry.update(date_sample)
        elif kind == "boolean":
            entry.update(_tier2_boolean_sample(row_source, name))

        out[name] = entry
    return out
