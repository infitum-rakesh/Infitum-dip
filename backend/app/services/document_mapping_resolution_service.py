"""
document_mapping_resolution_service — Scheduled Mapping (Document
Intelligence: Dynamic JSON Extraction spec, section 7).

Two responsibilities, kept in one small module since they're tightly
coupled (the second IS the precedence rule section 7.2 describes):

1. Org-level default mapping profile administration (section 7.3) —
   plain CRUD over DocumentMappingDefault, one row per
   (organization_id, document_type).
2. resolve_scheduled_mapping() — the actual precedence logic a scheduled
   run consults once extraction succeeds:
     Explicit schedule mapping (DocumentScheduleConfig.mapping_id)
               -> Compatible document-type default (DocumentMappingDefault)
               -> No mapping — preserve original JSON (returns None)
   "Do not silently choose an unrelated profile" and "if a configured
   profile is inactive, deleted, inaccessible, or incompatible, mark the
   mapping stage as failed... according to policy" are both taken
   literally: an explicit OR default mapping_id that doesn't resolve to a
   real, org-owned, document-sourced, PUBLISHED mapping raises
   ScheduledMappingConfigError rather than falling through to "no
   mapping" or to the other precedence tier — see each docstring below
   for exactly which fallback the two tiers get (they differ on purpose:
   an explicit schedule mapping going bad does NOT fall through to the
   org default, since that would be exactly the kind of "silently choose
   an unrelated profile" behavior section 7.2 forbids).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditStatus, record_event
from app.models.document_mapping_default import DocumentMappingDefault
from app.models.document_schedule import DocumentScheduleConfig
from app.models.schedule import Schedule
from app.services import mapping_service


class ScheduledMappingConfigError(Exception):
    """A schedule's explicit mapping, or the org's document-type default,
    is configured but unusable (deleted/wrong org/not document-sourced/
    incompatible document_type/not PUBLISHED) — the caller (schedule_
    service.py) records this as a FAILED DocumentMappedOutput row and
    continues processing the rest of the batch; it never fails the
    document's own extraction result."""


@dataclass
class ResolvedScheduledMapping:
    mapping_id: int
    mapping_name: str
    version_number: int
    source: str  # "EXPLICIT" | "DEFAULT" — the precedence tier that resolved this mapping


async def _load_and_validate(db: AsyncSession, *, mapping_id: int, organization_id: int | None, document_type: str, label: str, tier: str) -> ResolvedScheduledMapping:
    """`label` is the human-readable prefix used in error messages (e.g.
    "The schedule's explicit"); `tier` is the literal "EXPLICIT"/"DEFAULT"
    recorded on the returned ResolvedScheduledMapping.source."""
    mapping = await mapping_service.get_mapping(db, mapping_id, organization_id)
    if mapping is None:
        raise ScheduledMappingConfigError(f"{label} mapping profile {mapping_id} was not found (or belongs to a different organisation).")
    if mapping.source_document_type is None:
        raise ScheduledMappingConfigError(f"{label} mapping profile '{mapping.name}' is not a Document Intelligence mapping profile — its source is a connection, not a document_type.")
    if mapping.source_document_type not in ("GENERIC", document_type):
        raise ScheduledMappingConfigError(
            f"{label} mapping profile '{mapping.name}' is built for document_type '{mapping.source_document_type}', "
            f"which is incompatible with this schedule's document_type '{document_type}'."
        )
    if mapping.current_published_version is None:
        raise ScheduledMappingConfigError(f"{label} mapping profile '{mapping.name}' has no PUBLISHED version yet.")
    version = await mapping_service.get_version(db, mapping_id, mapping.current_published_version)
    if version is None or version.status != "PUBLISHED":
        raise ScheduledMappingConfigError(f"{label} mapping profile '{mapping.name}' v{mapping.current_published_version} is not PUBLISHED.")
    return ResolvedScheduledMapping(mapping_id=mapping.id, mapping_name=mapping.name, version_number=version.version_number, source=tier)


async def resolve_scheduled_mapping(
    db: AsyncSession, *, organization_id: int | None, schedule_mapping_id: int | None, document_type: str | None,
) -> ResolvedScheduledMapping | None:
    """Returns the mapping to apply, or None meaning "no mapping
    configured — preserve the original extracted JSON as-is" (section
    7.2's third, non-error option). A document_type-less schedule can
    never resolve anything beyond an explicit mapping (a default is keyed
    by document_type, so there's nothing to look up) — still a normal,
    valid "no mapping" outcome, not an error."""
    if schedule_mapping_id is not None:
        # Explicit schedule mapping is the highest-precedence tier and,
        # per section 7.2, never silently falls through to the org
        # default if it's bad — that default might belong to a
        # completely different document_type's intent. A bad explicit
        # config is always a reportable failure, never silently ignored.
        return await _load_and_validate(
            db, mapping_id=schedule_mapping_id, organization_id=organization_id,
            document_type=document_type or "", label="The schedule's explicit", tier="EXPLICIT",
        )

    if document_type is None:
        return None

    default = await get_default(db, organization_id, document_type)
    if default is None:
        return None
    return await _load_and_validate(
        db, mapping_id=default.mapping_id, organization_id=organization_id,
        document_type=document_type, label=f"The organisation's default ({document_type})", tier="DEFAULT",
    )


