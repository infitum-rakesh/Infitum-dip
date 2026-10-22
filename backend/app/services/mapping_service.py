"""
mapping_service — the API-facing layer for Object & Column Mapping
(Phase 4; real immutable versioning + publish lifecycle added by CM2, see
models/column_mapping_version.py). Owns ColumnMapping/ColumnMappingVersion
CRUD and is the seam between the pure mapping/ engine (auto_mapper,
mapper, datatype_mapper — no I/O) and the real connectors that supply
live source/target column lists.

Source/target schema is always re-fetched live here, never cached from
the stored mapping row — see models/column_mapping.py module docstring.

Versioning model (mirrors services/pipeline_service.py's save_version()
exactly): every save creates a NEW ColumnMappingVersion row
(create_new_version) — nothing is ever overwritten. Only a version whose
status has been advanced through DRAFT -> VALIDATED -> PUBLISHED via
run_validate_version()/publish_version() may be attached to a production
Pipeline Builder node (enforced server-side in
execution/mapping_execution.py::resolve_mapping_version(), never just
filtered client-side — see MODULE_SCOPE note in api/pipeline_mapping.py).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal

from app.connectors.base import ColumnInfo, ConnectorError
from app.mapping import auto_mapper, mapper
from app.models.audit_log import AuditStatus, record_event
from app.models.column_mapping import ColumnMapping
from app.models.column_mapping_usage import ColumnMappingUsage
from app.models.column_mapping_version import ColumnMappingVersion
from app.models.connection import Connection
from app.models.pipeline import Pipeline
from app.models.user import User
from app.services import connection_service, document_service, document_taxonomies
from app.schemas.mapping import ObjectRef

# Document Mapping Profiles — the uppercase family strings
# mapping/datatype_mapper.py's classify() pattern-matches on (confirmed:
# "STRING"/"NUMERIC"/"DATE"/"BOOLEAN" each match that module's own regex
# families directly), so auto_map()/validate_mapping() treat a document's
# extracted field exactly like a real reflected DB column type with zero
# changes to either pure engine.
_DOCUMENT_FIELD_TYPE_MAP = {"string": "STRING", "number": "NUMERIC", "date": "DATE", "boolean": "BOOLEAN"}

# Invoice Schema target — a curated allowlist of ApInvoice's genuine
# business/data columns, NOT a blanket introspection of every column on
# the model. ApInvoice also carries internal workflow/decision-engine
# bookkeeping (status, final_decision, extractor_confidence,
# erp_transaction_id, rule_version, ...) that must never be offered as a
# mapping TARGET — those are written by invoice_decision_service.py/
# invoice_processing_service.py's own logic, not by a user-defined
# mapping. Keeping this list hand-maintained (rather than "every column
# except a blocklist") means a future internal column added to the model
# is safe-by-default — it simply doesn't appear here until someone
# deliberately decides it belongs in this list.
_INVOICE_SCHEMA_TARGET_COLUMNS = [
    "invoice_number", "invoice_date", "due_date", "vendor_name", "purchase_order",
    "currency", "payment_terms", "payment_reference",
    "vendor_gstin", "vendor_pan", "buyer_gstin", "place_of_supply",
    "tds_section", "tds_rate", "tds_amount",
    "subtotal", "tax", "cgst_amount", "sgst_amount", "igst_amount", "cess_amount",
    "discount", "freight", "total",
]


class MappingValidationError(Exception):
    """Bad request — a referenced connection/object doesn't exist, or the
    mapping definition can't be turned into SQL."""


class MappingLifecycleError(Exception):
    """A version-lifecycle action was attempted out of order — e.g.
    publishing a version that hasn't been validated cleanly, or retiring
    a version still attached to a pipeline without confirm=True."""


async def _resolve_connection(db: AsyncSession, connection_id: int, organization_id: int | None) -> Connection:
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise MappingValidationError(f"Connection {connection_id} was not found.")
    return conn


