"""
Monitoring models — SlaDefinition / AlertRule / AlertEvent.

Deliberately the ONLY new persisted tables the Monitoring module adds.
Everything else Monitoring shows (pipeline runs, job runs, data quality,
performance, system health) is read live from the execution tracking
tables that already exist — PipelineJob/PipelineJobLog, PipelineRun,
TargetLoadRun, ScheduleRun, ValidationRun, ReconciliationRun,
RejectedRecord, Incident (see app/monitoring/repository.py) — never
duplicated here. This mirrors how models/schedule.py and models/
incident.py reference pipelines/connections by id/name only, no FK: a
Pipeline row is a Transform Workspace concept the backend never modeled
as its own table, so SlaDefinition/AlertRule reference pipeline_id (the
Pipeline Builder's own string/int id) the same additive way.

SlaDefinition — one row per pipeline's SLA contract. Evaluated after every
PipelineJob completes (see app/monitoring/service.py's evaluate_sla()),
producing MET / WARNING / BREACHED, never stored as its own history table
— the verdict is a live computation against SlaDefinition + the job's own
started_at/completed_at, so it can never drift out of sync with the job
record it's judging.

AlertRule / AlertEvent — configurable condition -> notification. Modeled
on the notification_channels registry (app/monitoring/notification_channels.py)
so a rule's `channels` list is data ({"type": "EMAIL", ...}), not a
hardcoded call to one provider — adding a new channel type never touches
this model or existing rules.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class SlaStatus(str, enum.Enum):
    MET = "MET"
    WARNING = "WARNING"
    BREACHED = "BREACHED"
    NOT_EVALUATED = "NOT_EVALUATED"  # no run yet, or run still in progress


class SlaDefinition(Base):
    """SLA contract for one Pipeline (Pipeline Builder's pipelines.id)."""
    __tablename__ = "sla_definitions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)

    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), nullable=False, index=True)
    pipeline_name: Mapped[str] = mapped_column(String(200), nullable=False)  # snapshot, survives a later rename

    # Wall-clock "HH:MM" (interpreted in the org's Settings timezone, same
    # convention as Schedule.start_at) — None means "no expectation, judge
    # on duration/failures only".
    expected_start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    expected_completion_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    max_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warning_threshold_pct: Mapped[int] = mapped_column(Integer, default=80)  # % of max_duration that trips WARNING before BREACHED
    max_failure_count: Mapped[int] = mapped_column(Integer, default=1)  # consecutive failures allowed before BREACHED

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_create_incident: Mapped[bool] = mapped_column(Boolean, default=True)  # file an Incident the moment a run BREACHES

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<SlaDefinition pipeline={self.pipeline_id}>"


class AlertCondition(str, enum.Enum):
    JOB_FAILED = "JOB_FAILED"
    SLA_BREACHED = "SLA_BREACHED"
    SLA_WARNING = "SLA_WARNING"
    REJECT_RATE_ABOVE = "REJECT_RATE_ABOVE"        # % of records rejected in a run, threshold = percent (0-100)
    DURATION_ABOVE_MINUTES = "DURATION_ABOVE_MINUTES"  # any run longer than threshold minutes
    VALIDATION_FAILED = "VALIDATION_FAILED"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    CONSECUTIVE_FAILURES = "CONSECUTIVE_FAILURES"  # threshold = consecutive failure count


class AlertSeverity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AlertRule(Base):
    """Configurable alert rule — see module docstring for the channel-
    registry design. `channels` shape: [{"type": "IN_APP"},
    {"type": "EMAIL", "to": "ops@acme.com"},
    {"type": "WEBHOOK", "url": "https://..."},
    {"type": "TEAMS", "webhook_url": "https://..."}]."""
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    condition: Mapped[AlertCondition] = mapped_column(Enum(AlertCondition), nullable=False, index=True)
    # None = applies to every pipeline in the org; set = scoped to one.
    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipelines.id"), nullable=True, index=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)  # meaning depends on `condition` — see enum doc

    severity: Mapped[AlertSeverity] = mapped_column(Enum(AlertSeverity), default=AlertSeverity.MEDIUM)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 0 = fire every time the condition is met; >0 = suppress repeats within the window
    channels: Mapped[list] = mapped_column(JSON, default=list)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<AlertRule {self.name} ({self.condition})>"


class AlertEventStatus(str, enum.Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AlertEvent(Base):
    """One fired instance of an AlertRule. This row IS the in-app
    notification (the IN_APP channel is a no-op beyond creating this row);
    EMAIL/WEBHOOK/TEAMS channels additionally attempt real delivery and
    record the outcome in `delivery_results`."""
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id"), nullable=False, index=True)
    rule_name: Mapped[str] = mapped_column(String(200), nullable=False)  # snapshot

    condition: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipelines.id"), nullable=True, index=True)
    pipeline_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_jobs.id"), nullable=True, index=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id"), nullable=True)

    status: Mapped[AlertEventStatus] = mapped_column(Enum(AlertEventStatus), default=AlertEventStatus.OPEN, index=True)
    # [{type, delivered: bool, detail: str}] — one entry per channel attempted, see notification_channels.py
    delivery_results: Mapped[list] = mapped_column(JSON, default=list)

    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<AlertEvent rule={self.rule_id} {self.severity} {self.status}>"
