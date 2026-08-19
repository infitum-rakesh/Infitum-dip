from datetime import datetime

from pydantic import BaseModel


# ── Section shapes (mirror models/settings.py DEFAULT_SETTINGS exactly) ──

class GeneralSettings(BaseModel):
    timezone: str
    date_format: str
    default_language: str


class DataSettings(BaseModel):
    default_environment: str
    default_data_retention_days: int
    max_upload_size_mb: int
    default_dataset_visibility: str


class DocumentIntelligenceSettings(BaseModel):
    ocr_provider: str
    default_ocr_language: str
    default_confidence_threshold: float


class AISettings(BaseModel):
    ai_provider: str
    default_model: str
    temperature: float
    default_extraction_confidence_threshold: float


class NotificationSettings(BaseModel):
    email_notifications_enabled: bool
    notify_pipeline_failure: bool
    notify_validation_failure: bool
    notify_reconciliation_failure: bool
    notify_review_queue: bool
    notify_incident_alerts: bool


class PasswordPolicy(BaseModel):
    min_length: int
    require_uppercase: bool
    require_number: bool
    require_symbol: bool


class SecuritySettings(BaseModel):
    session_timeout_minutes: int
    password_policy: PasswordPolicy
    mfa: str  # OFF | OPTIONAL | REQUIRED
    ip_restrictions: list[str]


class AuditSettings(BaseModel):
    audit_retention_days: int
    audit_logging_level: str  # MINIMAL | STANDARD | VERBOSE


class PipelineSettings(BaseModel):
    default_retry_count: int
    default_timeout_seconds: int
    max_concurrent_pipelines: int


# ── Full response ────────────────────────────────────────────────

class OrganizationSettingsOut(BaseModel):
    organization_id: int
    organization_name: str
    logo_url: str | None

    general: GeneralSettings
    data: DataSettings
    document_intelligence: DocumentIntelligenceSettings
    ai: AISettings
    notifications: NotificationSettings
    security: SecuritySettings
    audit: AuditSettings
    pipelines: PipelineSettings

    updated_by: int | None
    updated_by_name: str | None = None
    updated_at: datetime


# ── Update request — every section optional and itself partial; only the
# section(s) actually sent get merged and persisted. ───────────────────

class UpdateSettingsRequest(BaseModel):
    organization_name: str | None = None
    logo_url: str | None = None

    general: dict | None = None
    data: dict | None = None
    document_intelligence: dict | None = None
    ai: dict | None = None
    notifications: dict | None = None
    security: dict | None = None
    audit: dict | None = None
    pipelines: dict | None = None
