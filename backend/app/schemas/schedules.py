from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator, model_validator


# ── Scheduled Document Intelligence Workflow ──────────────────────
# Presence of `document_config` on a Schedule (in/out) is what makes it a
# document-source schedule — see schedule_service.execute_schedule()'s
# dispatch branch. Every field here mirrors DocumentScheduleConfig 1:1.

class DocumentScheduleConfigIn(BaseModel):
    source_connection_id: int

    # PAY18/PAY20 — "FILE" (default, unchanged), "DATABASE"
    # (source_connection_id is a DATABASE-category connection;
    # source_query runs via that connection's own preview_sql(), one
    # Document per returned row — see document_schedule_service.
    # discover_database_records()), or "API" (source_connection_id is an
    # API-category connection; the source_api_* fields below run via that
    # connection's own fetch_api(), one Document per decoded record — see
    # document_schedule_service.discover_api_records()). source_path stays
    # required for every kind; for DATABASE/API it's a free-text label
    # rather than a filesystem path.
    source_kind: str = "FILE"
    source_query: str | None = None
    source_record_key_column: str | None = None

    # PAY20 — API-kind fields, mirroring connectors/base.py's fetch_api()
    # kwargs directly (see that method's docstring for the pagination
    # dict's contract and json_path's dot-path envelope-extraction syntax).
    source_api_endpoint: str | None = None
    source_api_method: str = "GET"
    source_api_query_params: dict[str, Any] | None = None
    source_api_pagination: dict[str, Any] | None = None
    source_api_json_path: str | None = None

    # PAY21 — ERP-kind's one field: an optional Oracle REST query-language
    # filter string passed through unmodified to that connection's own
    # list_invoices() (see document_schedule_service.discover_erp_records()).
    # Blank means "no filter". No endpoint field is needed for ERP-kind —
    # unlike API-kind, the source_connection_id itself (an ORACLE_ERP_CLOUD
    # connection) already fully identifies the one resource being pulled.
    source_erp_query: str | None = None

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

    # Scheduled Mapping (Document Intelligence: Dynamic JSON Extraction
    # spec, section 7.1/7.2) — the EXPLICIT mapping profile this schedule
    # applies after extraction succeeds. None means "fall through to the
    # organisation's document-type default, if any" at resolution time
    # (document_mapping_resolution_service.resolve_scheduled_mapping()) —
    # never resolved here, only existence/compatibility-checked (see
    # _validate_and_apply_document_config in schedule_service.py), since a
    # mapping published later or re-published after this schedule is saved
    # must still work without re-saving the schedule.
    mapping_id: int | None = None

    @field_validator("source_kind")
    @classmethod
    def _source_kind_valid(cls, v: str) -> str:
        v = (v or "FILE").strip().upper()
        if v not in ("FILE", "DATABASE", "API", "ERP"):
            raise ValueError("Source Kind must be FILE, DATABASE, API, or ERP.")
        return v

    @field_validator("source_path")
    @classmethod
    def _source_path_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Source path is required.")
        return v.strip()

    @field_validator("source_query")
    @classmethod
    def _source_query_normalize(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None

    @field_validator("source_record_key_column")
    @classmethod
    def _source_record_key_column_normalize(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None

    @field_validator("source_api_endpoint", "source_api_json_path", "source_erp_query")
    @classmethod
    def _source_api_str_normalize(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None

    @field_validator("source_api_method")
    @classmethod
    def _source_api_method_valid(cls, v: str) -> str:
        v = (v or "GET").strip().upper()
        if v not in ("GET", "POST"):
            raise ValueError("API method must be GET or POST.")
        return v

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

    @model_validator(mode="after")
    def _database_source_requires_query(self) -> "DocumentScheduleConfigIn":
        # PAY18 — a DATABASE-kind schedule has no OCR/AI Extraction stage
        # to fall back on (see document_service.build_fields_from_record()):
        # its query's own column names ARE the mapping, so a document_type
        # is required up front rather than left to auto-detect. Only
        # "INVOICE" has a real taxonomy wired to this path today (see
        # document_taxonomies.TAXONOMIES) — a query result would otherwise
        # materialize as untyped generic fields with nothing downstream to
        # do with them, so this is enforced rather than silently allowed.
        if self.source_kind == "DATABASE":
            if not self.source_query:
                raise ValueError("A DATABASE source requires a SQL query.")
            if self.document_type != "INVOICE":
                raise ValueError("A DATABASE source currently requires Document Type to be set to INVOICE.")
        return self

    @model_validator(mode="after")
    def _api_source_requires_endpoint(self) -> "DocumentScheduleConfigIn":
        # PAY20 — same reasoning as _database_source_requires_query above:
        # an API-kind schedule has no OCR/AI Extraction stage either, so
        # the endpoint (whose decoded record IS the mapping) and a pinned
        # INVOICE document_type are both required up front.
        if self.source_kind == "API":
            if not self.source_api_endpoint:
                raise ValueError("An API source requires an endpoint.")
            if self.document_type != "INVOICE":
                raise ValueError("An API source currently requires Document Type to be set to INVOICE.")
        return self

    @model_validator(mode="after")
    def _erp_source_requires_invoice(self) -> "DocumentScheduleConfigIn":
        # PAY21 — same reasoning as _database_source_requires_query/
        # _api_source_requires_endpoint above: an ERP-kind schedule has no
        # OCR/AI Extraction stage either. No endpoint check here — unlike
        # API-kind, source_connection_id alone (an ORACLE_ERP_CLOUD
        # connection) already fully identifies the one resource pulled.
        if self.source_kind == "ERP" and self.document_type != "INVOICE":
            raise ValueError("An ERP source currently requires Document Type to be set to INVOICE.")
        return self


class DocumentScheduleConfigOut(DocumentScheduleConfigIn):
    model_config = {"from_attributes": True}


# ── TALLY-J: Scheduled/batch Tally reconciliation ──────────────────
# Presence of `tally_config` on a Schedule (in/out) is what makes it a
# Tally-reconciliation schedule — see schedule_service.execute_schedule()'s
# dispatch branch and TallyReconciliationScheduleConfig's own docstring.

class TallyReconciliationScheduleConfigIn(BaseModel):
    connection_id: int
    company_name: str
    tolerance: float = 0.02
    lookback_days: int = 7

    @field_validator("company_name")
    @classmethod
    def _company_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Tally company name is required.")
        return v.strip()

    @field_validator("lookback_days")
    @classmethod
    def _lookback_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Lookback days must be at least 1.")
        return v


class TallyReconciliationScheduleConfigOut(TallyReconciliationScheduleConfigIn):
    model_config = {"from_attributes": True}


# Presence of `india_verification_config` on a Schedule (in/out) is what
# makes it a scheduled India Verification expired-check advisory sweep —
# see schedule_service.execute_schedule()'s dispatch branch and
# verification_job_service.create_refresh_job_for_expired()'s docstring
# for why this is an advisory sweep, not a blind auto-reverify.

class IndiaVerificationScheduleConfigIn(BaseModel):
    business_process: str
    check_types: list[str] | None = None

    @field_validator("business_process")
    @classmethod
    def _business_process_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("business_process is required.")
        return v.strip()


class IndiaVerificationScheduleConfigOut(IndiaVerificationScheduleConfigIn):
    model_config = {"from_attributes": True}


# ── Responses ────────────────────────────────────────────────────

class ScheduleOut(BaseModel):
    id: int
    name: str
    pipeline_id: str
    pipeline_name: str
    anomaly_job_id: int | None = None
    document_config: DocumentScheduleConfigOut | None = None
    tally_config: TallyReconciliationScheduleConfigOut | None = None
    india_verification_config: IndiaVerificationScheduleConfigOut | None = None
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
    # PAY17 — approval-outcome counters, populated only for document_type=
    # "INVOICE" schedules (schedule_service._classify_invoice_approval_
    # outcome()). Real bug found live: this schema is a declared Pydantic
    # model, not a passthrough dict, so the 5 new keys schedule_service.py
    # already wrote into ScheduleRun.document_stats were being silently
    # dropped on serialization — the API returned every pre-existing stat
    # correctly but always 0 for these, even when the DB row itself held
    # the real, non-zero counts. Caught by browser verification, not by
    # the backend test suite (which asserts against the ORM row directly,
    # never through this schema).
    auto_approved: int = 0
    workflows_created: int = 0
    already_in_workflow: int = 0
    rule_conflicts: int = 0
    approver_not_found: int = 0


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
    tally_config: TallyReconciliationScheduleConfigIn | None = None  # TALLY-J — same server-side override, mutually exclusive with the two above
    india_verification_config: IndiaVerificationScheduleConfigIn | None = None  # Phase Group 4 (IV21) — same server-side override, mutually exclusive with the three above
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
    tally_config: TallyReconciliationScheduleConfigIn | None = None
    india_verification_config: IndiaVerificationScheduleConfigIn | None = None
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
