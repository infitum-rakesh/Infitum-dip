from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Overview / Pipeline Runs / Job detail ──

class JobSummaryOut(BaseModel):
    id: int
    pipeline_id: int
    pipeline_name: str
    pipeline_version: int
    status: str
    trigger: str
    records_in: int | None = None
    records_out: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    error_message: str | None = None
    records_rejected: int | None = None
    sla_status: str | None = None

    model_config = {"from_attributes": True}


class OverviewOut(BaseModel):
    jobs_total: int
    jobs_succeeded: int
    jobs_failed: int
    jobs_running: int
    loads_total: int
    loads_failed: int
    validations_failed: int
    reconciliations_failed: int
    incidents_open: int
    records_processed: int
    anomalies_critical: int
    anomalies_warning: int
    invoices_dead_letter: int
    invoices_reconciliation_mismatch: int
    success_rate_pct: float | None
    recent_jobs: list[JobSummaryOut]
    window_days: int


class PipelineRunsOut(BaseModel):
    items: list[JobSummaryOut]
    total: int
    page: int
    page_size: int


class ScheduleRunOut(BaseModel):
    id: int
    schedule_id: int
    status: str
    trigger: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None
    records_processed: int
    records_failed: int
    error_message: str | None


class ScheduleRunsOut(BaseModel):
    items: list[ScheduleRunOut]
    total: int
    page: int
    page_size: int


class JobDetailOut(JobSummaryOut):
    node_stats: dict[str, Any] = {}
    sla_reason: str | None = None


# ── Data Quality ──

class ValidationRunSummaryOut(BaseModel):
    id: int
    rule_set_id: int
    status: str
    total_records: int | None
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None


class ReconciliationRunSummaryOut(BaseModel):
    id: int
    job_id: int
    status: str
    source_count: int | None
    target_count: int | None
    matched_count: int | None
    missing_count: int | None
    extra_count: int | None
    mismatched_count: int | None
    started_at: datetime | None
    completed_at: datetime | None


class AnomalyExecutionSummaryOut(BaseModel):
    id: int
    job_id: int
    status: str
    overall_status: str | None
    records_scanned: int | None
    anomalies_found: int
    critical_count: int
    warning_count: int
    trigger: str
    started_at: datetime | None
    completed_at: datetime | None


class InvoiceErpDeadLetterSummaryOut(BaseModel):
    id: int
    invoice_id: int
    invoice_number: str | None
    erp_connection_id: int | None
    attempt_number: int
    error_code: str | None
    error_message: str | None
    created_at: datetime


class InvoiceReconciliationMismatchSummaryOut(BaseModel):
    id: int
    invoice_id: int
    invoice_number: str | None
    mismatched_fields: list[str]
    created_at: datetime


class DataQualityOut(BaseModel):
    validation_runs: list[ValidationRunSummaryOut]
    validation_total: int
    reconciliation_runs: list[ReconciliationRunSummaryOut]
    reconciliation_total: int
    anomaly_executions: list[AnomalyExecutionSummaryOut]
    anomaly_total: int
    invoice_erp_dead_letters: list[InvoiceErpDeadLetterSummaryOut]
    invoice_erp_dead_letter_total: int
    invoice_reconciliation_mismatches: list[InvoiceReconciliationMismatchSummaryOut]
    invoice_reconciliation_mismatch_total: int
    page: int
    page_size: int


# ── Invoice Processing (INV21) ──

class InvoiceAnomalyFindingOut(BaseModel):
    invoice_id: int
    invoice_number: str | None
    downgraded_to_review: bool
    created_at: datetime
    findings: list[dict[str, Any]]


class InvoiceProcessingOut(BaseModel):
    decided_total: int
    straight_through_count: int
    review_required_count: int
    reject_count: int
    retry_count: int
    straight_through_rate_pct: float | None
    erp_calls_total: int
    erp_succeeded: int
    erp_failed: int
    erp_dead_letter: int
    erp_retry_attempts: int
    erp_in_flight: int
    review_queue_depth: int
    avg_decision_to_erp_submit_seconds: float | None
    avg_erp_submit_to_succeeded_seconds: float | None
    avg_succeeded_to_reconciled_seconds: float | None
    anomaly_checks_run: int
    anomaly_findings_total: int
    anomaly_critical_count: int
    anomaly_downgraded_count: int
    recent_anomaly_findings: list[InvoiceAnomalyFindingOut]
    window_days: int


