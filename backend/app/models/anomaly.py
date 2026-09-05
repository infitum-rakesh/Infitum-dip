"""
Anomaly Detection — statistical/rule/ML-based scanning of any accessible
object's columns for outliers, null-rate spikes, duplicate blow-ups,
categorical drift, distribution shift and time-series anomalies. Sits
alongside Validation/Reconciliation as the platform's third Data Quality
module (see role_service.CATEGORY_MODULE_MAP / seller/module_service.
MODULE_CATALOG), and can additionally run embedded inside a Pipeline
Builder pipeline (execution/pipeline_job_executor.py's
"anomaly_detection_step" node type, added in a later phase).

AnomalyJob is a saved definition — one connection + schema/object (or an
uploaded file, same connection_id=0 sentinel convention Reconciliation/
Validation/custom_engine.py already use) plus the selected columns and
detection config as JSON blobs, same "flexible, per-row-shaped structure"
reasoning ColumnMapping.mappings/ValidationRuleSet.rules already
established — deliberately no separate anomaly_job_column/anomaly_rule
child tables.

AnomalyExecution/AnomalyColumnResult/AnomalyRecord record what a real run
actually found: anomaly/engine.py profiles the object (one aggregate SQL
round trip where possible, see anomaly/profiler.py), evaluates every
selected column's configured detectors, and persists one AnomalyColumnResult
per selected column — ALWAYS, including columns with zero anomalies
(status=PASS), never only the anomalous ones — plus up to a capped number
of individual AnomalyRecord rows per column (see anomaly/engine.py's
MAX_PERSISTED_RECORDS, added in a later phase).

AnomalyBaseline is versioned and never overwritten — each job+column gets a
new, monotonically-increasing version row every time a baseline is
(re)computed or manually approved, so a past execution's "baseline vs
current" comparison keeps meaning even after the baseline moves on.

AnomalyFeedback is an append-only log (never overwrites AnomalyRecord
itself) — a user marking a record TRUE_ANOMALY/FALSE_POSITIVE/EXPECTED/
IGNORED is preserved as history, same audit-safety reasoning
IncidentTimelineEvent already uses.

AnomalyReport is a structural clone of SqlWorkspaceExportJob — a large
report is always generated as a background job (see
anomaly_export_executor.py, added in a later phase), never synchronously
inside the request.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class AnomalyExecutionStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AnomalySeverity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ColumnResultStatus(str, enum.Enum):
    PASS = "PASS"
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"
    NOT_CHECKED = "NOT_CHECKED"


class OverallStatus(str, enum.Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"


class BaselineSource(str, enum.Enum):
    PREVIOUS_RUN = "PREVIOUS_RUN"
    LAST_N_RUNS = "LAST_N_RUNS"
    LAST_N_DAYS = "LAST_N_DAYS"
    ROLLING_WINDOW = "ROLLING_WINDOW"
    MANUAL = "MANUAL"


class FeedbackStatus(str, enum.Enum):
    TRUE_ANOMALY = "TRUE_ANOMALY"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    EXPECTED = "EXPECTED"
    IGNORED = "IGNORED"


class ReportFormat(str, enum.Enum):
    CSV = "CSV"
    XLSX = "XLSX"
    JSON = "JSON"
    PDF = "PDF"


class ReportStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AnomalyJob(TenantBase):
    __tablename__ = "anomaly_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 0 is the same "not a real Connection row, see uploaded_file_id
    # instead" sentinel Reconciliation/Validation/custom_engine.py all
    # use. Deliberately a bare int, not ForeignKey("connections.id") —
    # same reasoning as ValidationRuleSet.connection_id.
    connection_id: Mapped[int] = mapped_column(nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    object_name: Mapped[str] = mapped_column(String(200), nullable=False)
    uploaded_file_id: Mapped[int | None] = mapped_column(ForeignKey("uploaded_dataset_files.id"), nullable=True)

    # list[{name, data_type, method_override?}] — the selected columns and
    # any per-column detector override. See anomaly/engine.py (added in a
    # later phase) for the exact shape consumed at run time.
    columns: Mapped[list] = mapped_column(JSON, default=list)
    # {method: [...], params: {...}} per column or job-wide — replaces a
    # separate anomaly_rule child table, same JSON-blob convention
    # ValidationRuleSet.rules already established.
    detection_config: Mapped[dict] = mapped_column(JSON, default=dict)
    baseline_config: Mapped[dict] = mapped_column(JSON, default=dict)
    threshold_config: Mapped[dict] = mapped_column(JSON, default=dict)
    # {create_incident, mark_failed, stop} — read both by the standalone
    # run path (anomaly_service.run_job) and the pipeline-embedded path
    # (execution/pipeline_job_executor._run_anomaly_detection).
    on_critical: Mapped[dict] = mapped_column(JSON, default=dict)

    # Standalone scheduling (Manual/Hourly/Daily/Weekly/Monthly/Cron) —
    # "Pipeline Trigger" scheduling is NOT this; that's the job embedded
    # as a pipeline node instead (see models/schedule.py's
    # anomaly_job_id, added in a later phase).
    schedule_frequency: Mapped[str | None] = mapped_column(String(20), nullable=True)
    schedule_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # How long AnomalyExecution history (and its cascaded column results/
    # records) is kept for this job — enforced by anomaly_service.
    # purge_expired_executions(), called at the end of every run_job() so
    # it's a real, wired retention policy rather than a stored-but-unread
    # config value. None = keep forever (no automatic purge).
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<AnomalyJob {self.name} ({self.object_name})>"


class AnomalyExecution(TenantBase):
    __tablename__ = "anomaly_executions"
    # See PipelineJob.__table_args__'s identical comment — AnomalyColumnResult/
    # AnomalyRecord key off this numeric id, so SQLite must never reuse a
    # deleted execution's id for a new one.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("anomaly_jobs.id"), nullable=False, index=True)

    status: Mapped[AnomalyExecutionStatus] = mapped_column(Enum(AnomalyExecutionStatus), default=AnomalyExecutionStatus.QUEUED, index=True)
    overall_status: Mapped[OverallStatus | None] = mapped_column(Enum(OverallStatus), nullable=True, index=True)

    records_scanned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    columns_scanned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anomalies_found: Mapped[int] = mapped_column(Integer, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)

    # Performance instrumentation, surfaced in the Results UI (added in a
    # later phase).
    profiling_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detection_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    db_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transfer_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    trigger: Mapped[str] = mapped_column(String(20), default="Manual")  # Manual | Schedule | Pipeline
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<AnomalyExecution {self.id} job={self.job_id} status={self.status}>"


class AnomalyColumnResult(TenantBase):
    __tablename__ = "anomaly_column_results"
    __table_args__ = {"sqlite_autoincrement": True}  # AnomalyRecord keys off this id — same reasoning as above

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    execution_id: Mapped[int] = mapped_column(ForeignKey("anomaly_executions.id"), nullable=False, index=True)

    column_name: Mapped[str] = mapped_column(String(200), nullable=False)
    data_type: Mapped[str] = mapped_column(String(30), nullable=False)
    records: Mapped[int] = mapped_column(Integer, default=0)
    anomalies: Mapped[int] = mapped_column(Integer, default=0)
    anomaly_rate: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[AnomalySeverity | None] = mapped_column(Enum(AnomalySeverity), nullable=True)
    status: Mapped[ColumnResultStatus] = mapped_column(Enum(ColumnResultStatus), default=ColumnResultStatus.NOT_CHECKED, index=True)

    # {count, null_count, null_pct, distinct_count, min, max, mean, median,
    # std_dev, variance, p1, p25, p50, p75, p99, iqr, zero_count,
    # negative_count} for numeric; the string/date/boolean equivalents
    # from anomaly/profiler.py otherwise (added in a later phase).
    baseline_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    current_stats: Mapped[dict] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:
        return f"<AnomalyColumnResult {self.column_name} execution={self.execution_id} status={self.status}>"


class AnomalyRecord(TenantBase):
    """Individual anomalous values — capped per execution (see
    anomaly/engine.py's MAX_PERSISTED_RECORDS, added in a later phase),
    same ReconciliationException.exceptions_truncated pattern."""
    __tablename__ = "anomaly_records"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    execution_id: Mapped[int] = mapped_column(ForeignKey("anomaly_executions.id"), nullable=False, index=True)
    column_result_id: Mapped[int] = mapped_column(ForeignKey("anomaly_column_results.id"), nullable=False, index=True)

    record_identifier: Mapped[str | None] = mapped_column(String(200), nullable=True)  # e.g. primary key value, when known
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_range: Mapped[str | None] = mapped_column(String(300), nullable=True)
    anomaly_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # e.g. "Z_SCORE", "NULL_RATE", "DUPLICATE_KEY"

    score: Mapped[float] = mapped_column(Float, default=0.0)                   # normalized band, see anomaly/scoring.py
    raw_score: Mapped[float | None] = mapped_column(Float, nullable=True)      # the original algorithm's own score, unmodified
    severity: Mapped[AnomalySeverity] = mapped_column(Enum(AnomalySeverity), default=AnomalySeverity.LOW, index=True)

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self) -> str:
        return f"<AnomalyRecord {self.anomaly_type} column_result={self.column_result_id} severity={self.severity}>"


class AnomalyBaseline(TenantBase):
    """Versioned, never overwritten — see module docstring."""
    __tablename__ = "anomaly_baselines"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("anomaly_jobs.id"), nullable=False, index=True)
    column_name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    stats: Mapped[dict] = mapped_column(JSON, default=dict)  # same shape as AnomalyColumnResult.current_stats
    source: Mapped[BaselineSource] = mapped_column(Enum(BaselineSource), default=BaselineSource.PREVIOUS_RUN)

    approved_by: Mapped[int | None] = mapped_column(nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<AnomalyBaseline job={self.job_id} {self.column_name} v{self.version}>"


class AnomalyFeedback(TenantBase):
    """Append-only — never overwrites AnomalyRecord itself, see module
    docstring."""
    __tablename__ = "anomaly_feedback"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("anomaly_records.id"), nullable=False, index=True)

    status: Mapped[FeedbackStatus] = mapped_column(Enum(FeedbackStatus), nullable=False, index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<AnomalyFeedback record={self.record_id} {self.status}>"


class AnomalyReport(TenantBase):
    """Structural clone of SqlWorkspaceExportJob — see module docstring."""
    __tablename__ = "anomaly_reports"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    execution_id: Mapped[int] = mapped_column(ForeignKey("anomaly_executions.id"), nullable=False, index=True)

    format: Mapped[ReportFormat] = mapped_column(Enum(ReportFormat), nullable=False)
    status: Mapped[ReportStatus] = mapped_column(Enum(ReportStatus), default=ReportStatus.PENDING, index=True)

    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<AnomalyReport execution={self.execution_id} {self.format} {self.status}>"