async def _fetch_document_source_columns(db: AsyncSession, organization_id: int | None, document_type: str) -> list[ColumnInfo]:
    """Document Mapping Profiles' SOURCE schema — never a real connector.
    Reuses exactly the two functions document_taxonomies.py's own module
    docstring says are designed to compose: build_seed_schema() for the
    canonical/ad-hoc taxonomy's own field names, widened by
    discover_known_field_names()'s real, data-driven extra names mined
    from this org's own past documents of the type (the same widening
    extraction's own detect_schema() already does). Table-shaped fields
    (e.g. line_items) are excluded — a mapping row maps one scalar value
    to one target column; a table needs its own row-expansion handling,
    the same disclosed, not-yet-built limitation build_dataset() already
    has for a SECOND table field (see document_service.py's
    _reshape_generic_fields() docstring)."""
    extra_names = await document_service.discover_known_field_names(db, organization_id, document_type=document_type)
    schema = document_taxonomies.build_seed_schema(document_type, extra_names)
    return [
        ColumnInfo(name=f["name"], data_type=_DOCUMENT_FIELD_TYPE_MAP.get(f["type"], "STRING"), nullable=True)
        for f in schema if f["type"] != "table"
    ]


def _fetch_invoice_schema_target_columns() -> list[ColumnInfo]:
    """Document Mapping Profiles' "INVOICE_SCHEMA" TARGET — ApInvoice's
    own real typed columns, introspected directly via SQLAlchemy (never a
    hand-duplicated list that could drift from the actual model), but
    filtered to the curated _INVOICE_SCHEMA_TARGET_COLUMNS allowlist —
    see that constant's own comment for why internal workflow/decision
    columns are deliberately excluded rather than blanket-exposed."""
    from app.models.invoice_intelligence.core import ApInvoice
    by_name = {c.name: c for c in ApInvoice.__table__.columns}
    return [
        ColumnInfo(name=name, data_type=str(by_name[name].type), nullable=by_name[name].nullable)
        for name in _INVOICE_SCHEMA_TARGET_COLUMNS if name in by_name
    ]


async def fetch_columns(db: AsyncSession, ref: ObjectRef, organization_id: int | None) -> list:
    # Document Mapping Profiles — see schemas/mapping.py's ObjectRef
    # docstring. These two branches never touch a Connection row at all;
    # every other caller (run_validate_version, check_staleness,
    # run_auto_map, run_validate, execution/mapping_execution.py's own
    # live revalidation) goes through this ONE function, so branching
    # here is what makes every one of them document-aware "for free",
    # with zero changes to any of their own bodies.
    if ref.document_type is not None:
        return await _fetch_document_source_columns(db, organization_id, ref.document_type)
    if ref.target_format == "INVOICE_SCHEMA":
        return _fetch_invoice_schema_target_columns()
    conn = await _resolve_connection(db, ref.connection_id, organization_id)
    try:
        columns, _ = await connection_service.get_columns(conn, ref.object_name, ref.schema_name, False)
    except ConnectorError as exc:
        raise MappingValidationError(str(exc)) from exc
    return columns


def columns_to_dicts(columns: list) -> list[dict]:
    return [{"name": c.name, "data_type": c.data_type, "nullable": c.nullable, "default": c.default} for c in columns]


async def _snapshot_column_counts(
    db: AsyncSession, organization_id: int | None, source: ObjectRef, target: ObjectRef,
) -> tuple[int | None, int | None]:
    """Best-effort live column-count snapshot taken once at version-creation
    time — the "then" baseline the staleness route (api/mapping.py) diffs a
    later live re-fetch against. Swallows a fetch failure into (None, None)
    rather than blocking the save — a transient connector hiccup shouldn't
    stop someone from saving a mapping version; it just means staleness-by-
    count can't be computed for this one version later (the live drift
    issues validate_mapping() detects by column name still work either
    way)."""
    try:
        source_count = len(await fetch_columns(db, source, organization_id))
        target_count = len(await fetch_columns(db, target, organization_id))
        return source_count, target_count
    except MappingValidationError:
        return None, None


# ── CRUD (header) ────────────────────────────────────────────────