class TallyOverviewOut(BaseModel):
    extraction_runs_total: int
    extraction_completed: int
    extraction_failed: int
    extraction_records_fetched: int
    extraction_in_flight: int
    reconciliation_runs_total: int
    reconciliation_succeeded: int
    reconciliation_failed: int
    scheduled_reconciliation_runs: int
    open_exceptions: int
    exception_counts: dict[str, int]
    tally_review_queue_depth: int
    writeback_total: int
    writeback_succeeded: int
    writeback_dead_letter: int
    writeback_failed: int
    writeback_in_flight: int
    window_days: int


# ── Performance ──

class PerformanceStageOut(BaseModel):
    node_id: str
    label: str
    duration_ms: int | None = None
    rows_in: int | None = None
    rows_out: int | None = None


class PerformancePointOut(BaseModel):
    job_id: int
    pipeline_id: int
    pipeline_name: str
    completed_at: datetime | None
    duration_seconds: float
    rows_per_sec: float | None
    records_out: int | None
    threshold: str
    stages: list[PerformanceStageOut]


class PerformanceOut(BaseModel):
    points: list[PerformancePointOut]
    avg_duration_seconds: float | None
    window_days: int
    note: str


# ── System Health ──

class HealthCheckOut(BaseModel):
    component: str
    status: str
    latency_ms: float | None = None
    detail: str | None = None


class SystemHealthOut(BaseModel):
    checks: list[HealthCheckOut]
    checked_at: datetime


# ── SLA Definitions ──

class SlaDefinitionCreateRequest(BaseModel):
    pipeline_id: int
    expected_start_time: str | None = None
    expected_completion_time: str | None = None
    max_duration_minutes: int | None = None
    warning_threshold_pct: int | None = 80
    max_failure_count: int | None = 1
    enabled: bool | None = True
    auto_create_incident: bool | None = True


class SlaDefinitionUpdateRequest(BaseModel):
    expected_start_time: str | None = None
    expected_completion_time: str | None = None
    max_duration_minutes: int | None = None
    warning_threshold_pct: int | None = None
    max_failure_count: int | None = None
    enabled: bool | None = None
    auto_create_incident: bool | None = None


class SlaDefinitionOut(BaseModel):
    id: int
    pipeline_id: int
    pipeline_name: str
    expected_start_time: str | None
    expected_completion_time: str | None
    max_duration_minutes: int | None
    warning_threshold_pct: int
    max_failure_count: int
    enabled: bool
    auto_create_incident: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SlaDefinitionListOut(BaseModel):
    items: list[SlaDefinitionOut]


# ── Alert Rules ──

class AlertRuleCreateRequest(BaseModel):
    name: str
    description: str | None = None
    condition: str
    pipeline_id: int | None = None
    threshold: float | None = None
    severity: str | None = "MEDIUM"
    cooldown_minutes: int | None = 0
    channels: list[dict] = []
    enabled: bool | None = True


class AlertRuleUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    condition: str | None = None
    pipeline_id: int | None = None
    threshold: float | None = None
    severity: str | None = None
    cooldown_minutes: int | None = None
    channels: list[dict] | None = None
    enabled: bool | None = None


class AlertRuleOut(BaseModel):
    id: int
    name: str
    description: str | None
    condition: str
    pipeline_id: int | None
    threshold: float | None
    severity: str
    cooldown_minutes: int
    channels: list[dict]
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AlertRuleListOut(BaseModel):
    items: list[AlertRuleOut]


class AlertEventOut(BaseModel):
    id: int
    rule_id: int
    rule_name: str
    condition: str
    severity: str
    message: str
    pipeline_id: int | None
    pipeline_name: str | None
    job_id: int | None
    incident_id: int | None
    status: str
    delivery_results: list[dict]
    triggered_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class AlertEventListOut(BaseModel):
    items: list[AlertEventOut]
    total: int
    page: int
    page_size: int


class AvailableChannelsOut(BaseModel):
    types: list[str]
