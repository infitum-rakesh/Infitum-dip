"""
Orchestration — one real AnomalyExecution end to end: build the row
source (real DB connection or real uploaded file, same resolution
reconciliation_service._build_row_source uses), profile every selected
column (anomaly/profiler.py), resolve each column's baseline
(anomaly/baseline.py), run every configured detector per column
(anomaly/detectors/), normalize scores (anomaly/scoring.py), persist one
AnomalyColumnResult per selected column ALWAYS (including PASS — see
models/anomaly.py's module docstring) plus up to MAX_PERSISTED_RECORDS_PER_COLUMN
AnomalyRecord rows each, and roll every column's status up into the
execution's overall_status.

services/anomaly_service.run_job() is the thin wrapper that creates the
AnomalyExecution row, calls run_execution() here, and translates any
failure into a FAILED execution — mirrors reconciliation_service.run_job()'s
shape (build source -> try/except -> status transition -> commit), with
the actual detection work factored out here instead of inlined, since
AD's detection surface (7+ methods x profiling x baselines x scoring) is
substantially larger than reconciliation's four comparison types.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.anomaly import baseline as baseline_mod
from app.anomaly import scoring
from app.anomaly.detectors import categorical as categorical_detector
from app.anomaly.detectors import distribution as distribution_detector
from app.anomaly.detectors import duplicate as duplicate_detector
from app.anomaly.detectors import format as format_detector
from app.anomaly.detectors import null_missing
from app.anomaly.detectors import statistical
from app.anomaly.detectors import timeseries as timeseries_detector
from app.anomaly.detectors import ALL_DEFAULT_PARAMS
from app.anomaly.profiler import classify_type, profile_columns
from app.connectors.base import ConnectorError, scrub_secrets
from app.models.anomaly import (
    AnomalyColumnResult,
    AnomalyExecution,
    AnomalyExecutionStatus,
    AnomalyJob,
    AnomalyRecord,
    AnomalySeverity,
    ColumnResultStatus,
    OverallStatus,
)
from app.reconciliation.engine import ConnectorRowSource, FileRowSource
from app.services import connection_service, uploaded_file_service

MAX_PERSISTED_RECORDS_PER_COLUMN = 200
_SEVERITY_ORDER = (AnomalySeverity.CRITICAL, AnomalySeverity.HIGH, AnomalySeverity.MEDIUM, AnomalySeverity.LOW)


class AnomalyEngineError(Exception):
    """A real, disclosed failure resolving the source or running detection — never faked."""


async def _build_row_source(db: AsyncSession, *, connection_id: int, schema: str | None, object_name: str,
                             uploaded_file_id: int | None, organization_id: int | None):
    """Same resolution reconciliation_service._build_row_source uses —
    re-verifies the connection/file against organization_id at RUN time
    (defense in depth, not just at job-creation time)."""
    if uploaded_file_id is not None:
        record = await uploaded_file_service.get_by_id(db, uploaded_file_id, organization_id)
        if record is None:
            raise AnomalyEngineError("The uploaded file for this job no longer exists or no longer belongs to this organization.")
        connector = uploaded_file_service.build_connector(record)
        return FileRowSource(connector, record.filename), connector
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise AnomalyEngineError("The connection for this job no longer exists or no longer belongs to this organization.")
    connector = connection_service.build_connector(conn)
    return ConnectorRowSource(connector, schema, object_name), connector


def _resolve_params(method: str, job_wide_params: dict, column_params: dict) -> dict:
    return {**ALL_DEFAULT_PARAMS.get(method, {}), **(job_wide_params.get(method) or {}), **(column_params.get(method) or {})}


def _run_detector(method: str, *, row_source, column: str, kind: str, current_stats: dict,
                   baseline_stats: dict | None, id_column: str | None, params: dict, job_config: dict) -> list:
    # Magnitude-based statistical methods prefer the approved baseline as
    # their "expected" reference; without one yet (cold start / MANUAL
    # source with nothing approved), fall back to the CURRENT run's own
    # stats so a brand-new job still gets same-run outlier detection
    # instead of silently skipping every statistical check.
    reference_stats = baseline_stats or current_stats

    if method == "Z_SCORE":
        return statistical.detect_zscore(row_source, column, reference_stats, id_column=id_column, **params)
    if method == "MODIFIED_Z_SCORE":
        return statistical.detect_modified_zscore(row_source, column, reference_stats, id_column=id_column, **params)
    if method == "IQR":
        return statistical.detect_iqr(row_source, column, reference_stats, id_column=id_column, **params)
    if method == "PERCENTILE":
        return statistical.detect_percentile(row_source, column, reference_stats, id_column=id_column, **params)
    if method == "NULL_RATE":
        return null_missing.detect_null_rate(row_source, column, current_stats, baseline_stats, id_column=id_column, **params)
    if method == "FORMAT":
        return format_detector.detect_format(row_source, column, reference_stats, id_column=id_column, **params)
    if method == "CATEGORICAL":
        return categorical_detector.detect_categorical(column, current_stats, baseline_stats, **params)
    if method == "DUPLICATE":
        return duplicate_detector.detect_duplicate(row_source, column, id_column=id_column, **params)
    if method == "DISTRIBUTION":
        if kind == "numeric":
            if not baseline_stats:
                return []  # PSI/JS need a real baseline population to compare against — no fabricated "expected" shape
            return distribution_detector.detect_distribution_numeric(row_source, column, baseline_stats, **params)
        return distribution_detector.detect_distribution_categorical(current_stats, baseline_stats, **params)
    return []  # unknown/unconfigured method name — never crash a whole run over one bad config entry


def _severity_rollup(severities: list[AnomalySeverity]) -> AnomalySeverity | None:
    if not severities:
        return None
    present = set(severities)
    for sev in _SEVERITY_ORDER:
        if sev in present:
            return sev
    return AnomalySeverity.LOW


def _column_status(severity: AnomalySeverity | None) -> ColumnResultStatus:
    if severity is None:
        return ColumnResultStatus.PASS
    if severity == AnomalySeverity.CRITICAL:
        return ColumnResultStatus.CRITICAL
    if severity in (AnomalySeverity.HIGH, AnomalySeverity.MEDIUM):
        return ColumnResultStatus.WARNING
    return ColumnResultStatus.INFO


def _overall_status(column_statuses: list[ColumnResultStatus]) -> OverallStatus:
    if any(s == ColumnResultStatus.ERROR for s in column_statuses):
        return OverallStatus.ERROR
    if any(s == ColumnResultStatus.CRITICAL for s in column_statuses):
        return OverallStatus.CRITICAL
    if any(s == ColumnResultStatus.WARNING for s in column_statuses):
        return OverallStatus.WARNING
    return OverallStatus.PASS


async def _persist_column_result(db: AsyncSession, *, execution_id: int, column_name: str, data_type: str,
                                  records: int, hits: list, baseline_stats: dict, current_stats: dict,
                                  bands, status_override: ColumnResultStatus | None = None) -> AnomalyColumnResult:
    """Scores every hit, persists the AnomalyColumnResult + up to
    MAX_PERSISTED_RECORDS_PER_COLUMN AnomalyRecord rows, and returns the
    saved column result (with its real .id, .status, .severity)."""
    severities: list[AnomalySeverity] = []
    scored: list[tuple] = []
    for hit in hits:
        normalized = scoring.normalize_score(hit.raw_score, bands)
        severity = scoring.severity_for_score(normalized, bands)
        severities.append(severity)
        scored.append((hit, normalized, severity))

    current_stats = dict(current_stats)
    current_stats["anomaly_records_truncated"] = len(hits) > MAX_PERSISTED_RECORDS_PER_COLUMN

    anomaly_count = len(hits)
    anomaly_rate = round(anomaly_count / records, 6) if records else 0.0
    severity = _severity_rollup(severities)
    status = status_override or _column_status(severity)

    col_result = AnomalyColumnResult(
        execution_id=execution_id, column_name=column_name, data_type=data_type,
        records=records, anomalies=anomaly_count, anomaly_rate=anomaly_rate,
        severity=severity, status=status,
        baseline_stats=baseline_stats or {}, current_stats=current_stats,
    )
    db.add(col_result)
    await db.flush()  # need col_result.id before writing child AnomalyRecord rows

    for hit, normalized, sev in scored[:MAX_PERSISTED_RECORDS_PER_COLUMN]:
        db.add(AnomalyRecord(
            execution_id=execution_id, column_result_id=col_result.id,
            record_identifier=hit.record_identifier, value=hit.value, expected_range=hit.expected_range,
            anomaly_type=hit.anomaly_type, score=normalized, raw_score=hit.raw_score, severity=sev,
        ))
    return col_result


async def run_execution(db: AsyncSession, *, job: AnomalyJob, execution: AnomalyExecution, organization_id: int | None) -> None:
    """Runs the full pipeline for one already-created (RUNNING) execution
    row and mutates it + its children in place. Raises AnomalyEngineError/
    ConnectorError on a genuine failure to even start (bad connection,
    profiling failure) — a per-column detector failure instead marks just
    that column ERROR and continues, so one bad column doesn't sink an
    otherwise-successful run. Caller (services/anomaly_service.run_job)
    commits and handles the raised-exception path."""
    t_start = time.monotonic()
    execution.started_at = datetime.now(timezone.utc)

    detection_config = job.detection_config or {}
    job_wide_methods = detection_config.get("methods") or []
    job_wide_params = detection_config.get("params") or {}
    id_column = detection_config.get("id_column")
    per_column_config = detection_config.get("columns") or {}
    bands = scoring.bands_from_config(job.threshold_config or {})

    columns = job.columns or []
    if not columns:
        raise AnomalyEngineError("This job has no selected columns to check.")

    row_source, connector = await _build_row_source(
        db, connection_id=job.connection_id, schema=job.schema_name, object_name=job.object_name,
        uploaded_file_id=job.uploaded_file_id, organization_id=organization_id,
    )

    t_profile_start = time.monotonic()
    try:
        stats = profile_columns(row_source, job.schema_name, job.object_name, columns)
    except ConnectorError as exc:
        raise AnomalyEngineError(scrub_secrets(str(exc), connector.secrets)) from exc
    profiling_ms = int((time.monotonic() - t_profile_start) * 1000)

    total_records = stats.get("__total__") or 0
    execution.records_scanned = total_records
    execution.columns_scanned = len(columns)

    column_results: list[AnomalyColumnResult] = []
    t_detect_start = time.monotonic()

    for col in columns:
        name = col["name"]
        kind = classify_type(col.get("data_type", ""))
        current_stats = dict(stats.get(name) or {})
        current_stats.setdefault("data_type", kind)

        baseline_stats = await baseline_mod.resolve_baseline(
            db, job_id=job.id, column_name=name, baseline_config=job.baseline_config or {},
        )

        col_config = per_column_config.get(name) or {}
        methods = col_config.get("methods") or job_wide_methods
        records_count = current_stats.get("count") or total_records or 0

        hits: list = []
        status_override = None
        try:
            for method in methods:
                params = _resolve_params(method, job_wide_params, col_config.get("params") or {})
                hits.extend(_run_detector(
                    method, row_source=row_source, column=name, kind=kind,
                    current_stats=current_stats, baseline_stats=baseline_stats,
                    id_column=id_column, params=params, job_config=detection_config,
                ))
        except ConnectorError as exc:
            hits = []
            status_override = ColumnResultStatus.ERROR
            current_stats["detector_error"] = scrub_secrets(str(exc), connector.secrets)

        col_result = await _persist_column_result(
            db, execution_id=execution.id, column_name=name, data_type=kind, records=records_count,
            hits=hits, baseline_stats=baseline_stats or {}, current_stats=current_stats,
            bands=bands, status_override=status_override,
        )
        column_results.append(col_result)

    # TIMESERIES is job-wide (driven by a configured time_column), not
    # tied to any one selected column — runs once, if configured, and
    # gets its own synthetic column-result row so it shows up in the
    # Results screen's column table alongside the real columns.
    if "TIMESERIES" in job_wide_methods and detection_config.get("time_column"):
        ts_params = _resolve_params("TIMESERIES", job_wide_params, {})
        ts_hits = timeseries_detector.detect_timeseries(
            row_source, detection_config["time_column"], detection_config.get("value_column"), **ts_params,
        )
        ts_name = detection_config.get("value_column") or detection_config["time_column"]
        ts_result = await _persist_column_result(
            db, execution_id=execution.id, column_name=f"{ts_name} (time series)", data_type="timeseries",
            records=total_records, hits=ts_hits, baseline_stats={},
            current_stats={"bucket": ts_params.get("bucket", "day")}, bands=bands,
        )
        column_results.append(ts_result)

    detection_ms = int((time.monotonic() - t_detect_start) * 1000)
    total_ms = int((time.monotonic() - t_start) * 1000)

    execution.profiling_ms = profiling_ms
    execution.detection_ms = detection_ms
    # db_ms/transfer_ms would need finer-grained instrumentation INSIDE
    # profiler.py/detectors (splitting "waiting on the DB" from
    # "deserializing rows in Python") than either currently records —
    # left None rather than fabricated from profiling_ms/detection_ms.
    execution.total_ms = total_ms
    execution.anomalies_found = sum(cr.anomalies for cr in column_results)
    execution.critical_count = sum(1 for cr in column_results if cr.status == ColumnResultStatus.CRITICAL)
    execution.warning_count = sum(1 for cr in column_results if cr.status == ColumnResultStatus.WARNING)
    execution.overall_status = _overall_status([cr.status for cr in column_results])

    has_error = any(cr.status == ColumnResultStatus.ERROR for cr in column_results)
    execution.status = AnomalyExecutionStatus.PARTIAL_SUCCESS if has_error else AnomalyExecutionStatus.SUCCESS
    execution.completed_at = datetime.now(timezone.utc)
