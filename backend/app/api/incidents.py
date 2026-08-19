"""Incidents router — /api/incidents/...

Centralized monitoring of failures/anomalies across the platform. See
services/incident_service.py for why this never polls other modules
itself — it only owns incident records + their lifecycle/audit trail.

Foundational module (module=None) — Incidents watches over EVERY other
module (Pipeline, Validation, Reconciliation, Connections, Document
Intelligence, Schedules), so it isn't gated behind any single one of
their licenses, same reasoning as Connections/Schedules.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.models.user import User, UserRole
from app.schemas.incidents import (
    AssignRequest,
    ChangeSeverityRequest,
    CommentRequest,
    EvidenceCreateRequest,
    IncidentCreateRequest,
    IncidentEvidenceListOut,
    IncidentEvidenceOut,
    IncidentListOut,
    IncidentOut,
    IncidentStatsOut,
    IncidentTimelineOut,
    IncidentUpdateRequest,
    ReopenRequest,
    ResolveRequest,
)
from app.services import incident_service as svc

router = APIRouter(prefix="/incidents", tags=["incidents"])

_view    = require_action("VIEW", module=None)
_create  = require_action("CREATE", module=None)
_assign  = require_action("EDIT", module=None)
_update  = require_action("EDIT", module=None)
_resolve = require_action("APPROVE", module=None)
_delete  = require_action("DELETE", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Incident not found")


async def _get_or_404(db: AsyncSession, incident_id: int, user: User):
    incident = await svc.get_incident(db, incident_id, _effective_org_id(user))
    if incident is None:
        raise _not_found()
    return incident


async def _out(db: AsyncSession, incident) -> IncidentOut:
    name_map = await svc.user_name_map(db, {incident.owner_id, incident.created_by})
    return IncidentOut(**svc.incident_to_out(incident, name_map))


# ── KPIs ──────────────────────────────────────────────────────────
# NOTE: literal-path /stats must stay registered before /{incident_id} —
# Starlette matches by position, not type.

@router.get("/stats", response_model=IncidentStatsOut)
async def stats(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await svc.get_stats(db, _effective_org_id(user))


# ── List / get ────────────────────────────────────────────────────

@router.get("", response_model=IncidentListOut)
async def list_incidents(
    q: str = Query("", description="Search title, incident number, dataset, pipeline"),
    severity: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    source: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("detected_at"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_db),
):
    incidents, total = await svc.list_incidents(
        db, organization_id=_effective_org_id(user), search=q, severity=severity,
        status_filter=status_filter, source=source, page=page, page_size=page_size,
        sort_by=sort_by, sort_dir=sort_dir,
    )
    name_map = await svc.user_name_map(db, {i.owner_id for i in incidents} | {i.created_by for i in incidents})
    items = [IncidentOut(**svc.incident_to_out(i, name_map)) for i in incidents]
    return IncidentListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/{incident_id}", response_model=IncidentOut)
async def get_incident(incident_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    return await _out(db, incident)


# ── Create / update / delete ────────────────────────────────────

@router.post("", response_model=IncidentOut, status_code=status.HTTP_201_CREATED)
async def create_incident(body: IncidentCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
    try:
        incident = await svc.create_incident(db, organization_id=user.organization_id, user_id=user.id, body=body)
    except svc.IncidentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _out(db, incident)


@router.put("/{incident_id}", response_model=IncidentOut)
async def update_incident(incident_id: int, body: IncidentUpdateRequest, user: User = Depends(_update), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    try:
        incident = await svc.update_incident(db, incident, body, user.id)
    except svc.IncidentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _out(db, incident)


@router.delete("/{incident_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_incident(incident_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    await svc.delete_incident(db, incident)


# ── Actions ───────────────────────────────────────────────────────

@router.post("/{incident_id}/assign", response_model=IncidentOut)
async def assign_incident(incident_id: int, body: AssignRequest, user: User = Depends(_assign), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    try:
        incident = await svc.assign_incident(db, incident, body.owner_id, body.owner_team, user.id)
    except svc.IncidentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _out(db, incident)


@router.post("/{incident_id}/start-investigation", response_model=IncidentOut)
async def start_investigation(incident_id: int, user: User = Depends(_update), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    try:
        incident = await svc.start_investigation(db, incident, user.id)
    except svc.IncidentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _out(db, incident)


@router.post("/{incident_id}/severity", response_model=IncidentOut)
async def change_severity(incident_id: int, body: ChangeSeverityRequest, user: User = Depends(_update), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    try:
        incident = await svc.change_severity(db, incident, body.severity, body.reason, user.id)
    except svc.IncidentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _out(db, incident)


@router.post("/{incident_id}/comments", response_model=IncidentOut)
async def add_comment(incident_id: int, body: CommentRequest, user: User = Depends(_update), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    try:
        incident = await svc.add_comment(db, incident, body.message, user.id)
    except svc.IncidentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _out(db, incident)


@router.post("/{incident_id}/resolve", response_model=IncidentOut)
async def resolve_incident(incident_id: int, body: ResolveRequest, user: User = Depends(_resolve), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    try:
        incident = await svc.resolve_incident(db, incident, body.resolution_note, user.id)
    except svc.IncidentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _out(db, incident)


@router.post("/{incident_id}/reopen", response_model=IncidentOut)
async def reopen_incident(incident_id: int, body: ReopenRequest, user: User = Depends(_update), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    try:
        incident = await svc.reopen_incident(db, incident, body.reason, user.id)
    except svc.IncidentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _out(db, incident)


@router.post("/{incident_id}/close", response_model=IncidentOut)
async def close_incident(incident_id: int, user: User = Depends(_resolve), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    incident = await svc.close_incident(db, incident, user.id)
    return await _out(db, incident)


# ── Timeline / Evidence ───────────────────────────────────────────

@router.get("/{incident_id}/timeline", response_model=IncidentTimelineOut)
async def get_timeline(incident_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    await _get_or_404(db, incident_id, user)
    events = await svc.get_timeline(db, incident_id)
    name_map = await svc.user_name_map(db, {e.actor_id for e in events})
    return IncidentTimelineOut(incident_id=incident_id, items=[svc.timeline_event_to_out(e, name_map) for e in events])


@router.get("/{incident_id}/evidence", response_model=IncidentEvidenceListOut)
async def get_evidence(incident_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    await _get_or_404(db, incident_id, user)
    items = await svc.get_evidence(db, incident_id)
    name_map = await svc.user_name_map(db, {e.created_by for e in items})
    return IncidentEvidenceListOut(incident_id=incident_id, items=[svc.evidence_to_out(e, name_map) for e in items])


@router.post("/{incident_id}/evidence", response_model=IncidentEvidenceOut, status_code=status.HTTP_201_CREATED)
async def add_evidence(incident_id: int, body: EvidenceCreateRequest, user: User = Depends(_update), db: AsyncSession = Depends(get_db)):
    incident = await _get_or_404(db, incident_id, user)
    try:
        evidence = await svc.add_evidence(db, incident, body.evidence_type, body.title, body.content, user.id)
    except svc.IncidentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name_map = await svc.user_name_map(db, {evidence.created_by})
    return IncidentEvidenceOut(**svc.evidence_to_out(evidence, name_map))