def _validate_ref_discriminators(source: ObjectRef, target: ObjectRef) -> None:
    """Document Mapping Profiles — exactly one of connection_id/
    document_type on the source side, and target_format must agree with
    whether target_connection_id is set. Checked once, here, rather than
    relying on every caller to get this right."""
    if source.document_type is not None and source.connection_id is not None:
        raise MappingValidationError("A mapping's source must be either a connection or a Document Intelligence document_type, not both.")
    if source.document_type is None and source.connection_id is None:
        raise MappingValidationError("A mapping's source needs either a connection_id or a document_type.")
    if target.target_format == "INVOICE_SCHEMA" and target.connection_id is not None:
        raise MappingValidationError("An INVOICE_SCHEMA target has no connection — leave target.connection_id unset.")
    if (target.target_format or "DB_TABLE") == "DB_TABLE" and target.connection_id is None:
        raise MappingValidationError("A DB_TABLE target needs a connection_id.")


async def create_mapping(
    db: AsyncSession, *, organization_id: int | None, name: str, source: ObjectRef, target: ObjectRef,
    mappings: list[dict], created_by: int | None,
) -> ColumnMapping:
    _validate_ref_discriminators(source, target)
    # A real connection must genuinely belong to this org (or
    # organization_id is None — a true platform-wide/super-admin
    # context) — never trust a connection_id from the request body as-is.
    # Without this, any authenticated user could save a mapping pointing
    # at another tenant's connection and later run Preview/Validate/Load
    # against it. Skipped for a document_type source or an INVOICE_SCHEMA
    # target — neither has a connection_id to even check.
    if organization_id is not None:
        if source.connection_id is not None:
            await _resolve_connection(db, source.connection_id, organization_id)
        if target.connection_id is not None:
            await _resolve_connection(db, target.connection_id, organization_id)
    row = ColumnMapping(
        organization_id=organization_id, name=name,
        source_connection_id=source.connection_id, source_schema=source.schema_name, source_object=source.object_name,
        source_document_type=source.document_type,
        target_connection_id=target.connection_id, target_schema=target.schema_name, target_object=target.object_name,
        target_format=target.target_format or "DB_TABLE",
        mappings=mappings, status="DRAFT",  # legacy columns, kept only for pre-CM1 read compat — see models/column_mapping.py docstring
        latest_version=1, current_published_version=None,
        created_by=created_by, updated_by=created_by,
    )
    db.add(row)
    await db.flush()  # need row.id before the version FK can be written
    source_count, target_count = await _snapshot_column_counts(db, organization_id, source, target)
    version = ColumnMappingVersion(
        mapping_id=row.id, version_number=1, status="DRAFT", mappings=mappings,
        source_connection_id=source.connection_id, source_schema=source.schema_name, source_object=source.object_name,
        source_document_type=source.document_type,
        target_connection_id=target.connection_id, target_schema=target.schema_name, target_object=target.object_name,
        target_format=target.target_format or "DB_TABLE",
        source_column_count=source_count, target_column_count=target_count,
        change_description="Initial version.", created_by=created_by,
    )
    db.add(version)
    source_label = source.object_name or f"document_type={source.document_type}"
    target_label = target.object_name or target.target_format or "DB_TABLE"
    await record_event(
        db, organization_id=organization_id, actor_id=created_by, action="Column Mapping Created", module="Column Mapping",
        resource=name, resource_id=str(row.id), status=AuditStatus.SUCCESS,
        details=f"{source_label} -> {target_label}, {len(mappings)} row(s), v1 DRAFT", commit=False,
    )
    await db.commit()
    await db.refresh(row)
    return row


async def update_mapping(db: AsyncSession, row: ColumnMapping, *, name: str | None, updated_by: int | None) -> ColumnMapping:
    """Header-only edit (name). Mapping-row edits go through
    create_new_version() instead — see module docstring."""
    if name:
        row.name = name
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