async def validate_schedule_mapping_id(db: AsyncSession, *, mapping_id: int, organization_id: int | None, document_type: str | None) -> None:
    """Save-time counterpart to resolve_scheduled_mapping()'s explicit-
    mapping branch above — called from schedule_service.py's
    _validate_and_apply_document_config so a schedule referencing a
    deleted/foreign/connection-sourced/incompatible/unpublished mapping
    fails loudly at save time (same cross-cutting DB-lookup validation
    convention that function already applies to source_connection_id/
    invoice_erp_connection_id), rather than only surfacing as a
    mapping_failed stat the first time the schedule actually runs. Runs
    the identical check resolve_scheduled_mapping() would run for this
    same (mapping_id, document_type) pair, so there is no drift between
    what's accepted here and what's accepted at execution time."""
    await _load_and_validate(
        db, mapping_id=mapping_id, organization_id=organization_id,
        document_type=document_type or "", label="The schedule's selected", tier="EXPLICIT",
    )


# ── Default profile administration (section 7.3) ────────────────────

async def get_default(db: AsyncSession, organization_id: int | None, document_type: str) -> DocumentMappingDefault | None:
    stmt = select(DocumentMappingDefault).where(DocumentMappingDefault.document_type == document_type)
    if organization_id is not None:
        stmt = stmt.where(DocumentMappingDefault.organization_id == organization_id)
    else:
        stmt = stmt.where(DocumentMappingDefault.organization_id.is_(None))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_defaults(db: AsyncSession, organization_id: int | None) -> list[DocumentMappingDefault]:
    stmt = select(DocumentMappingDefault)
    if organization_id is not None:
        stmt = stmt.where(DocumentMappingDefault.organization_id == organization_id)
    else:
        stmt = stmt.where(DocumentMappingDefault.organization_id.is_(None))
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.document_type)
    return rows


class DefaultMappingError(Exception):
    """A bad request to set_default() — the referenced mapping doesn't
    exist, belongs to another org, isn't document-sourced, or is
    incompatible with the document_type being defaulted."""


async def set_default(db: AsyncSession, *, organization_id: int | None, document_type: str, mapping_id: int, actor_id: int | None) -> DocumentMappingDefault:
    """Upserts the single (organization_id, document_type) row — "prevent
    ambiguous default configurations" (section 7.3) via the UNIQUE
    constraint plus this get-or-update pattern, never a second row for
    the same type. Does NOT require the mapping to be PUBLISHED yet
    (an admin may want to set tomorrow's intended default before
    publishing it) — publication/compatibility is re-checked for real at
    resolution time by resolve_scheduled_mapping(), not here."""
    mapping = await mapping_service.get_mapping(db, mapping_id, organization_id)
    if mapping is None:
        raise DefaultMappingError(f"Mapping profile {mapping_id} was not found.")
    if mapping.source_document_type is None:
        raise DefaultMappingError(f"Mapping profile '{mapping.name}' is not a Document Intelligence mapping profile — its source is a connection, not a document_type.")
    if mapping.source_document_type not in ("GENERIC", document_type):
        raise DefaultMappingError(f"Mapping profile '{mapping.name}' is built for document_type '{mapping.source_document_type}', not '{document_type}'.")

    existing = await get_default(db, organization_id, document_type)
    if existing:
        existing.mapping_id = mapping_id
        existing.updated_by = actor_id
        row = existing
    else:
        row = DocumentMappingDefault(organization_id=organization_id, document_type=document_type, mapping_id=mapping_id, created_by=actor_id, updated_by=actor_id)
        db.add(row)

    await record_event(
        db, organization_id=organization_id, actor_id=actor_id, action="Document Mapping Default Set", module="Column Mapping",
        resource=document_type, resource_id=str(mapping_id), status=AuditStatus.SUCCESS,
        details=f"Set '{mapping.name}' as the default mapping profile for document_type '{document_type}'.", commit=False,
    )
    await db.commit()
    await db.refresh(row)
    return row


async def list_schedules_using_mapping(db: AsyncSession, *, mapping_id: int, organization_id: int | None) -> list[dict]:
    """Section 7.3 — "view which schedules use each profile". Two distinct
    usage kinds, matching the precedence rule exactly: EXPLICIT (this
    schedule's own DocumentScheduleConfig.mapping_id points straight at
    it) and DEFAULT (the schedule has no explicit mapping_id of its own,
    but its document_type's organisation default currently resolves to
    this mapping — an explicit override on the SAME schedule always wins
    at resolution time, so such a schedule is never double-counted as
    using the default it's actually ignoring)."""
    stmt = select(Schedule, DocumentScheduleConfig).join(
        DocumentScheduleConfig, DocumentScheduleConfig.schedule_id == Schedule.id
    )
    if organization_id is not None:
        stmt = stmt.where(Schedule.organization_id == organization_id)
    else:
        stmt = stmt.where(Schedule.organization_id.is_(None))
    rows = (await db.execute(stmt)).all()

    defaults = {d.document_type: d.mapping_id for d in await list_defaults(db, organization_id)}

    usages: list[dict] = []
    for schedule, config in rows:
        if config.mapping_id == mapping_id:
            usages.append({"schedule_id": schedule.id, "schedule_name": schedule.name, "document_type": config.document_type, "usage": "EXPLICIT"})
        elif config.mapping_id is None and config.document_type and defaults.get(config.document_type) == mapping_id:
            usages.append({"schedule_id": schedule.id, "schedule_name": schedule.name, "document_type": config.document_type, "usage": "DEFAULT"})
    return usages


async def clear_default(db: AsyncSession, *, organization_id: int | None, document_type: str, actor_id: int | None) -> None:
    existing = await get_default(db, organization_id, document_type)
    if existing is None:
        return
    await db.delete(existing)
    await record_event(
        db, organization_id=organization_id, actor_id=actor_id, action="Document Mapping Default Removed", module="Column Mapping",
        resource=document_type, resource_id=str(existing.mapping_id), status=AuditStatus.SUCCESS,
        details=f"Removed the default mapping profile for document_type '{document_type}'.", commit=False,
    )
    await db.commit()
