"""
Baseline resolution, versioning and manual approval.

A "baseline" is the "expected" stats snapshot (same shape as
AnomalyColumnResult.current_stats — see anomaly/profiler.py) that a
column's CURRENT run is compared against to detect drift/anomalies.
AnomalyBaseline rows are versioned and never overwritten (see
models/anomaly.py's module docstring) — every (re)computation appends a
new version row, so a historical execution's "baseline vs current"
comparison keeps its original meaning even after the baseline moves on.

Five sources (AnomalyJob.baseline_config["source"], one of
BaselineSource's members):
  PREVIOUS_RUN    — the immediately preceding execution's stats, verbatim.
  LAST_N_RUNS     — numeric fields averaged across the last N executions.
  LAST_N_DAYS     — numeric fields averaged across executions in the last
                    N days.
  ROLLING_WINDOW  — same averaging as LAST_N_RUNS, but recomputed and
                    re-versioned on every run (vs. LAST_N_RUNS' behavior
                    of only advancing the window one run at a time).
  MANUAL          — never auto-computed. A user creates/approves the
                    exact baseline via the AD4 approval API; resolve_baseline
                    only ever returns the latest APPROVED version.

AnomalyJob.baseline_config["require_approval"] (bool, default False) gates
whether an auto-computed (non-MANUAL) version is immediately usable or
sits pending until a human approves it via approve_baseline() — this is
the "manual approval workflow" the spec requires as an option, not the
default (defaulting to require_approval=True would silently produce
"no baseline available" on every job's first N runs, which is worse than
an honest auto-baseline that a user can override at any time).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.anomaly import (
    AnomalyBaseline,
    AnomalyColumnResult,
    AnomalyExecution,
    AnomalyExecutionStatus,
    BaselineSource,
)

DEFAULT_LAST_N_RUNS = 5
DEFAULT_LAST_N_DAYS = 30

# current_stats/baseline_stats keys that are numeric and safe to average
# across multiple historical snapshots. Anything else (e.g. "sampled",
# "top_values", "data_type") is metadata, not carried through averaging.
_AVERAGEABLE_KEYS = {
    "count", "null_count", "null_pct", "distinct_count", "distinct_pct",
    "min", "max", "mean", "median", "std_dev", "variance",
    "p1", "p5", "p25", "p50", "p75", "p95", "p99", "iqr",
    "zero_count", "negative_count", "empty_count", "min_length", "max_length",
    "avg_length", "true_count", "false_count", "min_date", "max_date",
    "sample_size",
}


async def _successful_column_results(
    db: AsyncSession, job_id: int, column_name: str, *, limit: int | None = None,
    since: datetime | None = None,
) -> list[AnomalyColumnResult]:
    """Column results for successful/partial executions of this job,
    newest first. Backing query for every auto-computed source below."""
    stmt = (
        select(AnomalyColumnResult)
        .join(AnomalyExecution, AnomalyExecution.id == AnomalyColumnResult.execution_id)
        .where(
            AnomalyExecution.job_id == job_id,
            AnomalyColumnResult.column_name == column_name,
            AnomalyExecution.status.in_(
                [AnomalyExecutionStatus.SUCCESS, AnomalyExecutionStatus.PARTIAL_SUCCESS]
            ),
        )
        .order_by(AnomalyExecution.completed_at.desc())
    )
    if since is not None:
        stmt = stmt.where(AnomalyExecution.completed_at >= since)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _merge_stats(snapshots: list[dict]) -> dict:
    """Averages numeric fields across N stats snapshots. Non-numeric
    metadata is taken from the most recent snapshot (snapshots[0], since
    callers always pass newest-first lists). Returns {} for an empty
    input — callers treat that as "no baseline available yet"."""
    if not snapshots:
        return {}
    merged: dict = dict(snapshots[0])
    for key in _AVERAGEABLE_KEYS:
        values = [s[key] for s in snapshots if isinstance(s.get(key), (int, float))]
        if values:
            merged[key] = sum(values) / len(values)
    merged["sampled"] = any(bool(s.get("sampled")) for s in snapshots)
    merged["baseline_run_count"] = len(snapshots)
    return merged


async def compute_stats_from_history(
    db: AsyncSession, job_id: int, column_name: str, *,
    source: BaselineSource, n_runs: int = DEFAULT_LAST_N_RUNS, n_days: int = DEFAULT_LAST_N_DAYS,
) -> dict:
    """Computes a fresh baseline snapshot from execution history per
    `source`'s policy. Returns {} on cold start (no prior successful
    execution exists yet for this job+column) — the caller (resolve_baseline)
    treats that as "not enough history, skip comparison this run"."""
    if source == BaselineSource.PREVIOUS_RUN:
        rows = await _successful_column_results(db, job_id, column_name, limit=1)
    elif source == BaselineSource.LAST_N_DAYS:
        since = datetime.now(timezone.utc) - timedelta(days=n_days)
        rows = await _successful_column_results(db, job_id, column_name, since=since)
    else:  # LAST_N_RUNS, ROLLING_WINDOW — same "average the last N" policy
        rows = await _successful_column_results(db, job_id, column_name, limit=n_runs)

    snapshots = [row.current_stats or {} for row in rows if row.current_stats]
    return _merge_stats(snapshots)


async def get_latest_version(db: AsyncSession, job_id: int, column_name: str) -> AnomalyBaseline | None:
    result = await db.execute(
        select(AnomalyBaseline)
        .where(AnomalyBaseline.job_id == job_id, AnomalyBaseline.column_name == column_name)
        .order_by(AnomalyBaseline.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_baseline_by_id(db: AsyncSession, baseline_id: int) -> AnomalyBaseline | None:
    result = await db.execute(select(AnomalyBaseline).where(AnomalyBaseline.id == baseline_id))
    return result.scalar_one_or_none()


async def list_baselines(db: AsyncSession, job_id: int, column_name: str | None = None) -> list[AnomalyBaseline]:
    """Every version, newest first — the Baselines UI (AD9's Configuration
    page, or a per-job baseline tab) shows the full version history, not
    just the latest, since a user approving an OLDER version over a newer
    one is a legitimate action (see approve_baseline's docstring)."""
    stmt = select(AnomalyBaseline).where(AnomalyBaseline.job_id == job_id)
    if column_name:
        stmt = stmt.where(AnomalyBaseline.column_name == column_name)
    stmt = stmt.order_by(AnomalyBaseline.column_name, AnomalyBaseline.version.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_latest_approved(db: AsyncSession, job_id: int, column_name: str) -> AnomalyBaseline | None:
    result = await db.execute(
        select(AnomalyBaseline)
        .where(
            AnomalyBaseline.job_id == job_id,
            AnomalyBaseline.column_name == column_name,
            AnomalyBaseline.approved_at.is_not(None),
        )
        .order_by(AnomalyBaseline.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def record_baseline_version(
    db: AsyncSession, *, job_id: int, column_name: str, stats: dict,
    source: BaselineSource, auto_approve: bool = False, approved_by: int | None = None,
) -> AnomalyBaseline:
    """Appends a new, monotonically-versioned baseline row. Never updates
    an existing row — see module docstring."""
    latest = await get_latest_version(db, job_id, column_name)
    next_version = (latest.version + 1) if latest else 1
    row = AnomalyBaseline(
        job_id=job_id,
        column_name=column_name,
        version=next_version,
        stats=stats,
        source=source,
    )
    if auto_approve:
        row.approved_by = approved_by
        row.approved_at = datetime.now(timezone.utc)
    db.add(row)
    await db.flush()
    return row


async def approve_baseline(db: AsyncSession, baseline: AnomalyBaseline, *, approved_by: int | None) -> AnomalyBaseline:
    """Manual approval — see AD4's approval API. Approving an OLDER
    version does not retroactively unapprove a newer one; resolve_baseline
    always reads the latest *approved* version, so approving version 3
    after version 4 already exists still means version 4 wins once it is
    itself approved. This mirrors "approval is a per-version fact", not
    "there's exactly one approved slot"."""
    baseline.approved_by = approved_by
    baseline.approved_at = datetime.now(timezone.utc)
    await db.flush()
    return baseline


async def resolve_baseline(
    db: AsyncSession, *, job_id: int, column_name: str, baseline_config: dict,
) -> dict | None:
    """The entry point anomaly/engine.py calls once per column per run,
    BEFORE detection — returns the stats dict to diff the current run
    against, or None if no baseline is available yet (first run, or a
    MANUAL source with nothing approved yet). Also persists a new
    AnomalyBaseline version for auto-computed sources so every run's
    "what did we compare against" stays auditable, even when the merged
    stats happen to equal the previous version's (still recorded as a new
    version — recomputing IS the event being versioned, not just a value
    change)."""
    baseline_config = baseline_config or {}
    try:
        source = BaselineSource(baseline_config.get("source", BaselineSource.PREVIOUS_RUN.value))
    except ValueError:
        source = BaselineSource.PREVIOUS_RUN
    require_approval = bool(baseline_config.get("require_approval", False))

    if source == BaselineSource.MANUAL:
        approved = await get_latest_approved(db, job_id, column_name)
        return approved.stats if approved else None

    stats = await compute_stats_from_history(
        db, job_id, column_name,
        source=source,
        n_runs=int(baseline_config.get("n_runs", DEFAULT_LAST_N_RUNS)),
        n_days=int(baseline_config.get("n_days", DEFAULT_LAST_N_DAYS)),
    )
    if not stats:
        return None  # cold start — nothing to compare against yet

    await record_baseline_version(
        db, job_id=job_id, column_name=column_name, stats=stats,
        source=source, auto_approve=not require_approval,
    )

    if require_approval:
        # A fresh version was just recorded but is pending approval — the
        # CURRENT run still needs something to compare against, so fall
        # back to the latest already-approved version (if any) rather
        # than silently skipping detection this run.
        approved = await get_latest_approved(db, job_id, column_name)
        return approved.stats if approved else None

    return stats