async def delete_mapping(db: AsyncSession, row: ColumnMapping, *, actor_id: int | None = None) -> None:
    # ColumnMappingVersion/ColumnMappingUsage rows have no ORM cascade
    # configured (they're independent TenantBase tables, not a
    # relationship) — clean them up explicitly so deleting a mapping
    # doesn't leave orphaned version/usage rows behind.
    mapping_id, mapping_name, organization_id = row.id, row.name, row.organization_id
    for tbl, col in ((ColumnMappingVersion, ColumnMappingVersion.mapping_id), (ColumnMappingUsage, ColumnMappingUsage.mapping_id)):
        rows = (await db.execute(select(tbl).where(col == row.id))).scalars().all()
        for r in rows:
            await db.delete(r)
    await db.delete(row)
    await record_event(
        db, organization_id=organization_id, actor_id=actor_id, action="Column Mapping Deleted", module="Column Mapping",
        resource=mapping_name, resource_id=str(mapping_id), status=AuditStatus.SUCCESS, commit=False,
    )
    await db.commit()


async def get_mapping(db: AsyncSession, mapping_id: int, organization_id: int | None) -> ColumnMapping | None:
    stmt = select(ColumnMapping).where(ColumnMapping.id == mapping_id)
    if organization_id is not None:
        stmt = stmt.where(ColumnMapping.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_mappings(db: AsyncSession, organization_id: int | None, *, published_only: bool = False) -> tuple[list[ColumnMapping], int]:
    """published_only=True — the "Select Existing Mapping" picker on a
    Pipeline Builder column_mapping_step node (spec section 14/16) only
    ever shows mappings with a real PUBLISHED version, since "only
    PUBLISHED mappings can be used in production pipelines" (section 13).
    This is a UI convenience filter, NOT the enforcement point — the real
    gate is execution/mapping_execution.py::resolve_mapping_version() and
    api/pipeline_mapping.py's /attach route, both of which re-check
    PUBLISHED server-side regardless of what this filter already hid."""
    stmt = select(ColumnMapping)
    if organization_id is not None:
        stmt = stmt.where(ColumnMapping.organization_id == organization_id)
    if published_only:
        stmt = stmt.where(ColumnMapping.current_published_version.isnot(None))
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.updated_at, reverse=True)
    return rows, len(rows)


async def user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


# ── Versioning / publish lifecycle (CM2) ────────────────────────────

async def list_versions(db: AsyncSession, mapping_id: int) -> list[ColumnMappingVersion]:
    stmt = select(ColumnMappingVersion).where(ColumnMappingVersion.mapping_id == mapping_id)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.version_number, reverse=True)
    return rows


