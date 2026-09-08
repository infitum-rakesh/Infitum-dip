"""
Bulk Object Migration — planner service (BM3).

Owns everything that happens BEFORE a worker ever touches data: dependency
graph resolution, pre-flight configuration validation, dry-run planning,
and — the actual "ONE PIPELINE, MANY OBJECTS" mechanism — materializing a
BulkMigrationRun's BulkMigrationTask rows from the pipeline's enabled
BulkMigrationObject rows. See bulk_migration_service.py (BM4) for what
happens AFTER tasks exist (claiming, the bounded worker pool, execution).

Dependency levels: two mechanisms, mirrored from models/bulk_migration.py's
BulkMigrationObject docstring —
  1. Fine-grained: if ANY enabled object sets `depends_on` (a list of this
     pipeline's own object_key values), the WHOLE pipeline is compiled into
     a DAG and topologically layered into "waves" — every object in wave N
     can run concurrently; wave N+1 only starts once wave N's objects are
     done. This is a direct adaptation of execution/pipeline_job_executor.
     py's `_topo_sort()` (Kahn's algorithm) — the difference is returning
     LEVELS (lists of objects with in-degree 0 at each round) instead of a
     single flat order, since ties within a level are meant to run in
     parallel, not sequentially.
  2. Coarse fallback: no object sets `depends_on` -> group by
     `dependency_order` (ascending int "phase number"); each distinct value
     is one wave.
A cycle in mechanism 1 is rejected with BulkMigrationErrorCode.
DEPENDENCY_ERROR pre-execution, never discovered mid-run (spec's explicit
"circular dependencies must be detected pre-execution" requirement) — this
is the one point where an already-existing engine's cycle-rejection
behavior (`_topo_sort` raising ValueError) is reused in spirit, adapted to
this domain's own error taxonomy.

Task materialization is deliberately NOT a Python loop calling one insert
per object with its own commit — objects are already loaded as one cheap
in-memory list (config rows are small; see bulk_migration_config_service.py
module docstring) and inserted in TASK_INSERT_BATCH_SIZE batches, matching
this codebase's "never one round trip per row" convention elsewhere
(loading/writers/base.py's write_batch_with_fallback, import_objects's own
IMPORT_BATCH_SIZE).

GLOBAL_SYSTEM_WORKER_LIMIT lives here (not in bulk_migration_service.py)
because run creation needs to resolve `effective_workers` at plan time;
BM4 imports this same constant for its own per-connection concurrency
bounding rather than duplicating the number.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import record_event
from app.models.bulk_migration import BulkMigrationObject, BulkMigrationPipeline
from app.models.bulk_migration_run import (
    BulkMigrationError, BulkMigrationErrorCode, BulkMigrationRun, BulkMigrationRunStatus,
    BulkMigrationTask, BulkMigrationTaskStatus,
)
from app.services import connection_service

# No process/thread-per-run architecture exists in this codebase (see
# execution/pipeline_executor.py's own asyncio.Semaphore-bounded pattern) —
# this is a soft, in-process ceiling on total concurrently-active migration
# tasks across ALL runs in this backend instance, mirroring how every
# other chunk-size/worker-count default in this project (PipelineRun.
# worker_count, chunk_size, SOURCE_STREAM_CHUNK_SIZE) is a plain tunable
# constant rather than something pulled from external infra config.
GLOBAL_SYSTEM_WORKER_LIMIT = 32

TASK_INSERT_BATCH_SIZE = 500
VALIDATE_CONCURRENCY = 8


def _compute_dependency_levels(objects: list[BulkMigrationObject]) -> list[list[BulkMigrationObject]]:
    if not objects:
        return []
    by_key = {o.object_key: o for o in objects}
    has_depends_on = any(o.depends_on for o in objects)

    if not has_depends_on:
        groups: dict[int, list[BulkMigrationObject]] = {}
        for o in objects:
            groups.setdefault(o.dependency_order or 0, []).append(o)
        return [groups[k] for k in sorted(groups.keys())]

    in_degree = {key: 0 for key in by_key}
    adj: dict[str, list[str]] = {key: [] for key in by_key}
    for o in objects:
        for dep_key in (o.depends_on or []):
            if dep_key not in by_key or dep_key == o.object_key:
                continue  # unresolved/self-reference — silently skipped, not fatal (dependency may be a disabled object)
            adj[dep_key].append(o.object_key)
            in_degree[o.object_key] += 1

    levels: list[list[BulkMigrationObject]] = []
    remaining = set(by_key.keys())
    current = sorted(k for k in remaining if in_degree[k] == 0)
    while current:
        levels.append([by_key[k] for k in current])
        next_level = []
        for k in current:
            remaining.discard(k)
            for nxt in adj[k]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    next_level.append(nxt)
        current = sorted(set(next_level))

    if remaining:
        raise BulkMigrationError(
            BulkMigrationErrorCode.DEPENDENCY_ERROR,
            f"Circular dependency detected among {len(remaining)} object(s): {sorted(remaining)[:10]}"
            + (" ..." if len(remaining) > 10 else ""),
        )
    return levels


async def _enabled_objects(db: AsyncSession, pipeline_id: int) -> list[BulkMigrationObject]:
    stmt = select(BulkMigrationObject).where(BulkMigrationObject.pipeline_id == pipeline_id, BulkMigrationObject.enabled.is_(True)).order_by(BulkMigrationObject.id.asc())
    return (await db.execute(stmt)).scalars().all()


# ── Pre-flight validation ────────────────────────────────────────────────

async def validate_pipeline(db: AsyncSession, pipeline: BulkMigrationPipeline) -> dict:
    """Real connectivity/object-existence/schema checks — bounded
    concurrency via asyncio.Semaphore, the same idiom every execution
    engine in this codebase uses (see bulk_migration_service.py's worker
    pool for the run-time equivalent). Since one pipeline has exactly ONE
    source and ONE target connection (shared by every object), connection
    reachability is tested ONCE, not once per object."""
    details: list[dict] = []
    errors = 0
    warnings = 0

    source_conn = await connection_service.get_connection(db, pipeline.source_connection_id, pipeline.organization_id)
    target_conn = await connection_service.get_connection(db, pipeline.target_connection_id, pipeline.organization_id)
    if not source_conn:
        return {"total": 0, "valid": 0, "warnings": 0, "errors": 1, "details": [
            {"level": "ERROR", "code": BulkMigrationErrorCode.CONNECTION_ERROR, "message": f"Source connection {pipeline.source_connection_id} not found."}
        ]}
    if not target_conn:
        return {"total": 0, "valid": 0, "warnings": 0, "errors": 1, "details": [
            {"level": "ERROR", "code": BulkMigrationErrorCode.CONNECTION_ERROR, "message": f"Target connection {pipeline.target_connection_id} not found."}
        ]}

    source_test = await connection_service.test_connection(db, source_conn)
    if not source_test.get("success"):
        details.append({"level": "ERROR", "code": BulkMigrationErrorCode.CONNECTION_ERROR, "message": f"Source connection unreachable: {source_test.get('message')}"})
        errors += 1
    target_test = await connection_service.test_connection(db, target_conn)
    if not target_test.get("success"):
        details.append({"level": "ERROR", "code": BulkMigrationErrorCode.CONNECTION_ERROR, "message": f"Target connection unreachable: {target_test.get('message')}"})
        errors += 1

    objects = await _enabled_objects(db, pipeline.id)
    total = len(objects)

    try:
        levels = _compute_dependency_levels(objects)
        dependency_levels = len(levels)
    except BulkMigrationError as exc:
        details.append({"level": "ERROR", "code": exc.code, "message": exc.message})
        errors += 1
        dependency_levels = 0

    valid_objects = 0
    if not errors:  # connections unreachable -> skip the expensive per-object checks entirely
        semaphore = asyncio.Semaphore(VALIDATE_CONCURRENCY)

        async def _check(obj: BulkMigrationObject) -> dict | None:
            async with semaphore:
                try:
                    columns, _ = await connection_service.get_columns(source_conn, obj.source_object, obj.source_schema, include_row_count=False)
                except Exception as exc:
                    return {"level": "ERROR", "object_key": obj.object_key, "code": BulkMigrationErrorCode.SOURCE_OBJECT_NOT_FOUND, "message": str(exc)}
                if not columns:
                    return {"level": "ERROR", "object_key": obj.object_key, "code": BulkMigrationErrorCode.SOURCE_OBJECT_NOT_FOUND, "message": f"Source object '{obj.source_object}' has no columns / does not exist."}
                if obj.primary_key:
                    # get_columns() returns real ColumnInfo dataclass
                    # instances (connectors/base.py), not dicts.
                    col_names = {c.name for c in columns}
                    missing_pk = [pk for pk in obj.primary_key if pk not in col_names]
                    if missing_pk:
                        return {"level": "WARNING", "object_key": obj.object_key, "code": BulkMigrationErrorCode.VALIDATION_ERROR, "message": f"Primary key column(s) not found in source: {missing_pk}"}
                return None

        results = await asyncio.gather(*[_check(o) for o in objects])
        for r in results:
            if r is None:
                valid_objects += 1
            elif r["level"] == "ERROR":
                errors += 1
                details.append(r)
            else:
                warnings += 1
                details.append(r)
    else:
        valid_objects = 0

    return {
        "total": total, "valid": valid_objects, "warnings": warnings, "errors": errors,
        "dependency_levels": dependency_levels,
        "details": details[:1000],
    }


# ── Dry run ───────────────────────────────────────────────────────────────

async def dry_run(db: AsyncSession, pipeline: BulkMigrationPipeline) -> dict:
    """Same planning logic as a real run — dependency levels, effective
    worker resolution, per-object target action preview — but writes
    NOTHING to bulk_migration_tasks or any target system. Spec's explicit
    "Dry Run must not modify target data" requirement."""
    objects = await _enabled_objects(db, pipeline.id)
    try:
        levels = _compute_dependency_levels(objects)
    except BulkMigrationError as exc:
        return {"error": {"code": exc.code, "message": exc.message}, "object_count": len(objects)}

    effective_workers = min(pipeline.max_workers, GLOBAL_SYSTEM_WORKER_LIMIT)
    estimated_rows = sum(o.estimated_row_count or 0 for o in objects)
    actions = []
    for o in objects:
        if o.load_type in ("FULL_REPLACE",):
            action = "DROP+CREATE then LOAD" if o.drop_and_recreate else "TRUNCATE then LOAD"
        elif o.load_type == "FULL_APPEND":
            action = "APPEND (not idempotent on rerun)"
        elif o.load_type == "MERGE":
            action = f"UPSERT on {o.primary_key or '(no primary key configured)'}"
        else:
            action = f"UNSUPPORTED in this release ({o.load_type}) — will fail fast with UNSUPPORTED_LOAD_TYPE if run"
        actions.append({"object_key": o.object_key, "load_type": o.load_type, "target_action": action, "estimated_rows": o.estimated_row_count})

    return {
        "object_count": len(objects),
        "dependency_levels": len(levels),
        "levels_preview": [[o.object_key for o in level] for level in levels[:20]],
        "estimated_total_rows": estimated_rows,
        "effective_workers": effective_workers,
        "actions": actions[:2000],
    }


# ── Run creation (task materialization) ─────────────────────────────────

def _generate_run_key(pipeline_id: int) -> str:
    now = datetime.now(timezone.utc)
    return f"RUN-{now:%Y%m%d-%H%M%S}-P{pipeline_id}"


async def create_run(db: AsyncSession, pipeline: BulkMigrationPipeline, *, trigger_type: str = "MANUAL", created_by: int | None = None) -> BulkMigrationRun:
    """The "ONE PIPELINE RUN + N EXECUTION TASKS" mechanism — clicking Run
    creates exactly one BulkMigrationRun row here, never one run per
    object. Raises BulkMigrationError(DEPENDENCY_ERROR) before writing
    anything if the dependency graph has a cycle."""
    objects = await _enabled_objects(db, pipeline.id)
    if not objects:
        raise BulkMigrationError(BulkMigrationErrorCode.CONFIGURATION_ERROR, "This pipeline has no enabled objects to migrate.")

    levels = _compute_dependency_levels(objects)  # raises DEPENDENCY_ERROR on a cycle — nothing persisted yet
    effective_workers = min(pipeline.max_workers, GLOBAL_SYSTEM_WORKER_LIMIT)

    run = BulkMigrationRun(
        organization_id=pipeline.organization_id, pipeline_id=pipeline.id, pipeline_version=pipeline.version,
        run_key=_generate_run_key(pipeline.id), status=BulkMigrationRunStatus.PENDING, trigger_type=trigger_type,
        effective_workers=effective_workers, effective_batch_size=pipeline.batch_size,
        continue_on_error=pipeline.continue_on_error, total_objects=len(objects), dependency_levels=len(levels),
        created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    pending_count = 0
    blocked_count = 0
    batch: list[BulkMigrationTask] = []
    for level_index, level_objects in enumerate(levels):
        initial_status = BulkMigrationTaskStatus.PENDING if level_index == 0 else BulkMigrationTaskStatus.BLOCKED
        for obj in level_objects:
            batch.append(BulkMigrationTask(
                run_id=run.id, object_id=obj.id, object_key=obj.object_key,
                source_connection_id=pipeline.source_connection_id, source_schema=obj.source_schema, source_object=obj.source_object,
                target_connection_id=pipeline.target_connection_id, target_schema=obj.target_schema, target_object=obj.target_object,
                object_type=obj.object_type, load_type=obj.load_type.value if hasattr(obj.load_type, "value") else obj.load_type,
                primary_key=obj.primary_key, filter_condition=obj.filter_condition, watermark_column=obj.watermark_column,
                batch_size=obj.batch_size_override or pipeline.batch_size, max_retries=obj.retry_count_override if obj.retry_count_override is not None else pipeline.max_retries,
                timeout_seconds=obj.timeout_seconds, truncate_before_load=obj.truncate_before_load,
                create_target_if_not_exists=obj.create_target_if_not_exists, drop_and_recreate=obj.drop_and_recreate,
                reject_policy=obj.reject_policy, dependency_group=obj.dependency_group, dependency_order=obj.dependency_order,
                depends_on=obj.depends_on, dependency_level=level_index, status=initial_status,
            ))
            if initial_status == BulkMigrationTaskStatus.PENDING:
                pending_count += 1
            else:
                blocked_count += 1
            if len(batch) >= TASK_INSERT_BATCH_SIZE:
                db.add_all(batch)
                await db.commit()
                batch = []
    if batch:
        db.add_all(batch)
        await db.commit()

    run.pending_objects = pending_count
    run.blocked_objects = blocked_count
    await db.commit()
    await db.refresh(run)

    try:
        await record_event(
            db, organization_id=pipeline.organization_id, actor_id=created_by, action="Run Created", module="Bulk Object Migration",
            resource="BulkMigrationRun", resource_id=run.run_key, details=f"{len(objects)} objects across {len(levels)} dependency level(s)",
        )
    except Exception:
        pass
    return run
