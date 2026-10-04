"""
Settings service — Administration -> Settings.

Organization-level configuration. Reuses the same lazy-create-on-first-
read pattern as role_service.ensure_default_roles: no seeding step is
needed anywhere else, the first GET for an org creates its settings row
with DEFAULT_SETTINGS.

Every successful update is recorded through audit_log_service.record_event
under the "Settings" category — the audit_log module already had this
category defined with nothing writing to it (see models/audit_log.py);
this is the first real source for it.

Nothing here is wired into what it configures (session_timeout_minutes
doesn't change the JWT's real expiry, max_upload_size_mb doesn't affect
Documents' real upload limit, etc.) — same "stored + displayed, not yet
enforced" scope boundary used everywhere in this codebase for settings
that would otherwise require touching an unrelated module's engine. The
one exception is "anomaly_detection" (AD9) — AnomalyJobEditor.jsx's
create-new-job path genuinely reads these defaults (via the dedicated
/anomaly/configuration routes in api/anomaly.py, gated on
ANOMALY_DETECTION permissions rather than Settings — see
services/anomaly_service.get_configuration/update_configuration) to
pre-fill threshold_config/baseline_config/retention_days/on_critical.
"""
import copy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditStatus, record_event
from app.models.organization import Organization
from app.models.settings import DEFAULT_SETTINGS, OrganizationSettings
from app.models.user import User

SECTION_LABELS = {
    "general": "General", "data": "Data", "document_intelligence": "Document Intelligence",
    "ai": "AI", "notifications": "Notifications", "security": "Security",
    "audit": "Audit", "pipelines": "Pipelines", "anomaly_detection": "Anomaly Detection",
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


async def get_or_create(db: AsyncSession, organization_id: int) -> OrganizationSettings:
    result = await db.execute(select(OrganizationSettings).where(OrganizationSettings.organization_id == organization_id))
    row = result.scalar_one_or_none()
    if row is None:
        row = OrganizationSettings(organization_id=organization_id, values=copy.deepcopy(DEFAULT_SETTINGS))
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def _effective_values(row: OrganizationSettings) -> dict:
    """Merges stored values under the current defaults so a newly added
    setting always has a value even for orgs whose row predates it."""
    return _deep_merge(DEFAULT_SETTINGS, row.values or {})


def effective_document_intelligence_settings(row: OrganizationSettings | None) -> dict:
    """Public accessor for document_service.py's real OCR pipeline
    (Multilingual Document Processing, Phase 4) — same deep-merge
    guarantee _effective_values() gives every other reader, so an org
    whose settings row predates supported_languages/primary_language
    still gets the real DEFAULT_SETTINGS values for them rather than a
    KeyError or a silently-empty dict. Returns DEFAULT_SETTINGS's
    document_intelligence block verbatim when there's no row at all
    (org_id is None — e.g. a structured/non-tenant source)."""
    if row is None:
        return copy.deepcopy(DEFAULT_SETTINGS["document_intelligence"])
    return _effective_values(row)["document_intelligence"]


def effective_ai_settings(row: OrganizationSettings | None) -> dict:
    """Same accessor pattern as effective_document_intelligence_settings()
    above, for the "ai" settings block — lets document_service.py's
    _pick_ai_connections() honor Settings > AI > "AI Provider" (ai_provider)
    as a real preference instead of ignoring it (see that function's own
    docstring for why this exists)."""
    if row is None:
        return copy.deepcopy(DEFAULT_SETTINGS["ai"])
    return _effective_values(row)["ai"]


async def to_out(db: AsyncSession, org: Organization, row: OrganizationSettings) -> dict:
    values = _effective_values(row)
    updated_by_name = None
    if row.updated_by:
        result = await db.execute(select(User.full_name).where(User.id == row.updated_by))
        updated_by_name = result.scalar_one_or_none()

    return {
        "organization_id": org.id,
        "organization_name": org.name,
        "logo_url": org.logo_url,
        "updated_by": row.updated_by,
        "updated_by_name": updated_by_name,
        "updated_at": row.updated_at,
        **values,
    }


async def update_settings(
    db: AsyncSession, platform_db: AsyncSession, org: Organization, row: OrganizationSettings, body, actor_id: int | None,
    ip_address: str | None = None,
) -> OrganizationSettings:
    """`org` (PlatformBase) is committed via `platform_db`; `row`
    (OrganizationSettings, TenantBase) and the audit event (also
    TenantBase) via `db` — two separate sessions/physical databases once
    an org has migrated, so `org` can never be `db.add()`ed onto the same
    session that owns `row` (a plain ORM object can only be attached to
    one session's identity map at a time — mixing them raises)."""
    patch = body.model_dump(exclude_unset=True)
    changed_sections = []

    if "organization_name" in patch and patch["organization_name"] and patch["organization_name"] != org.name:
        org.name = patch["organization_name"]
        changed_sections.append("Organization Name")
    if "logo_url" in patch and patch["logo_url"] != org.logo_url:
        org.logo_url = patch["logo_url"]
        changed_sections.append("Logo")

    current = _effective_values(row)
    new_values = copy.deepcopy(current)
    for section in SECTION_LABELS:
        if section in patch and patch[section] is not None:
            new_values[section] = _deep_merge(current.get(section, {}), patch[section])
            changed_sections.append(SECTION_LABELS[section])

    row.values = new_values
    row.updated_by = actor_id
    db.add(row)
    platform_db.add(org)

    if changed_sections:
        await record_event(
            db, organization_id=org.id, actor_id=actor_id, action="Settings Updated", module="Settings",
            resource=", ".join(changed_sections), status=AuditStatus.SUCCESS, ip_address=ip_address,
            details=f"Updated: {', '.join(changed_sections)}", commit=False,
        )

    await db.commit()
    await db.refresh(row)
    await platform_db.commit()
    await platform_db.refresh(org)
    return row