async def get_version(db: AsyncSession, mapping_id: int, version_number: int) -> ColumnMappingVersion | None:
    stmt = select(ColumnMappingVersion).where(
        ColumnMappingVersion.mapping_id == mapping_id, ColumnMappingVersion.version_number == version_number,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_effective_mappings(db: AsyncSession, mapping: ColumnMapping) -> ColumnMappingVersion | None:
    """The version the standalone Column Mapping module's UI/backward-
    compat read shim should show as "the current mapping" — the newest
    PUBLISHED version if one exists, else the newest version of any
    status (so a brand-new never-published mapping still shows its
    v1 DRAFT rather than nothing). Never used by production pipeline
    execution — that path always resolves an explicit pinned
    (mapping_id, mapping_version), see execution/mapping_execution.py."""
    version_number = mapping.current_published_version or mapping.latest_version
    return await get_version(db, mapping.id, version_number)


async def create_new_version(
    db: AsyncSession, mapping: ColumnMapping, *, mappings: list[dict], change_description: str | None,
    unmapped_column_policy: str, batch_size: int | None, on_conversion_failure: str, created_by: int | None,
) -> ColumnMappingVersion:
    """Always INSERTs a new row — never overwrites a prior version, exactly
    like services/pipeline_service.py::save_version(). Snapshots the
    header's CURRENT source/target refs onto the new version (a version
    created today describes today's source/target; earlier versions keep
    whatever they were snapshotted against, even if that differs)."""
    new_version_number = mapping.latest_version + 1
    source_ref = ObjectRef(
        connection_id=mapping.source_connection_id, schema_name=mapping.source_schema, object_name=mapping.source_object,
        document_type=mapping.source_document_type,
    )
    target_ref = ObjectRef(
        connection_id=mapping.target_connection_id, schema_name=mapping.target_schema, object_name=mapping.target_object,
        target_format=mapping.target_format,
    )
    source_count, target_count = await _snapshot_column_counts(db, mapping.organization_id, source_ref, target_ref)
    version = ColumnMappingVersion(
        mapping_id=mapping.id, version_number=new_version_number, status="DRAFT", mappings=mappings,
        source_connection_id=mapping.source_connection_id, source_schema=mapping.source_schema, source_object=mapping.source_object,
        source_document_type=mapping.source_document_type,
        target_connection_id=mapping.target_connection_id, target_schema=mapping.target_schema, target_object=mapping.target_object,
        target_format=mapping.target_format,
        source_column_count=source_count, target_column_count=target_count,
        unmapped_column_policy=unmapped_column_policy, batch_size=batch_size, on_conversion_failure=on_conversion_failure,
        change_description=change_description, created_by=created_by,
    )
    db.add(version)
    mapping.latest_version = new_version_number
    mapping.updated_by = created_by
    await record_event(
        db, organization_id=mapping.organization_id, actor_id=created_by, action="Column Mapping Version Created", module="Column Mapping",
        resource=mapping.name, resource_id=f"{mapping.id}:v{new_version_number}", status=AuditStatus.SUCCESS,
        details=change_description, commit=False,
    )
    await db.commit()
    await db.refresh(version)
    return version


async def run_validate_version(db: AsyncSession, *, organization_id: int | None, version: ColumnMappingVersion, actor_id: int | None = None) -> dict:
    """Live-refetches source/target schema (never cached) and persists the
    result onto the version row. A version advances to VALIDATED only
    when the result carries no blocking ERROR (mapper's overall_status
    "CONFLICT") — a WARNING-only result still counts as validated (and
    publishable), matching how the standalone editor's own Validate
    button has always treated WARNING as non-blocking."""
    source_cols = columns_to_dicts(await fetch_columns(
        db, ObjectRef(
            connection_id=version.source_connection_id, schema_name=version.source_schema, object_name=version.source_object,
            document_type=version.source_document_type,
        ),
        organization_id,
    ))
    target_cols = columns_to_dicts(await fetch_columns(
        db, ObjectRef(
            connection_id=version.target_connection_id, schema_name=version.target_schema, object_name=version.target_object,
            target_format=version.target_format,
        ),
        organization_id,
    ))
    result = mapper.validate_mapping(version.mappings, source_cols, target_cols)
    version.validation_result = result
    version.status = "DRAFT" if result["overall_status"] == "CONFLICT" else "VALIDATED"
    blocking = len([i for i in result["issues"] if i["severity"] == "ERROR"])
    await record_event(
        db, organization_id=organization_id, actor_id=actor_id, action="Column Mapping Validated", module="Column Mapping",
        resource_id=f"{version.mapping_id}:v{version.version_number}",
        status=AuditStatus.FAILURE if result["overall_status"] == "CONFLICT" else AuditStatus.WARNING if result["overall_status"] == "WARNING" else AuditStatus.SUCCESS,
        details=f"{result['overall_status']} — {blocking} blocking error(s)", commit=False,
    )
    await db.commit()
    await db.refresh(version)
    return result


async def publish_version(db: AsyncSession, *, mapping: ColumnMapping, version: ColumnMappingVersion, actor_id: int | None) -> ColumnMappingVersion:
    """Publish is a status flip on the ONE version being published — it
    never touches any other version row (so an older PUBLISHED version,
    if one exists, quietly stops being "current" only because
    mapping.current_published_version now points elsewhere; it is not
    itself modified or retired — that's a separate, explicit Retire
    action). Enforced server-side regardless of what the caller already
    checked, since a pipeline node's execution-time resolve also depends
    on this same PUBLISHED gate (execution/mapping_execution.py)."""
    if version.status != "VALIDATED":
        raise MappingLifecycleError(
            f"Version {version.version_number} must be validated with no blocking errors before it can be published "
            f"(current status: {version.status})."
        )
    version.status = "PUBLISHED"
    mapping.current_published_version = version.version_number
    mapping.updated_by = actor_id
    await record_event(
        db, organization_id=mapping.organization_id, actor_id=actor_id, action="Column Mapping Published", module="Column Mapping",
        resource=mapping.name, resource_id=f"{mapping.id}:v{version.version_number}", status=AuditStatus.SUCCESS, commit=False,
    )
    await db.commit()
    await db.refresh(version)
    return version


async def retire_version(
    db: AsyncSession, *, mapping: ColumnMapping, version: ColumnMappingVersion, actor_id: int | None, confirm: bool,
) -> ColumnMappingVersion:
    if version.status != "PUBLISHED":
        raise MappingLifecycleError(f"Only a PUBLISHED version can be retired (current status: {version.status}).")
    usage = await list_usage(db, mapping.id, version_number=version.version_number)
    if usage and not confirm:
        pipeline_names = sorted({u.pipeline_id for u in usage})
        raise MappingLifecycleError(
            f"Version {version.version_number} has been attached to {len(pipeline_names)} pipeline(s) "
            f"({', '.join(f'#{p}' for p in pipeline_names)}). Retiring it does not change what those pipelines "
            f"currently run (they keep executing whatever version their own PipelineVersion pins), but it will "
            f"block selecting this version for any NEW attachment. Pass confirm=true to proceed."
        )
    version.status = "RETIRED"
    if mapping.current_published_version == version.version_number:
        mapping.current_published_version = None
    mapping.updated_by = actor_id
    await record_event(
        db, organization_id=mapping.organization_id, actor_id=actor_id, action="Column Mapping Retired", module="Column Mapping",
        resource=mapping.name, resource_id=f"{mapping.id}:v{version.version_number}", status=AuditStatus.SUCCESS,
        details=f"Attached to {len(usage)} pipeline(s) at time of retirement." if usage else None, commit=False,
    )
    await db.commit()
    await db.refresh(version)
    return version


async def check_staleness(db: AsyncSession, *, organization_id: int | None, version: ColumnMappingVersion) -> dict:
    """Live re-fetches source/target schema and diffs it against this
    version's own snapshot — spec sections 19/20's "MAPPING OUT OF DATE"
    detection. Two independent signals, either one marks stale=True:
    (1) the live column COUNT differs from what was snapshotted at
    version-creation time (source_column_count/target_column_count —
    None for versions predating that column, see the model docstring —
    count-based detection is simply skipped then), (2) validate_mapping()
    itself reports a column-no-longer-exists ERROR for any row this
    version actually references — a real name-level drift signal that
    works regardless of whether counts were ever snapshotted. Never
    mutates the version — purely a read/diff, matching "do not silently
    change production mappings"."""
    source_ref = ObjectRef(
        connection_id=version.source_connection_id, schema_name=version.source_schema, object_name=version.source_object,
        document_type=version.source_document_type,
    )
    target_ref = ObjectRef(
        connection_id=version.target_connection_id, schema_name=version.target_schema, object_name=version.target_object,
        target_format=version.target_format,
    )
    source_cols = columns_to_dicts(await fetch_columns(db, source_ref, organization_id))
    target_cols = columns_to_dicts(await fetch_columns(db, target_ref, organization_id))
    now_source_count, now_target_count = len(source_cols), len(target_cols)

    validation = mapper.validate_mapping(version.mappings, source_cols, target_cols)
    drift_issues = [
        i["message"] for i in validation["issues"]
        if i["severity"] == "ERROR" and "no longer exists" in i["message"]
    ]

    count_drift = (
        (version.source_column_count is not None and version.source_column_count != now_source_count)
        or (version.target_column_count is not None and version.target_column_count != now_target_count)
    )
    stale = count_drift or bool(drift_issues)

    issues = list(drift_issues)
    if version.source_column_count is not None and version.source_column_count != now_source_count:
        issues.append(f"Source object had {version.source_column_count} column(s) when this version was created; it now has {now_source_count}.")
    if version.target_column_count is not None and version.target_column_count != now_target_count:
        issues.append(f"Target object had {version.target_column_count} column(s) when this version was created; it now has {now_target_count}.")

    return {
        "stale": stale, "issues": issues,
        "source_column_count_then": version.source_column_count if version.source_column_count is not None else now_source_count,
        "source_column_count_now": now_source_count,
        "target_column_count_then": version.target_column_count if version.target_column_count is not None else now_target_count,
        "target_column_count_now": now_target_count,
    }


# ── Usage tracking (CM3/CM11 — pipeline attach/upgrade write here) ──

async def record_usage(
    db: AsyncSession, *, organization_id: int | None, mapping_id: int, mapping_version: int,
    pipeline_id: int, pipeline_version: int, node_id: str, created_by: int | None, environment: str = "production",
) -> ColumnMappingUsage:
    row = ColumnMappingUsage(
        organization_id=organization_id, mapping_id=mapping_id, mapping_version=mapping_version,
        pipeline_id=pipeline_id, pipeline_version=pipeline_version, node_id=node_id,
        environment=environment, created_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_usage(db: AsyncSession, mapping_id: int, *, version_number: int | None = None) -> list[ColumnMappingUsage]:
    """Append-only history of attach/upgrade events — see
    models/column_mapping_usage.py's module docstring. This lists every
    event ever recorded, not "what's currently attached"; callers that
    need "used by N *distinct* pipelines right now" should de-duplicate
    by pipeline_id and take the latest row per pipeline (see
    api/mapping.py's usage route, which does exactly that for the
    edit-time warning UI)."""
    stmt = select(ColumnMappingUsage).where(ColumnMappingUsage.mapping_id == mapping_id)
    if version_number is not None:
        stmt = stmt.where(ColumnMappingUsage.mapping_version == version_number)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return rows


async def pipeline_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    result = await db.execute(select(Pipeline.id, Pipeline.name).where(Pipeline.id.in_(ids)))
    return {row.id: row.name for row in result.all()}


async def mapping_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    result = await db.execute(select(ColumnMapping.id, ColumnMapping.name).where(ColumnMapping.id.in_(ids)))
    return {row.id: row.name for row in result.all()}


# ── Engine operations ────────────────────────────────────────────

async def run_auto_map(db: AsyncSession, *, organization_id: int | None, source: ObjectRef, target: ObjectRef, existing: list[dict]) -> list[dict]:
    source_cols = columns_to_dicts(await fetch_columns(db, source, organization_id))
    target_cols = columns_to_dicts(await fetch_columns(db, target, organization_id))
    return auto_mapper.auto_map(source_cols, target_cols, existing)


async def run_validate(db: AsyncSession, *, organization_id: int | None, source: ObjectRef, target: ObjectRef, mappings: list[dict]) -> dict:
    source_cols = columns_to_dicts(await fetch_columns(db, source, organization_id))
    target_cols = columns_to_dicts(await fetch_columns(db, target, organization_id))
    return mapper.validate_mapping(mappings, source_cols, target_cols)


async def run_preview(db: AsyncSession, *, organization_id: int | None, source: ObjectRef, mappings: list[dict], limit: int):
    if source.document_type is not None:
        raise MappingValidationError(
            "SQL preview isn't available for a Document Intelligence mapping profile — its source is a document, "
            "not a queryable table. Use POST /documents/{document_id}/apply-mapping/{mapping_id} to run it against a real document instead."
        )
    conn = await _resolve_connection(db, source.connection_id, organization_id)
    connector = connection_service.build_connector(conn)

    def _build_and_run():
        sql = mapper.build_preview_sql(connector.quote_object, connector.quote_identifier, source.schema_name, source.object_name, mappings)
        return connector.preview_sql(sql, {}, limit)

    try:
        return await asyncio.to_thread(_build_and_run)
    except mapper.MapperValidationError as exc:
        raise MappingValidationError(str(exc)) from exc
    except ConnectorError as exc:
        raise MappingValidationError(str(exc)) from exc
