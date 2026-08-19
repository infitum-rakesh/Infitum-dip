from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Responses ────────────────────────────────────────────────────

class IncidentOut(BaseModel):
    id: int
    incident_number: str
    title: str
    description: str | None
    source: str
    severity: str
    status: str

    dataset_name: str | None
    pipeline_id: str | None
    pipeline_name: str | None
    connection_id: str | None
    connection_name: str | None
    environment: str

    owner_id: int | None
    owner_name: str | None = None
    owner_team: str | None

    root_cause: str | None
    ai_investigation: str | None

    affected_records: int | None
    affected_pipelines: list[str]
    affected_datasets: list[str]
    affected_connections: list[str]

    sla_breached: bool  # computed — see incident_service.compute_sla_breached

    detected_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None

    created_by: int | None
    created_by_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IncidentListOut(BaseModel):
    items: list[IncidentOut]
    total: int
    page: int
    page_size: int


class IncidentStatsOut(BaseModel):
    open: int
    critical: int
    high: int
    investigating: int
    resolved: int
    sla_breached: int


class IncidentTimelineEventOut(BaseModel):
    id: int
    event_type: str
    message: str | None
    actor_id: int | None
    actor_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class IncidentTimelineOut(BaseModel):
    incident_id: int
    items: list[IncidentTimelineEventOut]


class IncidentEvidenceOut(BaseModel):
    id: int
    evidence_type: str
    title: str
    content: dict
    created_by: int | None
    created_by_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class IncidentEvidenceListOut(BaseModel):
    incident_id: int
    items: list[IncidentEvidenceOut]


# ── Requests ─────────────────────────────────────────────────────

class IncidentCreateRequest(BaseModel):
    title: str
    description: str | None = None
    source: str
    severity: str = "MEDIUM"
    dataset_name: str | None = None
    pipeline_id: str | None = None
    pipeline_name: str | None = None
    connection_id: str | None = None
    connection_name: str | None = None
    environment: str = "PRODUCTION"
    owner_team: str | None = None
    root_cause: str | None = None
    affected_records: int | None = None
    affected_pipelines: list[str] = []
    affected_datasets: list[str] = []
    affected_connections: list[str] = []


class IncidentUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    severity: str | None = None
    status: str | None = None
    dataset_name: str | None = None
    pipeline_id: str | None = None
    pipeline_name: str | None = None
    connection_id: str | None = None
    connection_name: str | None = None
    environment: str | None = None
    owner_team: str | None = None
    root_cause: str | None = None
    ai_investigation: str | None = None
    affected_records: int | None = None
    affected_pipelines: list[str] | None = None
    affected_datasets: list[str] | None = None
    affected_connections: list[str] | None = None


class AssignRequest(BaseModel):
    owner_id: int | None = None
    owner_team: str | None = None


class ChangeSeverityRequest(BaseModel):
    severity: str
    reason: str | None = None


class ResolveRequest(BaseModel):
    resolution_note: str | None = None


class ReopenRequest(BaseModel):
    reason: str | None = None


class CommentRequest(BaseModel):
    message: str


class EvidenceCreateRequest(BaseModel):
    evidence_type: str
    title: str
    content: dict = {}
