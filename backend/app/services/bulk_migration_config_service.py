"""
Bulk Object Migration — config service (BM2).

Owns BulkMigrationPipeline / BulkMigrationObject CRUD, bulk config-grid
operations (enable/disable/edit/delete selected — spec's bulk management
UI), and CSV/Excel/JSON/YAML config import/export for thousands of objects
in one upload. This is the DEFINITION side only — see
bulk_migration_planner_service.py (BM3) for dependency graph/validate/
dry-run and bulk_migration_service.py (BM4) for run execution.

Import is real, streamed, and batched: rows are parsed then inserted in
batches of IMPORT_BATCH_SIZE (never one INSERT+commit per row, and never
5,000 individual ORM objects held uncommitted at once for a huge upload).
A dry_run=True pass parses and validates every row without writing
anything — the same "Validate Configuration" preview the spec asks for
before a real import commit.

object_key is deterministic (source_schema.source_object -> target_schema.
target_object) so re-importing the same file is idempotent at the config
level too: an existing object_key gets updated in place, not duplicated
(the DB UniqueConstraint on (pipeline_id, object_key) is the actual
enforcement — see models/bulk_migration.py).

Audit trail reuses the existing generic AuditLog.record_event()
(models/audit_log.py) rather than a bespoke table — see "Bulk Object
Migration" in AUDIT_MODULES there.
"""
from __future__ import annotations

import csv
import io
import json

from sqlalchemy import delete as sa_delete, func, or_, select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditStatus, record_event
from app.models.bulk_migration import BulkMigrationLoadType, BulkMigrationObject, BulkMigrationPipeline
from app.models.bulk_migration_run import BulkMigrationRun, BulkMigrationTask, BulkMigrationTaskEvent

IMPORT_BATCH_SIZE = 500
ALLOWED_IMPORT_EXTENSIONS = {"csv", "tsv", "txt", "xlsx", "xls", "json", "yaml", "yml"}

# Column/field name -> BulkMigrationObject attribute. Accepts a few common
# aliases per field so a hand-edited spreadsheet exported from another tool
# doesn't need exact header renaming.
_FIELD_ALIASES = {
    "source_schema": "source_schema",
    "source_sch": "source_schema",
    "src_schema": "source_schema",
    "source_object": "source_object",
    "source_table": "source_object",
    "src_object": "source_object",
    "src_table": "source_object",
    "target_schema": "target_schema",
    "target_sch": "target_schema",
    "tgt_schema": "target_schema",
    "target_object": "target_object",
    "target_table": "target_object",
    "tgt_object": "target_object",
    "tgt_table": "target_object",
    "object_type": "object_type",
    "load_type": "load_type",
    "primary_key": "primary_key",
    "filter_condition": "filter_condition",
    "watermark_column": "watermark_column",
    "priority": "priority",
    "dependency_group": "dependency_group",
    "dependency_order": "dependency_order",
    "depends_on": "depends_on",
    "enabled": "enabled",
    "parallel_allowed": "parallel_allowed",
    "batch_size": "batch_size_override",
    "batch_size_override": "batch_size_override",
    "timeout_seconds": "timeout_seconds",
    "max_workers": "max_workers_override",
    "max_workers_override": "max_workers_override",
    "retry_count": "retry_count_override",
    "retry_count_override": "retry_count_override",
    "truncate_before_load": "truncate_before_load",
    "create_target_if_not_exists": "create_target_if_not_exists",
    "drop_and_recreate": "drop_and_recreate",
    "reject_policy": "reject_policy",
}
_LIST_FIELDS = {"primary_key", "depends_on"}
_BOOL_FIELDS = {"enabled", "parallel_allowed", "truncate_before_load", "create_target_if_not_exists", "drop_and_recreate"}
_INT_FIELDS = {"priority", "dependency_order", "batch_size_override", "timeout_seconds", "max_workers_override", "retry_count_override"}


class BulkMigrationConfigError(Exception):
    pass


