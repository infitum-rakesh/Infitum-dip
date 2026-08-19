"""
Audit Log service — Administration -> Audit Logs.

"Provide a COMPLETE audit trail" is taken literally here rather than
building yet another logging table nothing writes to: this service reads
and merges the audit tables three other modules already own —
  UserActivityLog        (Administration -> Users)   -> Authentication, User Management
  RoleAuditLog            (Administration -> Roles)   -> Role Management, Permission Changes
  IncidentTimelineEvent   (Incidents)                 -> Incident
— with the new general-purpose AuditLog table (models/audit_log.py) for
every other category (Connection Changes, License Changes, Settings,
Document Upload, OCR, AI Extraction, Transformation, Validation,
Reconciliation, Pipeline, Schedule). Nothing writes into those categories
yet — same "seam, not wired up" pattern as incident_service.report_incident
— so they simply have no history until something calls record_event().

Never a second source of truth: this never duplicates what those three
modules already log, it just presents their existing rows in one timeline.

Secrets are masked defensively on every field this service returns (see
_mask), even though none of the source messages have ever contained one —
required by the "Never expose secrets" spec line regardless.
"""
import csv
import io
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AUDIT_MODULES, AuditLog
from app.models.incident import Incident, IncidentTimelineEvent
from app.models.role import Role, RoleAuditLog
from app.models.user import User
from app.models.user_activity import UserActivityLog

SOURCE_LIMIT = 1000  # per-source row cap — bounds cost on the in-Python merge/sort below

_MASK = "••••••"
_SENSITIVE_KEY_RE = re.compile(r"(password|token|secret|credential|api[_-]?key)", re.IGNORECASE)
_SENSITIVE_TEXT_RE = re.compile(r"((?:password|token|secret|api[_-]?key)\s*[:=]\s*)(\S+)", re.IGNORECASE)


def _mask_text(text: str | None) -> str | None:
    if not text:
        return text
    return _SENSITIVE_TEXT_RE.sub(lambda m: m.group(1) + _MASK, text)


def _mask_value(value):
    if isinstance(value, dict):
        return {k: (_MASK if _SENSITIVE_KEY_RE.search(k) else _mask_value(v)) for k, v in value.items()}
    if isinstance(value, str):
        return _mask_text(value)
    return value


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


USER_ACTIVITY_LABEL = {
    "LOGIN": ("Authentication", "Login", "SUCCESS"),
    "LOGIN_FAILED": ("Authentication", "Login Failed", "FAILURE"),
    "INVITED": ("User Management", "User Invited", "SUCCESS"),
    "UPDATED": ("User Management", "User Updated", "SUCCESS"),
    "ROLE_CHANGED": ("User Management", "Role Changed", "SUCCESS"),
    "DISABLED": ("User Management", "User Disabled", "SUCCESS"),
    "ENABLED": ("User Management", "User Enabled", "SUCCESS"),
    "PASSWORD_RESET": ("User Management", "Password Reset", "SUCCESS"),
    "DELETED": ("User Management", "User Deleted", "SUCCESS"),
}
ROLE_AUDIT_LABEL = {
    "CREATED": ("Role Management", "Role Created"),
    "UPDATED": ("Role Management", "Role Updated"),
    "PERMISSIONS_CHANGED": ("Permission Changes", "Permissions Changed"),
    "STATUS_CHANGED": ("Role Management", "Role Status Changed"),
    "USER_ASSIGNED": ("Role Management", "User Assigned to Role"),
    "USER_UNASSIGNED": ("Role Management", "User Unassigned from Role"),
    "DELETED": ("Role Management", "Role Deleted"),
}
INCIDENT_LABEL = {
    "DETECTED": "Incident Detected", "ASSIGNED": "Incident Assigned",
    "INVESTIGATION_STARTED": "Investigation Started", "ROOT_CAUSE_IDENTIFIED": "Root Cause Identified",
    "MITIGATION": "Mitigation Logged", "RESOLVED": "Incident Resolved", "REOPENED": "Incident Reopened",
    "CLOSED": "Incident Closed", "SEVERITY_CHANGED": "Severity Changed", "COMMENT": "Comment Added",
    "EVIDENCE_ADDED": "Evidence Added",
}


async def _org_user_ids(db: AsyncSession, organization_id: int | None) -> set[int] | None:
    """None = unscoped (super admin viewing everything)."""
    if organization_id is None:
        return None
    result = await db.execute(select(User.id).where(User.organization_id == organization_id))
    return {row[0] for row in result.all()}


