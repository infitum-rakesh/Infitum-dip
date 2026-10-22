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
        # default_ocr_language/default_confidence_threshold predate the
        # Multilingual Document Processing feature and are kept for
        # backward compatibility (a real repo-analysis finding: this field
        # was never actually read anywhere in the OCR path before Phase 3
        # — see ocr/language_config.py's own docstring). The fields below
        # are the real, wired multilingual configuration surface
        # (Phase 3) — see ocr/language_config.py for the language-code
        # vocabulary (ISO 639-1: "en"/"hi"/"kn"/"ta"/...).
        "default_ocr_language": "en",
        "default_confidence_threshold": 85,
        # Languages this org has explicitly opted into for OCR/extraction
        # — an org must add a language here before documents in it are
        # processed with anything beyond English + script-detection.
        # "en" ships enabled by default so existing orgs' behavior is
        # unchanged until they explicitly opt into more.
        "supported_languages": ["en"],
        # "auto" = real hybrid language detection (ocr/language_detection/)
        # picks the OCR language per page; any other value pins every
        # document to that one language, skipping detection entirely.
        "primary_language": "auto",
        "ocr_confidence_threshold": 85,
        # Below this, script-block detection disagreeing with the
        # configured/expected language is flagged OCR_LANGUAGE_MISMATCH
        # (ocr/language_errors.py) rather than silently accepted.
        "language_mismatch_threshold": 80,
        # Optional and OFF by default (spec requirement: the platform must
        # work completely without translation) — no translation capability
        # exists yet as of Phase 3; this is the switch a later phase's
        # real implementation will read.
        "translation_enabled": False,
        # Generic Extraction (Document Intelligence: Dynamic JSON
        # Extraction spec) — OFF by default, a deliberate controlled
        # rollout: this asks the AI model for additional evidence-
        # supported key-value pairs beyond the known/canonical schema
        # (see extraction_service.find_gap_fields()). Real, wired
        # implementation, gated per-org so it can be validated on
        # representative documents before broader enablement, per the
        # spec's "introduce generic extraction behind a controlled
        # rollout or feature flag" requirement.
        "generic_extraction_enabled": False,
        # Document Mapping Profiles (Document Intelligence: Dynamic JSON
        # Extraction spec) — OFF by default, same controlled-rollout
        # reasoning as generic_extraction_enabled above. Gates the
        # "Apply Mapping Profile" action (services/document_mapping_
        # service.py::apply_mapping_profile_to_document()) — the mapping
        # PROFILES themselves (services/mapping_service.py, reusing the
        # existing Column Mapping versioning system) can still be
        # created/versioned/published regardless of this flag; only
        # running one against a real document is gated, so an org can
        # author and validate a profile before it's ever actually applied.
        "mapping_profiles_enabled": False,
        # Tally Invoice Auto-Sync — OFF by default, same controlled-rollout
        # reasoning as the two flags above. When on, every invoice that
        # reaches the decision engine's STRAIGHT_THROUGH outcome (document_
        # service.py, right after invoice_decision_service.decide(), the
        # exact same gate INV11's ERP auto-post already uses) attempts a
        # real Tally voucher preview + submit-for-approval, provided this
        # org has a TallyVendorMapping configured for that invoice's vendor
        # (or an org-wide default row) — see tally_invoice_sync_service.py.
        # No mapping configured = no-op, regardless of this flag, so
        # turning this on alone changes nothing until an admin also sets
        # up at least one vendor mapping.
        "tally_auto_sync_enabled": False,
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