def _qualify(schema: str | None, obj: str) -> str:
    """'schema.object' when a real schema is given, else the bare object
    name — never a leading '.'. Salesforce/NetSuite (and any other
    schema-less source) leave source_schema/target_schema unset, so
    build_object_key() used to always splice in the '.' separator
    regardless, producing keys like '.Account->.Account' instead of
    'Account->Account'. Found live via a real SFDC/NetSuite pipeline."""
    schema = (schema or "").strip()
    return f"{schema}.{obj.strip()}" if schema else obj.strip()


def build_object_key(source_schema: str | None, source_object: str, target_schema: str | None, target_object: str) -> str:
    return f"{_qualify(source_schema, source_object)}->{_qualify(target_schema, target_object)}"


async def _audit(db: AsyncSession, *, organization_id, actor_id, action, resource_id=None, status=AuditStatus.SUCCESS, details=None):
    try:
        await record_event(
            db, organization_id=organization_id, actor_id=actor_id, action=action, module="Bulk Object Migration",
            resource="BulkMigrationPipeline", resource_id=str(resource_id) if resource_id is not None else None,
            status=status, details=details,
        )
    except Exception:
        pass  # audit is best-effort, never blocks the real operation


async def _bump_version(db: AsyncSession, pipeline_id: int) -> None:
    """Atomic SQL-side increment (version = version + 1), not a Python
    attribute mutation — deliberately does not depend on the in-memory
    pipeline object's current attribute value being fresh. A prior
    operation in the same session/request (e.g. create_object's duplicate-
    key rollback() below) can leave that object expired; touching an
    expired attribute outside an awaited context raises MissingGreenlet —
    the same failure class already root-caused and fixed once in
    services/schedule_service.py this session. Going straight to SQL sidesteps
    the whole bug class rather than just working around one call site."""
    await db.execute(sa_update(BulkMigrationPipeline).where(BulkMigrationPipeline.id == pipeline_id).values(version=BulkMigrationPipeline.version + 1))


# ── Pipelines ────────────────────────────────────────────────────────────