async def _fetch_entries(db: AsyncSession, organization_id: int | None) -> list[dict]:
    entries: list[dict] = []
    org_user_ids = await _org_user_ids(db, organization_id)

    # ── UserActivityLog -> Authentication / User Management ──────
    stmt = select(UserActivityLog).order_by(UserActivityLog.created_at.desc()).limit(SOURCE_LIMIT)
    if org_user_ids is not None:
        stmt = stmt.where(UserActivityLog.user_id.in_(org_user_ids))
    rows = (await db.execute(stmt)).scalars().all()
    subject_ids = {r.user_id for r in rows} | {r.actor_id for r in rows if r.actor_id}
    users = await _users_by_id(db, subject_ids)
    for r in rows:
        module, action, status = USER_ACTIVITY_LABEL.get(r.event_type.value if hasattr(r.event_type, "value") else r.event_type, ("User Management", r.event_type, "SUCCESS"))
        actor = users.get(r.actor_id) if r.actor_id else users.get(r.user_id)
        subject = users.get(r.user_id)
        entries.append({
            "id": f"ual-{r.id}", "timestamp": _as_utc(r.created_at),
            "user_id": r.actor_id or r.user_id, "user_name": actor.full_name if actor else None,
            "action": action, "module": module,
            "resource": subject.full_name if subject else None, "resource_id": str(r.user_id),
            "ip_address": r.ip_address, "status": status, "details": r.message,
            "before_value": None, "after_value": None, "user_agent": r.user_agent,
        })

    # ── RoleAuditLog -> Role Management / Permission Changes ─────
    stmt = select(RoleAuditLog).order_by(RoleAuditLog.created_at.desc()).limit(SOURCE_LIMIT)
    role_org_filter_ids = None
    if organization_id is not None:
        role_ids = (await db.execute(select(Role.id).where(Role.organization_id == organization_id))).scalars().all()
        role_org_filter_ids = set(role_ids)
        stmt = stmt.where(RoleAuditLog.role_id.in_(role_org_filter_ids))
    rows = (await db.execute(stmt)).scalars().all()
    role_ids = {r.role_id for r in rows}
    roles = await _roles_by_id(db, role_ids)
    actor_ids = {r.actor_id for r in rows if r.actor_id}
    users = await _users_by_id(db, actor_ids)
    for r in rows:
        module, action = ROLE_AUDIT_LABEL.get(r.event_type.value if hasattr(r.event_type, "value") else r.event_type, ("Role Management", r.event_type))
        actor = users.get(r.actor_id)
        role = roles.get(r.role_id)
        entries.append({
            "id": f"ral-{r.id}", "timestamp": _as_utc(r.created_at),
            "user_id": r.actor_id, "user_name": actor.full_name if actor else "System",
            "action": action, "module": module,
            "resource": role.name if role else None, "resource_id": str(r.role_id),
            "ip_address": None, "status": "SUCCESS", "details": r.message,
            "before_value": None, "after_value": None, "user_agent": None,
        })

    # ── IncidentTimelineEvent -> Incident ─────────────────────────
    stmt = select(IncidentTimelineEvent).order_by(IncidentTimelineEvent.created_at.desc()).limit(SOURCE_LIMIT)
    if organization_id is not None:
        incident_ids = (await db.execute(select(Incident.id).where(Incident.organization_id == organization_id))).scalars().all()
        stmt = stmt.where(IncidentTimelineEvent.incident_id.in_(set(incident_ids)))
    rows = (await db.execute(stmt)).scalars().all()
    incident_ids = {r.incident_id for r in rows}
    incidents = await _incidents_by_id(db, incident_ids)
    actor_ids = {r.actor_id for r in rows if r.actor_id}
    users = await _users_by_id(db, actor_ids)
    for r in rows:
        etype = r.event_type.value if hasattr(r.event_type, "value") else r.event_type
        action = INCIDENT_LABEL.get(etype, etype)
        actor = users.get(r.actor_id)
        incident = incidents.get(r.incident_id)
        status = "FAILURE" if etype in ("REOPENED",) else "WARNING" if etype in ("SEVERITY_CHANGED",) else "SUCCESS"
        entries.append({
            "id": f"ite-{r.id}", "timestamp": _as_utc(r.created_at),
            "user_id": r.actor_id, "user_name": actor.full_name if actor else "System",
            "action": action, "module": "Incident",
            "resource": incident.incident_number if incident else None, "resource_id": str(r.incident_id),
            "ip_address": None, "status": status, "details": r.message,
            "before_value": None, "after_value": None, "user_agent": None,
        })

    # ── AuditLog -> everything else (Connection Changes, License Changes, ─
    #    Settings, Document Upload, OCR, AI Extraction, Transformation,
    #    Validation, Reconciliation, Pipeline, Schedule) ───────────────
    stmt = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(SOURCE_LIMIT)
    if organization_id is not None:
        stmt = stmt.where(AuditLog.organization_id == organization_id)
    rows = (await db.execute(stmt)).scalars().all()
    actor_ids = {r.actor_id for r in rows if r.actor_id}
    users = await _users_by_id(db, actor_ids)
    for r in rows:
        actor = users.get(r.actor_id)
        entries.append({
            "id": f"aud-{r.id}", "timestamp": _as_utc(r.timestamp),
            "user_id": r.actor_id, "user_name": actor.full_name if actor else "System",
            "action": r.action, "module": r.module,
            "resource": r.resource, "resource_id": r.resource_id,
            "ip_address": r.ip_address, "status": r.status.value if hasattr(r.status, "value") else r.status,
            "details": r.details, "before_value": r.before_value, "after_value": r.after_value,
            "user_agent": r.user_agent,
        })

    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return entries


