"""
Incident models — centralized monitoring of failures/anomalies across the
platform (Pipeline, Connection, OCR, AI Extraction, Transformation,
Validation, Reconciliation, Schedule, Data Quality, Anomaly Detection).

Additive module: no FKs into pipelines/connections/schedules/etc — those
are referenced by id/name only (mirrors models/schedule.py's own note on
why it references pipelines the same way), so this never has to modify
those other modules to interoperate with them. Any of those services CAN
call incident_service.report_incident(...) later to auto-file an incident,
but none of them are wired up here — see incident_service.py docstring.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class IncidentSource(str, enum.Enum):
    PIPELINE = "PIPELINE"
    CONNECTION = "CONNECTION"
    OCR = "OCR"
    AI_EXTRACTION = "AI_EXTRACTION"
    TRANSFORMATION = "TRANSFORMATION"
    VALIDATION = "VALIDATION"
    RECONCILIATION = "RECONCILIATION"
    SCHEDULE = "SCHEDULE"
    DATA_QUALITY = "DATA_QUALITY"
    # AD10 — filed by pipeline_job_executor._run_anomaly_detection's
    # on-critical action (create_incident), and by AD11's standalone-job
    # critical-result path once that's wired.
    ANOMALY_DETECTION = "ANOMALY_DETECTION"
    # INV12 — filed by invoice_erp_service._post_in_background() when an
    # ERP posting attempt exhausts retries or is blocked by an open
    # circuit breaker (DEAD_LETTER), and by invoice_reconciliation_
    # service.reconcile_invoice() when a post-success reconciliation
    # finds a genuine field mismatch — same "best-effort, never fails the
    # real operation" convention as ANOMALY_DETECTION above.
    INVOICE_ERP_POSTING = "INVOICE_ERP_POSTING"
    INVOICE_RECONCILIATION = "INVOICE_RECONCILIATION"
    # Phase 6 (spec item #7) — filed by document_webhook_service.py's
    # background retry loop on the same DEAD_LETTER/circuit-open
    # conditions as INVOICE_ERP_POSTING above.
    DOCUMENT_WEBHOOK = "DOCUMENT_WEBHOOK"
    # TALLY-J — filed by tally_writeback_service.py's background retry loop
    # on the same DEAD_LETTER/circuit-open conditions as INVOICE_ERP_POSTING
    # above, and by schedule_service._execute_tally_reconciliation_schedule()
    # when a scheduled reconciliation run itself lands in FAILED (Tally
    # unreachable, connection deleted, etc.) — a distinct source from the
    # pre-existing RECONCILIATION one above, whose own message template
    # describes a per-record MISMATCH, not a whole-run failure.
    TALLY_WRITEBACK = "TALLY_WRITEBACK"
    TALLY_RECONCILIATION = "TALLY_RECONCILIATION"


class IncidentSeverity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class IncidentStatus(str, enum.Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    MITIGATED = "MITIGATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class IncidentTimelineEventType(str, enum.Enum):
    DETECTED = "DETECTED"
    ASSIGNED = "ASSIGNED"
    INVESTIGATION_STARTED = "INVESTIGATION_STARTED"
    ROOT_CAUSE_IDENTIFIED = "ROOT_CAUSE_IDENTIFIED"
    MITIGATION = "MITIGATION"
    RESOLVED = "RESOLVED"
    REOPENED = "REOPENED"
    CLOSED = "CLOSED"
    SEVERITY_CHANGED = "SEVERITY_CHANGED"
    COMMENT = "COMMENT"
    EVIDENCE_ADDED = "EVIDENCE_ADDED"


class IncidentEvidenceType(str, enum.Enum):
    LOG = "LOG"
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    PIPELINE_RUN = "PIPELINE_RUN"
    DATA_SAMPLE = "DATA_SAMPLE"
    CONNECTION_STATUS = "CONNECTION_STATUS"
    RECONCILIATION_RESULT = "RECONCILIATION_RESULT"


class Incident(TenantBase):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # FK only, no relationship() back-populates — additive, doesn't touch
    # models/organization.py or models/user.py.
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    incident_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # e.g. "INC-10281"
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[IncidentSource] = mapped_column(Enum(IncidentSource), nullable=False, index=True)
    severity: Mapped[IncidentSeverity] = mapped_column(Enum(IncidentSeverity), default=IncidentSeverity.MEDIUM, index=True)
    status: Mapped[IncidentStatus] = mapped_column(Enum(IncidentStatus), default=IncidentStatus.OPEN, index=True)

    # References into other modules — by id/name only (see module docstring).
    dataset_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pipeline_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pipeline_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    connection_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    connection_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    environment: Mapped[str] = mapped_column(String(30), default="PRODUCTION")

    owner_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    owner_team: Mapped[str | None] = mapped_column(String(100), nullable=True)  # e.g. "Data Engineering"

    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_investigation: Mapped[str | None] = mapped_column(Text, nullable=True)

    affected_records: Mapped[int | None] = mapped_column(Integer, nullable=True)
    affected_pipelines: Mapped[list] = mapped_column(JSON, default=list)
    affected_datasets: Mapped[list] = mapped_column(JSON, default=list)
    affected_connections: Mapped[list] = mapped_column(JSON, default=list)

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Incident {self.incident_number} {self.severity}/{self.status}>"


class IncidentTimelineEvent(TenantBase):
    """Append-only audit trail — lifecycle stages + comments, one feed."""
    __tablename__ = "incident_timeline_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)

    event_type: Mapped[IncidentTimelineEventType] = mapped_column(Enum(IncidentTimelineEventType), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    actor_id: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<IncidentTimelineEvent incident={self.incident_id} {self.event_type}>"


class IncidentEvidence(TenantBase):
    __tablename__ = "incident_evidence"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)

    evidence_type: Mapped[IncidentEvidenceType] = mapped_column(Enum(IncidentEvidenceType), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, default=dict)  # free-form: {text, rows, metrics, ...}

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<IncidentEvidence incident={self.incident_id} {self.evidence_type}>"