async def list_pipelines(db: AsyncSession, organization_id: int | None, *, search: str | None = None,
                          status: str | None = None, page: int = 1, page_size: int = 50) -> dict:
    stmt = select(BulkMigrationPipeline)
    if organization_id is not None:
        stmt = stmt.where(BulkMigrationPipeline.organization_id == organization_id)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(BulkMigrationPipeline.name.ilike(like), BulkMigrationPipeline.description.ilike(like)))
    if status:
        stmt = stmt.where(BulkMigrationPipeline.status == status)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(BulkMigrationPipeline.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(stmt)).scalars().all()
    return {"items": items, "total": total, "page": page, "page_size": page_size, "total_pages": max(1, (total + page_size - 1) // page_size)}


async def get_pipeline(db: AsyncSession, pipeline_id: int, organization_id: int | None) -> BulkMigrationPipeline | None:
    stmt = select(BulkMigrationPipeline).where(BulkMigrationPipeline.id == pipeline_id)
    if organization_id is not None:
        stmt = stmt.where(BulkMigrationPipeline.organization_id == organization_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_pipeline(db: AsyncSession, *, organization_id: int | None, created_by: int | None, name: str,
                           description: str | None = None, source_connection_id: int, target_connection_id: int,
                           max_workers: int = 8, batch_size: int = 5000, max_retries: int = 3,
                           retry_backoff_seconds: float = 10.0, continue_on_error: bool = True,
                           max_concurrent_runs: int = 1) -> BulkMigrationPipeline:
    if not name or not name.strip():
        raise BulkMigrationConfigError("Pipeline name is required.")
    if not source_connection_id or not target_connection_id:
        raise BulkMigrationConfigError("Both a source and a target connection are required.")

    pipeline = BulkMigrationPipeline(
        organization_id=organization_id, created_by=created_by, updated_by=created_by,
        name=name.strip(), description=description, source_connection_id=source_connection_id,
        target_connection_id=target_connection_id, max_workers=max(1, max_workers), batch_size=max(1, batch_size),
        max_retries=max(0, max_retries), retry_backoff_seconds=max(0.0, retry_backoff_seconds),
        continue_on_error=continue_on_error, max_concurrent_runs=max(1, max_concurrent_runs),
    )
    db.add(pipeline)
    await db.commit()
    await db.refresh(pipeline)
    await _audit(db, organization_id=organization_id, actor_id=created_by, action="Pipeline Created", resource_id=pipeline.id)
    return pipeline


async def update_pipeline(db: AsyncSession, pipeline: BulkMigrationPipeline, *, updated_by: int | None, **fields) -> BulkMigrationPipeline:
    changed = []
    for key, value in fields.items():
        if value is None or not hasattr(pipeline, key):
            continue
        if getattr(pipeline, key) != value:
            setattr(pipeline, key, value)
            changed.append(key)
    if changed:
        pipeline.updated_by = updated_by
        # Execution-defaults changes affect what a future run inherits —
        # bump version so runs can be traced to "which generation of config".
        if any(k in changed for k in ("max_workers", "batch_size", "max_retries", "retry_backoff_seconds", "continue_on_error", "source_connection_id", "target_connection_id")):
            await _bump_version(db, pipeline.id)
        await db.commit()
        await db.refresh(pipeline)
        await _audit(db, organization_id=pipeline.organization_id, actor_id=updated_by, action="Pipeline Updated",
                     resource_id=pipeline.id, details=f"Changed: {', '.join(changed)}")
    return pipeline


async def set_pipeline_status(db: AsyncSession, pipeline: BulkMigrationPipeline, status: str, *, updated_by: int | None) -> BulkMigrationPipeline:
    if status not in ("ACTIVE", "DISABLED", "ARCHIVED"):
        raise BulkMigrationConfigError(f"Unknown pipeline status '{status}'.")
    pipeline.status = status
    pipeline.updated_by = updated_by
    await db.commit()
    await db.refresh(pipeline)
    await _audit(db, organization_id=pipeline.organization_id, actor_id=updated_by, action=f"Pipeline {status.title()}", resource_id=pipeline.id)
    return pipeline


async def delete_pipeline(db: AsyncSession, pipeline: BulkMigrationPipeline, *, actor_id: int | None) -> None:
    """No ORM relationship()/cascade is declared anywhere in this domain
    (matching this codebase's own no-cascade convention — see module
    docstring), so a plain db.delete(pipeline) would silently ORPHAN every
    BulkMigrationObject/Run/Task/TaskEvent row that pointed at it. Explicit
    child-first bulk deletes here are the real cleanup; sqlite_autoincrement
    on all 4 tables (models/bulk_migration.py, models/bulk_migration_run.py)
    is the belt-and-suspenders backstop against a FUTURE orphan (if this
    function is ever bypassed) colliding with a new pipeline that reuses a
    freed id — the exact bug this combination was written to fix, found
    live: a second test pipeline reused a deleted pipeline's id and picked
    up its orphaned objects as its own."""
    pipeline_id, organization_id = pipeline.id, pipeline.organization_id
    run_ids = (await db.execute(select(BulkMigrationRun.id).where(BulkMigrationRun.pipeline_id == pipeline_id))).scalars().all()
    if run_ids:
        await db.execute(sa_delete(BulkMigrationTaskEvent).where(BulkMigrationTaskEvent.run_id.in_(run_ids)))
        await db.execute(sa_delete(BulkMigrationTask).where(BulkMigrationTask.run_id.in_(run_ids)))
        await db.execute(sa_delete(BulkMigrationRun).where(BulkMigrationRun.id.in_(run_ids)))
    await db.execute(sa_delete(BulkMigrationObject).where(BulkMigrationObject.pipeline_id == pipeline_id))
    await db.delete(pipeline)
    await db.commit()
    await _audit(db, organization_id=organization_id, actor_id=actor_id, action="Pipeline Deleted", resource_id=pipeline_id)


# ── Objects ──────────────────────────────────────────────────────────────

async def list_objects(db: AsyncSession, pipeline_id: int, *, search: str | None = None, enabled: bool | None = None,
                        object_type: str | None = None, load_type: str | None = None, dependency_group: str | None = None,
                        page: int = 1, page_size: int = 100) -> dict:
    stmt = select(BulkMigrationObject).where(BulkMigrationObject.pipeline_id == pipeline_id)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(
            BulkMigrationObject.source_object.ilike(like), BulkMigrationObject.target_object.ilike(like),
            BulkMigrationObject.object_key.ilike(like),
        ))
    if enabled is not None:
        stmt = stmt.where(BulkMigrationObject.enabled == enabled)
    if object_type:
        stmt = stmt.where(BulkMigrationObject.object_type == object_type)
    if load_type:
        stmt = stmt.where(BulkMigrationObject.load_type == load_type)
    if dependency_group:
        stmt = stmt.where(BulkMigrationObject.dependency_group == dependency_group)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(BulkMigrationObject.id.asc()).offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(stmt)).scalars().all()
    return {"items": items, "total": total, "page": page, "page_size": page_size, "total_pages": max(1, (total + page_size - 1) // page_size)}


async def get_object(db: AsyncSession, object_id: int, pipeline_id: int) -> BulkMigrationObject | None:
    stmt = select(BulkMigrationObject).where(BulkMigrationObject.id == object_id, BulkMigrationObject.pipeline_id == pipeline_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_object(db: AsyncSession, pipeline: BulkMigrationPipeline, *, actor_id: int | None, **fields) -> BulkMigrationObject:
    source_object = (fields.get("source_object") or "").strip()
    target_object = (fields.get("target_object") or "").strip()
    if not source_object or not target_object:
        raise BulkMigrationConfigError("source_object and target_object are required.")
    source_schema = fields.get("source_schema")
    target_schema = fields.get("target_schema")
    object_key = build_object_key(source_schema, source_object, target_schema, target_object)

    obj = BulkMigrationObject(
        pipeline_id=pipeline.id, object_key=object_key, source_schema=source_schema, source_object=source_object,
        target_schema=target_schema, target_object=target_object, object_type=fields.get("object_type") or "TABLE",
        load_type=fields.get("load_type") or BulkMigrationLoadType.FULL_REPLACE,
        primary_key=fields.get("primary_key"), filter_condition=fields.get("filter_condition"),
        watermark_column=fields.get("watermark_column"), priority=fields.get("priority", 100),
        dependency_group=fields.get("dependency_group"), dependency_order=fields.get("dependency_order", 0),
        depends_on=fields.get("depends_on"), enabled=fields.get("enabled", True),
        parallel_allowed=fields.get("parallel_allowed", True), batch_size_override=fields.get("batch_size_override"),
        timeout_seconds=fields.get("timeout_seconds"), max_workers_override=fields.get("max_workers_override"),
        retry_count_override=fields.get("retry_count_override"),
        truncate_before_load=fields.get("truncate_before_load", True),
        create_target_if_not_exists=fields.get("create_target_if_not_exists", True),
        drop_and_recreate=fields.get("drop_and_recreate", False), reject_policy=fields.get("reject_policy") or "LOG",
    )
    db.add(obj)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # rollback() expires EVERY object in the session, not just `obj` —
        # refreshing `pipeline` in place here heals the SAME object the
        # caller is holding (Python passes it by reference), so a caller
        # that keeps using `pipeline` after catching this error doesn't
        # later crash with MissingGreenlet touching a now-expired attribute
        # outside an awaited context. Same failure class already root-
        # caused once this session in services/schedule_service.py.
        await db.refresh(pipeline)
        raise BulkMigrationConfigError(f"An object with key '{object_key}' already exists in this pipeline.") from exc
    await db.refresh(obj)
    await _bump_version(db, pipeline.id)
    await db.commit()
    await _audit(db, organization_id=pipeline.organization_id, actor_id=actor_id, action="Object Added",
                 resource_id=pipeline.id, details=object_key)
    return obj


async def update_object(db: AsyncSession, pipeline: BulkMigrationPipeline, obj: BulkMigrationObject, *, actor_id: int | None, **fields) -> BulkMigrationObject:
    changed = []
    for key, value in fields.items():
        if key not in _FIELD_ALIASES.values() and not hasattr(obj, key):
            continue
        if value is not None and getattr(obj, key, None) != value:
            setattr(obj, key, value)
            changed.append(key)
    if not changed:
        return obj

    # Identity fields changed -> recompute object_key.
    if any(k in changed for k in ("source_schema", "source_object", "target_schema", "target_object")):
        obj.object_key = build_object_key(obj.source_schema, obj.source_object, obj.target_schema, obj.target_object)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # See create_object()'s identical comment — heal the caller's
        # `pipeline` reference in place after the rollback expires it.
        await db.refresh(pipeline)
        raise BulkMigrationConfigError(f"An object with key '{obj.object_key}' already exists in this pipeline.") from exc
    await db.refresh(obj)
    await _bump_version(db, pipeline.id)
    await db.commit()
    await _audit(db, organization_id=pipeline.organization_id, actor_id=actor_id, action="Object Updated",
                 resource_id=pipeline.id, details=obj.object_key)
    return obj


async def delete_object(db: AsyncSession, pipeline: BulkMigrationPipeline, obj: BulkMigrationObject, *, actor_id: int | None) -> None:
    object_key = obj.object_key
    await db.delete(obj)
    await _bump_version(db, pipeline.id)
    await db.commit()
    await _audit(db, organization_id=pipeline.organization_id, actor_id=actor_id, action="Object Removed",
                 resource_id=pipeline.id, details=object_key)


async def bulk_update_objects(db: AsyncSession, pipeline: BulkMigrationPipeline, object_ids: list[int], patch: dict, *, actor_id: int | None) -> int:
    """Enable/disable/change load type/target schema/batch size/priority/etc.
    for the selected rows in one round trip — spec's bulk config grid ops."""
    if not object_ids:
        return 0
    # target_connection_id lives on the pipeline, not the object — not
    # patchable here.
    allowed = {
        "enabled", "load_type", "target_schema", "batch_size_override", "priority",
        "dependency_group", "dependency_order", "reject_policy", "parallel_allowed", "truncate_before_load",
    }
    safe_patch = {k: v for k, v in patch.items() if k in allowed}
    if not safe_patch:
        return 0
    stmt = (
        sa_update(BulkMigrationObject)
        .where(BulkMigrationObject.pipeline_id == pipeline.id, BulkMigrationObject.id.in_(object_ids))
        .values(**safe_patch)
    )
    result = await db.execute(stmt)
    await _bump_version(db, pipeline.id)
    await db.commit()
    await _audit(db, organization_id=pipeline.organization_id, actor_id=actor_id, action="Bulk Objects Updated",
                 resource_id=pipeline.id, details=f"{result.rowcount} objects: {list(safe_patch.keys())}")
    return result.rowcount or 0


async def bulk_delete_objects(db: AsyncSession, pipeline: BulkMigrationPipeline, object_ids: list[int], *, actor_id: int | None) -> int:
    if not object_ids:
        return 0
    stmt = sa_delete(BulkMigrationObject).where(BulkMigrationObject.pipeline_id == pipeline.id, BulkMigrationObject.id.in_(object_ids))
    result = await db.execute(stmt)
    await _bump_version(db, pipeline.id)
    await db.commit()
    await _audit(db, organization_id=pipeline.organization_id, actor_id=actor_id, action="Bulk Objects Deleted",
                 resource_id=pipeline.id, details=f"{result.rowcount} objects")
    return result.rowcount or 0


# ── Import / Export ──────────────────────────────────────────────────────

def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _coerce_value(field: str, raw) -> object:
    if raw is None or raw == "":
        return None
    if field in _BOOL_FIELDS:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "y")
    if field in _INT_FIELDS:
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"'{raw}' is not a valid integer for {field}")
    if field in _LIST_FIELDS:
        if isinstance(raw, list):
            return raw
        return [p.strip() for p in str(raw).replace("|", ",").split(",") if p.strip()]
    return str(raw).strip()


def _row_to_fields(row: dict, row_index: int) -> tuple[dict | None, str | None]:
    normalized = {}
    for raw_key, raw_val in row.items():
        if raw_key is None:
            continue
        key = str(raw_key).strip().lower().replace(" ", "_")
        field = _FIELD_ALIASES.get(key)
        if not field:
            continue
        try:
            value = _coerce_value(field, raw_val)
        except ValueError as exc:
            return None, f"Row {row_index}: {exc}"
        if value is not None:
            normalized[field] = value

    if not normalized.get("source_object"):
        return None, f"Row {row_index}: missing source_object"
    if not normalized.get("target_object"):
        return None, f"Row {row_index}: missing target_object"

    load_type = normalized.get("load_type")
    if load_type and load_type.upper() not in BulkMigrationLoadType.__members__:
        return None, f"Row {row_index}: unknown load_type '{load_type}'"
    if load_type:
        normalized["load_type"] = load_type.upper()

    normalized["object_key"] = build_object_key(
        normalized.get("source_schema"), normalized["source_object"],
        normalized.get("target_schema"), normalized["target_object"],
    )
    return normalized, None


def _parse_delimited(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="ignore")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return list(reader)


def _parse_excel(content: bytes) -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
        except StopIteration:
            return []
        out = []
        for row in rows_iter:
            if row is None or all(v is None for v in row):
                continue
            out.append(dict(zip(header, row)))
        return out
    finally:
        wb.close()


def _parse_json(content: bytes) -> list[dict]:
    data = json.loads(content.decode("utf-8"))
    if isinstance(data, dict):
        data = data.get("objects", [])
    if not isinstance(data, list):
        raise BulkMigrationConfigError("JSON import must be a list of objects, or {\"objects\": [...]}.")
    return data


def _parse_yaml(content: bytes) -> list[dict]:
    import yaml
    data = yaml.safe_load(content.decode("utf-8"))
    if isinstance(data, dict):
        data = data.get("objects", [])
    if not isinstance(data, list):
        raise BulkMigrationConfigError("YAML import must be a list of objects, or an 'objects:' key holding one.")
    return data


def parse_import_rows(content: bytes, filename: str) -> list[dict]:
    ext = _ext(filename)
    if ext not in ALLOWED_IMPORT_EXTENSIONS:
        raise BulkMigrationConfigError(f"Unsupported import file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_IMPORT_EXTENSIONS))}.")
    if ext in ("csv", "tsv", "txt"):
        return _parse_delimited(content)
    if ext in ("xlsx", "xls"):
        return _parse_excel(content)
    if ext == "json":
        return _parse_json(content)
    return _parse_yaml(content)  # yaml | yml


def _unmapped_headers(raw_rows: list[dict]) -> list[str]:
    """Any column header the file used that _FIELD_ALIASES doesn't
    recognize — these get silently dropped from every row rather than
    raising a hard per-row error (a stray custom column shouldn't block
    5,000 otherwise-valid rows), but that used to mean a genuine typo
    (e.g. 'Source_sch' instead of 'source_schema') failed the exact same
    silent way, with zero feedback that the whole column had been ignored.
    Found live against a real import file. Computed once from the header
    set, not per-row, and excludes blank header cells (a trailing
    delimiter in a hand-edited .txt/.csv produces one)."""
    seen = set()
    for row in raw_rows:
        for raw_key in row.keys():
            if raw_key is None:
                continue
            key = str(raw_key).strip().lower().replace(" ", "_")
            if key:
                seen.add(key)
    return sorted(h for h in seen if h not in _FIELD_ALIASES)


async def import_objects(db: AsyncSession, pipeline: BulkMigrationPipeline, content: bytes, filename: str, *,
                          dry_run: bool, actor_id: int | None) -> dict:
    """Parses the whole file (config rows are lightweight even at 50k — see
    module docstring), validates every row, and — unless dry_run — commits
    valid rows in IMPORT_BATCH_SIZE batches with upsert-by-object_key
    semantics. Returns {total, valid, invalid, imported, updated, errors,
    unmapped_columns}.
    """
    raw_rows = parse_import_rows(content, filename)
    unmapped_columns = _unmapped_headers(raw_rows)
    valid_fields: list[dict] = []
    errors: list[str] = []
    seen_keys: set[str] = set()

    for i, row in enumerate(raw_rows, start=2):  # header is row 1
        fields, error = _row_to_fields(row, i)
        if error:
            errors.append(error)
            continue
        if fields["object_key"] in seen_keys:
            errors.append(f"Row {i}: duplicate object_key '{fields['object_key']}' within this file")
            continue
        seen_keys.add(fields["object_key"])
        valid_fields.append(fields)

    result = {"total": len(raw_rows), "valid": len(valid_fields), "invalid": len(errors), "imported": 0, "updated": 0, "errors": errors[:500], "unmapped_columns": unmapped_columns}
    if dry_run or not valid_fields:
        return result

    existing_rows = (await db.execute(
        select(BulkMigrationObject.id, BulkMigrationObject.object_key).where(BulkMigrationObject.pipeline_id == pipeline.id)
    )).all()
    existing_by_key = {key: oid for oid, key in existing_rows}

    for batch_start in range(0, len(valid_fields), IMPORT_BATCH_SIZE):
        batch = valid_fields[batch_start:batch_start + IMPORT_BATCH_SIZE]
        for fields in batch:
            object_key = fields["object_key"]
            existing_id = existing_by_key.get(object_key)
            if existing_id:
                await db.execute(sa_update(BulkMigrationObject).where(BulkMigrationObject.id == existing_id).values(**{k: v for k, v in fields.items() if k != "object_key"}))
                result["updated"] += 1
            else:
                db.add(BulkMigrationObject(pipeline_id=pipeline.id, **fields))
                result["imported"] += 1
        await db.commit()

    await _bump_version(db, pipeline.id)
    await db.commit()
    await _audit(db, organization_id=pipeline.organization_id, actor_id=actor_id, action="Config Imported",
                 resource_id=pipeline.id, details=f"{result['imported']} added, {result['updated']} updated, {result['invalid']} invalid")
    return result


EXPORT_FIELDS = [
    "object_key", "source_schema", "source_object", "target_schema", "target_object", "object_type", "load_type",
    "primary_key", "filter_condition", "watermark_column", "priority", "dependency_group", "dependency_order",
    "depends_on", "enabled", "parallel_allowed", "batch_size_override", "timeout_seconds", "max_workers_override",
    "retry_count_override", "truncate_before_load", "create_target_if_not_exists", "drop_and_recreate", "reject_policy",
]


def _export_value(v):
    """load_type is a real Python enum on the ORM object (str, enum.Enum) —
    str(v) would serialize it as 'BulkMigrationLoadType.FULL_REPLACE'
    instead of the plain 'FULL_REPLACE' every import/re-import round trip
    expects, and PyYAML's SafeDumper flatly refuses to represent an Enum
    member at all. Unwrap to .value explicitly rather than relying on
    json.dumps(default=str) to paper over it."""
    import enum
    return v.value if isinstance(v, enum.Enum) else v


async def export_objects(db: AsyncSession, pipeline_id: int, fmt: str = "csv") -> tuple[bytes, str, str]:
    """Small, in-memory export — config rows are lightweight even at 50k+
    (unlike migrated ROW DATA, which the execution engine streams/chunks;
    see module docstring). Returns (content_bytes, content_type, filename)."""
    objects = (await db.execute(
        select(BulkMigrationObject).where(BulkMigrationObject.pipeline_id == pipeline_id).order_by(BulkMigrationObject.id.asc())
    )).scalars().all()
    rows = [{f: _export_value(getattr(o, f)) for f in EXPORT_FIELDS} for o in objects]

    if fmt == "json":
        return json.dumps({"objects": rows}, default=str, indent=2).encode("utf-8"), "application/json", "bulk_migration_objects.json"
    if fmt in ("yaml", "yml"):
        import yaml
        return yaml.safe_dump({"objects": rows}, sort_keys=False).encode("utf-8"), "application/x-yaml", "bulk_migration_objects.yaml"
    if fmt in ("xlsx", "excel"):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(EXPORT_FIELDS)
        for row in rows:
            ws.append([",".join(v) if isinstance(v, list) else v for v in row.values()])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "bulk_migration_objects.xlsx"

    # default: csv
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (",".join(v) if isinstance(v, list) else v) for k, v in row.items()})
    return buf.getvalue().encode("utf-8"), "text/csv", "bulk_migration_objects.csv"


