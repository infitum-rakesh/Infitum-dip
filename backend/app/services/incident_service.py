"""
Incident service — centralized monitoring of failures/anomalies across the
platform.

Not a second monitoring framework: this module owns incident records,
their lifecycle (assign/investigate/resolve/reopen/close) and their audit
trail (timeline + evidence) — nothing else. It does NOT poll or scrape
Pipeline/Validation/Reconciliation/Connections/Document Intelligence/
Schedules itself. Those services can call `report_incident()` below to
file an incident when they detect a failure, but none of them are wired
up to do so here (that would mean modifying unrelated modules) — this is
the integration seam for that to happen later, additively, from their side.

`ai_investigation` is a simulated root-cause suggestion (see
`_simulate_ai_investigation` — same "no real engine yet" pattern as
connection_service.test_connection / schedule_service.execute_schedule),
generated once at creation time so the Incident Details page always has
something useful to show without a real LLM call wired in yet.
"""
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.incident import (
    Incident,
    IncidentEvidence,
    IncidentEvidenceType,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
    IncidentTimelineEvent,
    IncidentTimelineEventType,
)
from app.models.user import User

settings = get_settings()

SLA_HOURS_BY_SEVERITY_SETTING = {
    IncidentSeverity.CRITICAL: "incident_sla_hours_critical",
    IncidentSeverity.HIGH: "incident_sla_hours_high",
    IncidentSeverity.MEDIUM: "incident_sla_hours_medium",
    IncidentSeverity.LOW: "incident_sla_hours_low",
}

OPEN_STATUSES = {IncidentStatus.OPEN, IncidentStatus.INVESTIGATING, IncidentStatus.MITIGATED}


class IncidentValidationError(Exception):
    pass


# ── Time helpers (SQLite drops tzinfo on round-trip — see schedule_service.py) ──

def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_utc_opt(dt: datetime | None) -> datetime | None:
    return _as_utc(dt) if dt else None


# ── Simulated AI investigation ──────────────────────────────────

_AI_TEMPLATES = {
    IncidentSource.TRANSFORMATION: "A recent change in the transformation logic{pipeline_clause} appears to have altered the output mapping{dataset_clause}, producing unexpected downstream values.",
    IncidentSource.VALIDATION: "Validation rules{dataset_clause} are failing on a subset of records that recently changed shape — consistent with upstream schema drift.",
    IncidentSource.RECONCILIATION: "Reconciliation{dataset_clause} diverged beyond tolerance against its comparison source, consistent with a timing or key-mapping mismatch.",
    IncidentSource.PIPELINE: "The pipeline{pipeline_clause} failed partway through execution; the last successful stage did not hand off the expected columns to the next stage.",
    IncidentSource.CONNECTION: "The connection{connection_clause} is failing to authenticate or reach its endpoint — likely a credential rotation or network/firewall change.",
    IncidentSource.OCR: "OCR confidence dropped sharply on recent documents, consistent with a change in scan quality or source template.",
    IncidentSource.AI_EXTRACTION: "AI Extraction confidence fell below threshold on a cluster of documents sharing the same layout, suggesting a template change.",
    IncidentSource.SCHEDULE: "The scheduled run{pipeline_clause} failed before completing — the underlying pipeline reported an error partway through execution.",
    IncidentSource.DATA_QUALITY: "An anomaly was detected in{dataset_clause} volume/shape compared to its historical baseline.",
}


def _simulate_ai_investigation(source: IncidentSource, dataset_name: str | None, pipeline_name: str | None, connection_name: str | None) -> str:
    template = _AI_TEMPLATES.get(source, "Automated investigation did not find a conclusive root cause yet — see evidence and logs for details.")
    return template.format(
        dataset_clause=f" on {dataset_name}" if dataset_name else "",
        pipeline_clause=f" {pipeline_name}" if pipeline_name else "",
        connection_clause=f" {connection_name}" if connection_name else "",
    )


# ── Numbering ────────────────────────────────────────────────────

async def _next_incident_number(db: AsyncSession, incident_id: int) -> str:
    return f"INC-{10000 + incident_id}"


# ── SLA ──────────────────────────────────────────────────────────

def compute_sla_breached(incident: Incident) -> bool:
    if incident.status not in OPEN_STATUSES:
        return False
    hours_open = (datetime.now(timezone.utc) - _as_utc(incident.detected_at)).total_seconds() / 3600
    threshold = getattr(settings, SLA_HOURS_BY_SEVERITY_SETTING.get(incident.severity, "incident_sla_hours_medium"))
    return hours_open > threshold


# ── Lookups ──────────────────────────────────────────────────────

