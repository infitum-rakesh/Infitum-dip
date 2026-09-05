"""
OrganizationSettings — Administration -> Settings.

One row per organization, one JSON blob holding every section from the
spec except the two General fields that are already real Organization
columns (name, logo_url — settings_service reads/writes those directly
rather than duplicating them in the blob).

Like Organization.licensed_modules/User.accessible_modules elsewhere in
this codebase, a single JSON column is the right shape here: ~28 fields
across 8 sections with no relational structure between them, all
validated at the Pydantic layer (schemas/settings.py) rather than with
DB constraints. DEFAULT_SETTINGS is merged under whatever is actually
stored on read, so adding a new field later never breaks existing rows.
"""
from datetime import datetime

from sqlalchemy import DateTime, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase

DEFAULT_SETTINGS = {
    "general": {
        "timezone": "UTC",
        "date_format": "YYYY-MM-DD",
        "default_language": "en",
    },
    "data": {
        "default_environment": "DEVELOPMENT",
        "default_data_retention_days": 365,
        "max_upload_size_mb": 25,
        "default_dataset_visibility": "ORG",
    },
    "document_intelligence": {
        # "tesseract" is the only value that needs no Connection — it's the
        # built-in local engine (ocr/engine.py). The other three select a
        # real cloud OCR Provider connection (see connectors/ocr/*) — if
        # none is configured for the org, OCR fails with a real error
        # rather than silently falling back to Tesseract (see
        # services/document_service.py's _run_ocr()/_pick_ocr_connection()).
        "ocr_provider": "tesseract",
        "default_ocr_language": "en",
        "default_confidence_threshold": 85,
    },
    "ai": {
        "ai_provider": "openai",
        "default_model": "gpt-4o",
        "temperature": 0.2,
        "default_extraction_confidence_threshold": 85,
    },
    "notifications": {
        "email_notifications_enabled": True,
        "notify_pipeline_failure": True,
        "notify_validation_failure": True,
        "notify_reconciliation_failure": True,
        "notify_review_queue": False,
        "notify_incident_alerts": True,
    },
    "security": {
        "session_timeout_minutes": 60,
        "password_policy": {"min_length": 8, "require_uppercase": True, "require_number": True, "require_symbol": False},
        "mfa": "OPTIONAL",  # OFF | OPTIONAL | REQUIRED
        "ip_restrictions": [],
    },
    "audit": {
        "audit_retention_days": 365,
        "audit_logging_level": "STANDARD",  # MINIMAL | STANDARD | VERBOSE
    },
    "pipelines": {
        "default_retry_count": 2,
        "default_timeout_seconds": 3600,
        "max_concurrent_pipelines": 3,
    },
    # AD9 — org-wide defaults an Anomaly Job editor pre-fills a NEW job
    # with (see AnomalyJobEditor.jsx's create path and
    # anomaly_service.get_configuration/update_configuration). Real
    # wiring, not just "stored + displayed": unlike most sections above,
    # a new anomaly job's threshold_config/baseline_config/retention_days/
    # on_critical actually read these on creation. Exposed both here
    # (generic Settings admin path) and via a dedicated
    # /anomaly/configuration route pair gated on ANOMALY_DETECTION
    # permissions rather than Settings — an anomaly-detection editor
    # shouldn't need Settings access just to set these defaults.
    "anomaly_detection": {
        # Same shape as anomaly/scoring.py's DEFAULT_BANDS / bands_from_config's
        # expected threshold_config["score_bands"]: ascending "upper" bounds,
        # last one is the ceiling regardless of its stated value.
        "default_score_bands": [
            {"upper": 1.0, "label": "NORMAL"},
            {"upper": 3.0, "label": "LOW"},
            {"upper": 5.0, "label": "MEDIUM"},
            {"upper": 8.0, "label": "HIGH"},
            {"upper": 999999999, "label": "CRITICAL"},
        ],
        "default_retention_days": None,
        "default_baseline_source": "PREVIOUS_RUN",
        "default_require_baseline_approval": False,
        "default_create_incident_on_critical": False,
    },
}


class OrganizationSettings(TenantBase):
    __tablename__ = "organization_settings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, unique=True, index=True)

    values: Mapped[dict] = mapped_column(JSON, default=dict)

    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<OrganizationSettings org={self.organization_id}>"
