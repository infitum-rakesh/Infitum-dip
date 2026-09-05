from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator, model_validator


# ── Scheduled Document Intelligence Workflow ──────────────────────
# Presence of `document_config` on a Schedule (in/out) is what makes it a
# document-source schedule — see schedule_service.execute_schedule()'s
# dispatch branch. Every field here mirrors DocumentScheduleConfig 1:1.

class DocumentScheduleConfigIn(BaseModel):
    source_connection_id: int
    source_path: str
    file_pattern: str = "*"
    recursive_scan: bool = False
    batch_size: int = 500
    max_concurrency: int = 3
    confidence_threshold: float = 90.0
    accuracy_threshold: float = 90.0
    archive_after_success: bool = False
    archive_path: str | None = None
    move_failed_to_review_path: str | None = None
    duplicate_content_behavior: str = "SKIP_DUPLICATE"  # SKIP_DUPLICATE | PROCESS_AS_SEPARATE_DOCUMENT
    max_file_size_mb: int = 100

    # Pins scheduled intake to a known document_taxonomies type (e.g.
    # "INVOICE") instead of ad-hoc auto-detect — see DocumentScheduleConfig
    # model docstring (INV11). None keeps today's auto-detect behavior.
    document_type: str | None = None
    # INV11 — auto-post a STRAIGHT_THROUGH invoice decision straight to the
    # ERP with no human step. Only meaningful when document_type ==
    # "INVOICE"; see _validate_and_apply_document_config's cross-field check.
    invoice_auto_post: bool = False
    invoice_erp_connection_id: int | None = None

    @field_validator("source_path")
    @classmethod
    def _source_path_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Source path is required.")
        return v.strip()

    @field_validator("batch_size", "max_concurrency", "max_file_size_mb")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Must be greater than 0.")
        return v

    @field_validator("confidence_threshold", "accuracy_threshold")
    @classmethod
    def _threshold_range(cls, v: float) -> float:
        if not (0 <= v <= 100):
            raise ValueError("Must be between 0 and 100.")
        return v

    @field_validator("duplicate_content_behavior")
    @classmethod
    def _duplicate_behavior_valid(cls, v: str) -> str:
        if v not in ("SKIP_DUPLICATE", "PROCESS_AS_SEPARATE_DOCUMENT"):
            raise ValueError("Must be SKIP_DUPLICATE or PROCESS_AS_SEPARATE_DOCUMENT.")
        return v

    @field_validator("document_type")
    @classmethod
    def _document_type_normalize(cls, v: str | None) -> str | None:
        # Blank string from a cleared frontend dropdown means "no pin", same
        # as never sending the field — normalize here so the model column
        # stays genuinely NULL rather than an empty string.
        v = (v or "").strip()
        return v or None

    @model_validator(mode="after")
    def _auto_post_requires_invoice_and_connection(self) -> "DocumentScheduleConfigIn":
        if self.invoice_auto_post:
            if self.document_type != "INVOICE":
                raise ValueError("Auto-post to ERP requires Document Type to be set to INVOICE.")
            if not self.invoice_erp_connection_id:
                raise ValueError("Auto-post to ERP requires an ERP connection to be selected.")
        return self


class DocumentScheduleConfigOut(DocumentScheduleConfigIn):
    model_config = {"from_attributes": True}


# ── Responses ────────────────────────────────────────────────────

class ScheduleOut(BaseModel):
    id: int
    name: str
    pipeline_id: str
    pipeline_name: str
    anomaly_job_id: int | None = None
    document_config: DocumentScheduleConfigOut | None = None
    environment: str
    timezone: str
    frequency: str
    frequency_display: str  # e.g. "Every Day 02:00 AM" — computed, matches spec's example row
    start_at: datetime
    cron_expression: str | None
    status: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    retry_count: int
    retry_interval_seconds: int
    timeout_seconds: int
    concurrency: int
    notify_on_failure: bool
    notify_on_success: bool
    notification_emails: str | None
    owner_id: int | None
    owner_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScheduleListOut(BaseModel):
    items: list[ScheduleOut]
    total: int
    page: int
    page_size: int


class ScheduleStatsOut(BaseModel):
    active: int
    paused: int
    running_today: int
    failed_today: int
    next_run_at: datetime | None
    next_run_schedule_name: str | None


class RunLogEntry(BaseModel):
    timestamp: datetime
    level: str
    message: str


class DocumentStatsOut(BaseModel):
    discovered: int = 0
    queued: int = 0
    processed: int = 0
    success: int = 0
    review: int = 0
    failed: int = 0
    skipped: int = 0
    unsupported: int = 0
    current_file: str | None = None
    current_stage: str | None = None


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
    cancel_requested: bool = False
    document_stats: DocumentStatsOut | None = None

    model_config = {"from_attributes": True}


class ScheduleRunDetailOut(ScheduleRunOut):
    logs: list[RunLogEntry]


class ScheduleRunListOut(BaseModel):
    items: list[ScheduleRunOut]
    total: int


class DocumentRegistryRowOut(BaseModel):
    id: int
    file_name: str
    source_path: str
    file_size: int
    content_hash: str
    status: str
    processing_attempts: int
    last_error: str | None
    document_id: int | None
    discovered_at: datetime
    first_processed_at: datetime | None
    last_processed_at: datetime | None

    model_config = {"from_attributes": True}


class DocumentRegistryListOut(BaseModel):
    items: list[DocumentRegistryRowOut]
    total: int


# ── Requests ─────────────────────────────────────────────────────

class ScheduleCreateRequest(BaseModel):
    name: str
    pipeline_id: str = ""
    pipeline_name: str = ""
    anomaly_job_id: int | None = None  # AD12 — when set, pipeline_id/pipeline_name are overridden server-side (see schedule_service.create_schedule)
    document_config: DocumentScheduleConfigIn | None = None  # when set, pipeline_id/pipeline_name are overridden server-side the same way anomaly_job_id already is
    environment: str = "DEVELOPMENT"
    timezone: str = "UTC"
    frequency: str
    start_date: str  # "YYYY-MM-DD"
    start_time: str  # "HH:MM"
    cron_expression: str | None = None
    retry_count: int = 0
    retry_interval_seconds: int = 60
    timeout_seconds: int = 3600
    concurrency: int = 1
    notify_on_failure: bool = True
    notify_on_success: bool = False
    notification_emails: str | None = None


class ScheduleUpdateRequest(BaseModel):
    name: str | None = None
    pipeline_id: str | None = None
    pipeline_name: str | None = None
    document_config: DocumentScheduleConfigIn | None = None
    environment: str | None = None
    timezone: str | None = None
    frequency: str | None = None
    start_date: str | None = None
    start_time: str | None = None
    cron_expression: str | None = None
    retry_count: int | None = None
    retry_interval_seconds: int | None = None
    timeout_seconds: int | None = None
    concurrency: int | None = None
    notify_on_failure: bool | None = None
    notify_on_success: bool | None = None
    notification_emails: str | None = None