async def get_incident(db: AsyncSession, incident_id: int, organization_id: int | None) -> Incident | None:
    stmt = select(Incident).where(Incident.id == incident_id)
    if organization_id is not None:
        stmt = stmt.where(Incident.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_incidents(
    db: AsyncSession, *, organization_id: int | None, search: str = "",
    severity: str | None = None, status_filter: str | None = None, source: str | None = None,
    page: int = 1, page_size: int = 20, sort_by: str = "detected_at", sort_dir: str = "desc",
) -> tuple[list[Incident], int]:
    stmt = select(Incident)
    if organization_id is not None:
        stmt = stmt.where(Incident.organization_id == organization_id)
    if severity:
        stmt = stmt.where(Incident.severity == severity)
    if status_filter:
        stmt = stmt.where(Incident.status == status_filter)
    if source:
        stmt = stmt.where(Incident.source == source)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(
            Incident.title.ilike(like),
            Incident.incident_number.ilike(like),
            Incident.dataset_name.ilike(like),
            Incident.pipeline_name.ilike(like),
        ))

    count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = count_result.scalar_one()

    sort_col = getattr(Incident, sort_by, Incident.detected_at)
    stmt = stmt.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(stmt)
    return list(result.scalars().all()), total


async def get_stats(db: AsyncSession, organization_id: int | None) -> dict:
    base = select(Incident)
    if organization_id is not None:
        base = base.where(Incident.organization_id == organization_id)

    async def count_where(*conds):
        stmt = base
        for c in conds:
            stmt = stmt.where(c)
        r = await db.execute(select(func.count()).select_from(stmt.subquery()))
        return r.scalar_one()

    open_count = await count_where(Incident.status == IncidentStatus.OPEN)
    critical = await count_where(Incident.severity == IncidentSeverity.CRITICAL, Incident.status.in_(OPEN_STATUSES))
    high = await count_where(Incident.severity == IncidentSeverity.HIGH, Incident.status.in_(OPEN_STATUSES))
    investigating = await count_where(Incident.status == IncidentStatus.INVESTIGATING)
    resolved = await count_where(Incident.status == IncidentStatus.RESOLVED)

    # SLA breach depends on per-severity thresholds — compute in Python over the open set.
    open_result = await db.execute(base.where(Incident.status.in_(OPEN_STATUSES)))
    sla_breached = sum(1 for inc in open_result.scalars().all() if compute_sla_breached(inc))

    return {
        "open": open_count, "critical": critical, "high": high,
        "investigating": investigating, "resolved": resolved, "sla_breached": sla_breached,
    }


async def get_timeline(db: AsyncSession, incident_id: int) -> list[IncidentTimelineEvent]:
    result = await db.execute(
        select(IncidentTimelineEvent)
        .where(IncidentTimelineEvent.incident_id == incident_id)
        .order_by(IncidentTimelineEvent.created_at.asc(), IncidentTimelineEvent.id.asc())
    )
    return list(result.scalars().all())


async def get_evidence(db: AsyncSession, incident_id: int) -> list[IncidentEvidence]:
    result = await db.execute(
        select(IncidentEvidence)
        .where(IncidentEvidence.incident_id == incident_id)
        .order_by(IncidentEvidence.created_at.desc())
    )
    return list(result.scalars().all())


# ── Timeline logging (internal) ─────────────────────────────────

async def _log(db: AsyncSession, incident: Incident, event_type: IncidentTimelineEventType, message: str | None, actor_id: int | None):
    db.add(IncidentTimelineEvent(incident_id=incident.id, event_type=event_type, message=message, actor_id=actor_id))


# ── Create / update ──────────────────────────────────────────────

async def create_incident(db: AsyncSession, *, organization_id: int | None, user_id: int | None, body) -> Incident:
    try:
        source = IncidentSource(body.source)
        severity = IncidentSeverity(body.severity)
    except ValueError as exc:
        raise IncidentValidationError(str(exc))

    incident = Incident(
        organization_id=organization_id,
        incident_number="PENDING",
        title=body.title, description=body.description,
        source=source, severity=severity, status=IncidentStatus.OPEN,
        dataset_name=body.dataset_name, pipeline_id=body.pipeline_id, pipeline_name=body.pipeline_name,
        connection_id=body.connection_id, connection_name=body.connection_name,
        environment=body.environment or "PRODUCTION",
        owner_team=body.owner_team,
        root_cause=body.root_cause,
        affected_records=body.affected_records,
        affected_pipelines=body.affected_pipelines or [],
        affected_datasets=body.affected_datasets or [],
        affected_connections=body.affected_connections or [],
        created_by=user_id,
    )
    db.add(incident)
    await db.flush()  # assign id

    incident.incident_number = await _next_incident_number(db, incident.id)
    incident.ai_investigation = _simulate_ai_investigation(source, body.dataset_name, body.pipeline_name, body.connection_name)

    await _log(db, incident, IncidentTimelineEventType.DETECTED, body.description or f"{source.value} incident detected.", user_id)

    await db.commit()
    await db.refresh(incident)
    return incident