# ── API response shaping (BM6) ───────────────────────────────────────────
# Plain converter functions, not ORM auto-serialization — matches this
# codebase's established to_out() convention (connection_service.to_out(),
# schedule_service.to_out()) so the router stays a thin translation layer.

async def to_pipeline_out(db: AsyncSession, pipeline: BulkMigrationPipeline) -> dict:
    object_count = (await db.execute(select(func.count()).select_from(BulkMigrationObject).where(BulkMigrationObject.pipeline_id == pipeline.id))).scalar_one()
    return {
        "id": pipeline.id, "name": pipeline.name, "description": pipeline.description,
        "source_connection_id": pipeline.source_connection_id, "target_connection_id": pipeline.target_connection_id,
        "max_workers": pipeline.max_workers, "batch_size": pipeline.batch_size, "max_retries": pipeline.max_retries,
        "retry_backoff_seconds": pipeline.retry_backoff_seconds, "continue_on_error": pipeline.continue_on_error,
        "max_concurrent_runs": pipeline.max_concurrent_runs, "status": pipeline.status, "version": pipeline.version,
        "object_count": object_count, "created_at": pipeline.created_at, "updated_at": pipeline.updated_at,
    }


def to_object_out(obj: BulkMigrationObject) -> dict:
    return {
        "id": obj.id, "pipeline_id": obj.pipeline_id, "object_key": obj.object_key,
        "source_schema": obj.source_schema, "source_object": obj.source_object,
        "target_schema": obj.target_schema, "target_object": obj.target_object,
        "object_type": obj.object_type, "load_type": obj.load_type.value if hasattr(obj.load_type, "value") else obj.load_type,
        "primary_key": obj.primary_key, "filter_condition": obj.filter_condition, "watermark_column": obj.watermark_column,
        "priority": obj.priority, "dependency_group": obj.dependency_group, "dependency_order": obj.dependency_order,
        "depends_on": obj.depends_on, "enabled": obj.enabled, "parallel_allowed": obj.parallel_allowed,
        "batch_size_override": obj.batch_size_override, "timeout_seconds": obj.timeout_seconds,
        "max_workers_override": obj.max_workers_override, "retry_count_override": obj.retry_count_override,
        "truncate_before_load": obj.truncate_before_load, "create_target_if_not_exists": obj.create_target_if_not_exists,
        "drop_and_recreate": obj.drop_and_recreate, "reject_policy": obj.reject_policy,
        "estimated_row_count": obj.estimated_row_count, "last_run_duration_seconds": obj.last_run_duration_seconds,
        "last_run_row_count": obj.last_run_row_count,
    }
