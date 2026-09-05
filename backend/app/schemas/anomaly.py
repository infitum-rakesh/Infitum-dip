"""Anomaly Detection schemas — AD1 covers AnomalyJob CRUD; AD3 adds
execution/column-result/record schemas alongside the run engine; AD4 adds
baseline-approval/feedback schemas; AD5 adds report schemas."""
from datetime import datetime

from pydantic import BaseModel


class AnomalyJobCreateRequest(BaseModel):
    name: str
    description: str | None = None
    # 0 is the real "not a real Connection — see uploaded_file_id instead"
    # sentinel, same convention Reconciliation/Validation use.
    connection_id: int
    schema_name: str | None = None
    object_name: str
    uploaded_file_id: int | None = None
    columns: list[dict] = []
    detection_config: dict = {}
    baseline_config: dict = {}
    threshold_config: dict = {}
    on_critical: dict = {}
    schedule_frequency: str | None = None
    schedule_cron: str | None = None
    enabled: bool = True
    retention_days: int | None = None


class AnomalyJobUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    columns: list[dict] | None = None
    detection_config: dict | None = None
    baseline_config: dict | None = None
    threshold_config: dict | None = None
    on_critical: dict | None = None
    schedule_frequency: str | None = None
    schedule_cron: str | None = None
    enabled: bool | None = None
    retention_days: int | None = None


class AnomalyJobOut(BaseModel):
    id: int
    name: str
    description: str | None
    connection_id: int
    schema_name: str | None
    object_name: str
    uploaded_file_id: int | None = None
    columns: list[dict]
    detection_config: dict
    baseline_config: dict
    threshold_config: dict
    on_critical: dict
    schedule_frequency: str | None
    schedule_cron: str | None
    enabled: bool
    retention_days: int | None = None
    created_by: int | None
    created_by_name: str | None = None
    updated_by: int | None
    updated_by_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AnomalyJobListOut(BaseModel):
    items: list[AnomalyJobOut]
    total: int


class AnomalyProfilePreviewRequest(BaseModel):
    connection_id: int
    schema_name: str | None = None
    object_name: str
    uploaded_file_id: int | None = None
    columns: list[dict]  # [{"name": ..., "data_type": ...}, ...]


# ── Run ──────────────────────────────────────────────────────────

class AnomalyExecutionOut(BaseModel):
    id: int
    job_id: int
    status: str
    overall_status: str | None
    records_scanned: int | None
    columns_scanned: int | None
    anomalies_found: int
    critical_count: int
    warning_count: int
    profiling_ms: int | None
    detection_ms: int | None
    db_ms: int | None
    transfer_ms: int | None
    total_ms: int | None
    trigger: str
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_by: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AnomalyColumnResultOut(BaseModel):
    id: int
    execution_id: int
    column_name: str
    data_type: str
    records: int
    anomalies: int
    anomaly_rate: float
    severity: str | None
    status: str
    baseline_stats: dict
    current_stats: dict

    model_config = {"from_attributes": True}


class AnomalyRecordOut(BaseModel):
    id: int
    execution_id: int
    column_result_id: int
    record_identifier: str | None
    value: str | None
    expected_range: str | None
    anomaly_type: str
    score: float
    raw_score: float | None
    severity: str
    detected_at: datetime

    model_config = {"from_attributes": True}


class AnomalyRecordListOut(BaseModel):
    items: list[AnomalyRecordOut]
    total: int


# ── Baseline approval ────────────────────────────────────────────

class AnomalyBaselineOut(BaseModel):
    id: int
    job_id: int
    column_name: str
    version: int
    stats: dict
    source: str
    approved_by: int | None
    approved_by_name: str | None = None
    approved_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Feedback ─────────────────────────────────────────────────────

class AnomalyFeedbackCreateRequest(BaseModel):
    status: str  # one of FeedbackStatus: TRUE_ANOMALY | FALSE_POSITIVE | EXPECTED | IGNORED
    comment: str | None = None


class AnomalyFeedbackOut(BaseModel):
    id: int
    record_id: int
    status: str
    comment: str | None
    created_by: int | None
    created_by_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Reports ──────────────────────────────────────────────────────

class AnomalyReportCreateRequest(BaseModel):
    format: str  # one of ReportFormat: CSV | XLSX | JSON | PDF


# ── Org-level configuration (AD9) ────────────────────────────────

class AnomalyConfigurationOut(BaseModel):
    default_score_bands: list[dict]
    default_retention_days: int | None
    default_baseline_source: str
    default_require_baseline_approval: bool
    default_create_incident_on_critical: bool


class AnomalyConfigurationUpdateRequest(BaseModel):
    default_score_bands: list[dict] | None = None
    default_retention_days: int | None = None
    default_baseline_source: str | None = None
    default_require_baseline_approval: bool | None = None
    default_create_incident_on_critical: bool | None = None


class AnomalyReportOut(BaseModel):
    id: int
    execution_id: int
    format: str
    status: str
    file_path: str | None
    file_size_bytes: int | None
    row_count: int | None
    error_message: str | None
    created_by: int | None
    created_by_name: str | None = None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}