async def update_incident(db: AsyncSession, incident: Incident, body, actor_id: int | None) -> Incident:
    patch = body.model_dump(exclude_unset=True)
    severity_changed = False
    old_severity = incident.severity

    for key, value in patch.items():
        if key == "severity" and value is not None:
            try:
                value = IncidentSeverity(value)
            except ValueError:
                raise IncidentValidationError(f"Invalid severity: {value}")
            severity_changed = value != incident.severity
        if key == "status" and value is not None:
            try:
                value = IncidentStatus(value)
            except ValueError:
                raise IncidentValidationError(f"Invalid status: {value}")
        setattr(incident, key, value)

    if severity_changed:
        await _log(db, incident, IncidentTimelineEventType.SEVERITY_CHANGED, f"Severity changed from {old_severity.value} to {incident.severity.value}", actor_id)

    await db.commit()
    await db.refresh(incident)
    return incident


async def delete_incident(db: AsyncSession, incident: Incident) -> None:
    await db.execute(IncidentTimelineEvent.__table__.delete().where(IncidentTimelineEvent.incident_id == incident.id))
    await db.execute(IncidentEvidence.__table__.delete().where(IncidentEvidence.incident_id == incident.id))
    await db.delete(incident)
    await db.commit()


# ── Actions ──────────────────────────────────────────────────────

async def assign_incident(db: AsyncSession, incident: Incident, owner_id: int | None, owner_team: str | None, actor_id: int | None) -> Incident:
    if owner_id is None and not owner_team:
        raise IncidentValidationError("Provide an owner_id and/or owner_team to assign.")
    if owner_id is not None:
        incident.owner_id = owner_id
    if owner_team:
        incident.owner_team = owner_team

    name_map = await user_name_map(db, {owner_id} if owner_id else set())
    who = name_map.get(owner_id) or owner_team or "someone"
    await _log(db, incident, IncidentTimelineEventType.ASSIGNED, f"Assigned to {who}", actor_id)

    await db.commit()
    await db.refresh(incident)
    return incident


async def start_investigation(db: AsyncSession, incident: Incident, actor_id: int | None) -> Incident:
    if incident.status == IncidentStatus.CLOSED:
        raise IncidentValidationError("Cannot start investigation on a closed incident.")
    incident.status = IncidentStatus.INVESTIGATING
    await _log(db, incident, IncidentTimelineEventType.INVESTIGATION_STARTED, "Investigation started", actor_id)
    await db.commit()
    await db.refresh(incident)
    return incident


async def change_severity(db: AsyncSession, incident: Incident, severity: str, reason: str | None, actor_id: int | None) -> Incident:
    try:
        new_severity = IncidentSeverity(severity)
    except ValueError:
        raise IncidentValidationError(f"Invalid severity: {severity}")
    old = incident.severity
    incident.severity = new_severity
    msg = f"Severity changed from {old.value} to {new_severity.value}"
    if reason:
        msg += f" — {reason}"
    await _log(db, incident, IncidentTimelineEventType.SEVERITY_CHANGED, msg, actor_id)
    await db.commit()
    await db.refresh(incident)
    return incident


async def add_comment(db: AsyncSession, incident: Incident, message: str, actor_id: int | None) -> Incident:
    if not message or not message.strip():
        raise IncidentValidationError("Comment message cannot be empty.")
    await _log(db, incident, IncidentTimelineEventType.COMMENT, message.strip(), actor_id)
    await db.commit()
    await db.refresh(incident)
    return incident


async def add_evidence(db: AsyncSession, incident: Incident, evidence_type: str, title: str, content: dict, actor_id: int | None) -> IncidentEvidence:
    try:
        etype = IncidentEvidenceType(evidence_type)
    except ValueError:
        raise IncidentValidationError(f"Invalid evidence type: {evidence_type}")
    evidence = IncidentEvidence(incident_id=incident.id, evidence_type=etype, title=title, content=content or {}, created_by=actor_id)
    db.add(evidence)
    await _log(db, incident, IncidentTimelineEventType.EVIDENCE_ADDED, f"Evidence added: {title}", actor_id)
    await db.commit()
    await db.refresh(evidence)
    return evidence


async def resolve_incident(db: AsyncSession, incident: Incident, resolution_note: str | None, actor_id: int | None) -> Incident:
    if incident.status == IncidentStatus.CLOSED:
        raise IncidentValidationError("Incident is already closed.")
    incident.status = IncidentStatus.RESOLVED
    incident.resolved_at = datetime.now(timezone.utc)
    if resolution_note:
        incident.root_cause = incident.root_cause or resolution_note
    await _log(db, incident, IncidentTimelineEventType.RESOLVED, resolution_note or "Incident resolved", actor_id)
    await db.commit()
    await db.refresh(incident)
    return incident


