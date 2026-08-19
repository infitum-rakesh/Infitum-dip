from datetime import datetime

from pydantic import BaseModel


class AuditLogEntryOut(BaseModel):
    id: str  # composite, e.g. "ual-123" — see audit_log_service for the source prefix map
    timestamp: datetime
    user_id: int | None
    user_name: str | None
    action: str
    module: str
    resource: str | None
    resource_id: str | None
    ip_address: str | None
    status: str
    details: str | None


class AuditLogListOut(BaseModel):
    items: list[AuditLogEntryOut]
    total: int
    page: int
    page_size: int
    modules: list[str]  # AUDIT_MODULES, for the filter dropdown


class AuditLogDetailOut(AuditLogEntryOut):
    before_value: dict | None
    after_value: dict | None
    user_agent: str | None