async def _users_by_id(db: AsyncSession, ids: set[int]) -> dict[int, User]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    result = await db.execute(select(User).where(User.id.in_(ids)))
    return {u.id: u for u in result.scalars().all()}


async def _roles_by_id(db: AsyncSession, ids: set[int]) -> dict[int, Role]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    result = await db.execute(select(Role).where(Role.id.in_(ids)))
    return {r.id: r for r in result.scalars().all()}


async def _incidents_by_id(db: AsyncSession, ids: set[int]) -> dict[int, Incident]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    result = await db.execute(select(Incident).where(Incident.id.in_(ids)))
    return {i.id: i for i in result.scalars().all()}


def _apply_filters(entries: list[dict], *, user: str | None, module: str | None, action: str | None,
                    status: str | None, resource: str | None, date_from: datetime | None, date_to: datetime | None,
                    search: str | None) -> list[dict]:
    def keep(e):
        if user and (not e["user_name"] or user.lower() not in e["user_name"].lower()):
            return False
        if module and e["module"] != module:
            return False
        if action and action.lower() not in e["action"].lower():
            return False
        if status and e["status"] != status:
            return False
        if resource and (not e["resource"] or resource.lower() not in e["resource"].lower()):
            return False
        if date_from and e["timestamp"] < date_from:
            return False
        if date_to and e["timestamp"] > date_to:
            return False
        if search:
            haystack = " ".join(str(e.get(k) or "") for k in ("user_name", "action", "module", "resource", "details")).lower()
            if search.lower() not in haystack:
                return False
        return True
    return [e for e in entries if keep(e)]


def _shape_summary(e: dict) -> dict:
    return {
        "id": e["id"], "timestamp": e["timestamp"], "user_id": e["user_id"], "user_name": e["user_name"],
        "action": e["action"], "module": e["module"], "resource": e["resource"], "resource_id": e["resource_id"],
        "ip_address": e["ip_address"], "status": e["status"], "details": _mask_text(e["details"]),
    }


def _shape_detail(e: dict) -> dict:
    return {
        **_shape_summary(e),
        "before_value": _mask_value(e["before_value"]),
        "after_value": _mask_value(e["after_value"]),
        "user_agent": e["user_agent"],
    }


async def list_entries(
    db: AsyncSession, *, organization_id: int | None, user: str | None = None, module: str | None = None,
    action: str | None = None, status: str | None = None, resource: str | None = None,
    date_from: datetime | None = None, date_to: datetime | None = None, search: str | None = None,
    page: int = 1, page_size: int = 20,
) -> tuple[list[dict], int]:
    entries = await _fetch_entries(db, organization_id)
    entries = _apply_filters(entries, user=user, module=module, action=action, status=status, resource=resource, date_from=date_from, date_to=date_to, search=search)
    total = len(entries)
    start = (page - 1) * page_size
    page_entries = entries[start:start + page_size]
    return [_shape_summary(e) for e in page_entries], total


async def get_entry(db: AsyncSession, organization_id: int | None, entry_id: str) -> dict | None:
    entries = await _fetch_entries(db, organization_id)
    for e in entries:
        if e["id"] == entry_id:
            return _shape_detail(e)
    return None


async def export_entries(
    db: AsyncSession, *, organization_id: int | None, fmt: str, **filters,
) -> tuple[bytes, str, str]:
    """Returns (content, media_type, filename)."""
    entries = await _fetch_entries(db, organization_id)
    entries = _apply_filters(entries, **filters)
    rows = [_shape_summary(e) for e in entries]

    headers = ["Timestamp", "User", "Action", "Module", "Resource", "IP Address", "Status", "Details"]

    def row_values(r):
        return [
            r["timestamp"].isoformat(), r["user_name"] or "System", r["action"], r["module"],
            r["resource"] or "", r["ip_address"] or "", r["status"], r["details"] or "",
        ]

    if fmt == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Audit Logs"
        ws.append(headers)
        for r in rows:
            ws.append(row_values(r))
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "audit-logs.xlsx"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for r in rows:
        writer.writerow(row_values(r))
    return buf.getvalue().encode("utf-8"), "text/csv", "audit-logs.csv"


MODULES = AUDIT_MODULES