async def reopen_incident(db: AsyncSession, incident: Incident, reason: str | None, actor_id: int | None) -> Incident:
    if incident.status not in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED):
        raise IncidentValidationError("Only resolved or closed incidents can be reopened.")
    incident.status = IncidentStatus.OPEN
    incident.resolved_at = None
    incident.closed_at = None
    await _log(db, incident, IncidentTimelineEventType.REOPENED, reason or "Incident reopened", actor_id)
    await db.commit()
    await db.refresh(incident)
    return incident


async def close_incident(db: AsyncSession, incident: Incident, actor_id: int | None) -> Incident:
    incident.status = IncidentStatus.CLOSED
    incident.closed_at = datetime.now(timezone.utc)
    await _log(db, incident, IncidentTimelineEventType.CLOSED, "Incident closed", actor_id)
    await db.commit()
    await db.refresh(incident)
    return incident


# ── Integration seam for other modules (not wired up anywhere yet) ──

async def report_incident(
    db: AsyncSession, *, organization_id: int | None, source: str, title: str,
    description: str | None = None, severity: str = "MEDIUM", **refs,
) -> Incident:
    """
    Lets another service (pipeline/validation/reconciliation/connections/
    document-intelligence/schedules) file an incident without this module
    reaching into theirs. No caller is wired up yet — see module docstring.
    """
    class _Body:
        pass
    b = _Body()
    b.title, b.description, b.source, b.severity = title, description, source, severity
    for f in ("dataset_name", "pipeline_id", "pipeline_name", "connection_id", "connection_name",
              "environment", "owner_team", "root_cause", "affected_records",
              "affected_pipelines", "affected_datasets", "affected_connections"):
        setattr(b, f, refs.get(f) if f not in ("affected_pipelines", "affected_datasets", "affected_connections") else refs.get(f, []))
    if not getattr(b, "environment", None):
        b.environment = "PRODUCTION"
    return await create_incident(db, organization_id=organization_id, user_id=None, body=b)


# ── Output shaping ───────────────────────────────────────────────

async def user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    result = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row[0]: row[1] for row in result.all()}


def incident_to_out(incident: Incident, name_map: dict[int, str]) -> dict:
    return {
        "id": incident.id,
        "incident_number": incident.incident_number,
        "title": incident.title,
        "description": incident.description,
        "source": incident.source.value if hasattr(incident.source, "value") else incident.source,
        "severity": incident.severity.value if hasattr(incident.severity, "value") else incident.severity,
        "status": incident.status.value if hasattr(incident.status, "value") else incident.status,
        "dataset_name": incident.dataset_name,
        "pipeline_id": incident.pipeline_id,
        "pipeline_name": incident.pipeline_name,
        "connection_id": incident.connection_id,
        "connection_name": incident.connection_name,
        "environment": incident.environment,
        "owner_id": incident.owner_id,
        "owner_name": name_map.get(incident.owner_id),
        "owner_team": incident.owner_team,
        "root_cause": incident.root_cause,
        "ai_investigation": incident.ai_investigation,
        "affected_records": incident.affected_records,
        "affected_pipelines": incident.affected_pipelines or [],
        "affected_datasets": incident.affected_datasets or [],
        "affected_connections": incident.affected_connections or [],
        "sla_breached": compute_sla_breached(incident),
        "detected_at": _as_utc(incident.detected_at),
        "resolved_at": _as_utc_opt(incident.resolved_at),
        "closed_at": _as_utc_opt(incident.closed_at),
        "created_by": incident.created_by,
        "created_by_name": name_map.get(incident.created_by),
        "created_at": _as_utc(incident.created_at),
        "updated_at": _as_utc(incident.updated_at),
    }


def timeline_event_to_out(event: IncidentTimelineEvent, name_map: dict[int, str]) -> dict:
    return {
        "id": event.id,
        "event_type": event.event_type.value if hasattr(event.event_type, "value") else event.event_type,
        "message": event.message,
        "actor_id": event.actor_id,
        "actor_name": name_map.get(event.actor_id),
        "created_at": _as_utc(event.created_at),
    }


def evidence_to_out(evidence: IncidentEvidence, name_map: dict[int, str]) -> dict:
    return {
        "id": evidence.id,
        "evidence_type": evidence.evidence_type.value if hasattr(evidence.evidence_type, "value") else evidence.evidence_type,
        "title": evidence.title,
        "content": evidence.content or {},
        "created_by": evidence.created_by,
        "created_by_name": name_map.get(evidence.created_by),
        "created_at": _as_utc(evidence.created_at),
    }
