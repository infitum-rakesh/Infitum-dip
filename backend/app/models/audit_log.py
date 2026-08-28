"""
AuditLog — general-purpose audit event table for Administration -> Audit
Logs, for categories that don't already have their own real event table
elsewhere in this codebase.

This is NOT a second, competing audit system: services/audit_log_service.py
builds the unified feed the Audit Logs page reads by MERGING this table
with the audit tables three other modules already own and write to
(UserActivityLog, RoleAuditLog, IncidentTimelineEvent) — see that module's
docstring for the full source map. record_event() below is the integration
seam other services can call later for the categories (Connection Changes,
License Changes, Settings, Document Upload, OCR, AI Extraction,
Transformation, Validation, Reconciliation, Pipeline, Schedule) that have
no real write path yet — same "not wired up anywhere" pattern as
incident_service.report_incident().
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class AuditStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    WARNING = "WARNING"
    INFO = "INFO"


# The 16 event-type categories from the spec — a plain string column rather
# than forcing every source table to share one enum (UserActivityLog/
# RoleAuditLog/IncidentTimelineEvent keep their own types; the service layer
# maps each into one of these labels for display).
AUDIT_MODULES = [
    "Authentication", "User Management", "Role Management", "Permission Changes",
    "License Changes", "Connection Changes", "Document Upload", "OCR",
    "AI Extraction", "Transformation", "Validation", "Reconciliation",
    "Pipeline", "Schedule", "Incident", "Settings",
]


class AuditLog(TenantBase):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    action: Mapped[str] = mapped_column(String(100), nullable=False)
    module: Mapped[str] = mapped_column(String(60), nullable=False, index=True)  # one of AUDIT_MODULES
    resource: Mapped[str | None] = mapped_column(String(300), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    status: Mapped[AuditStatus] = mapped_column(Enum(AuditStatus), default=AuditStatus.SUCCESS, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)

    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<AuditLog {self.module}:{self.action} ({self.status})>"


async def record_event(
    db, *, organization_id, actor_id, action, module, resource=None, resource_id=None,
    status=AuditStatus.SUCCESS, ip_address=None, user_agent=None, details=None,
    before_value=None, after_value=None, commit=True,
):
    """Integration seam — see module docstring. Not called from anywhere yet."""
    entry = AuditLog(
        organization_id=organization_id, actor_id=actor_id, action=action, module=module,
        resource=resource, resource_id=resource_id, status=status, ip_address=ip_address,
        user_agent=user_agent, details=details, before_value=before_value, after_value=after_value,
    )
    db.add(entry)
    if commit:
        await db.commit()
    return entry
