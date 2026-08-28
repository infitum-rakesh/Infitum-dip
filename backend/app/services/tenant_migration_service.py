"""
tenant_migration_service — Phase 4 of Bring-Your-Own-Database: the real
migration/cutover engine that moves one organization's TenantBase data
from wherever it lives today (still the shared platform database, for
every org until this runs) onto the external database they configured
and Test-Connected in Phase 1 (services/tenant_database_service.py).

State machine (TenantMigrationJob.status — models/tenant_migration_job.py):
  PENDING -> PROVISIONING_SCHEMA -> COPYING -> VERIFYING -> CUTTING_OVER
    -> COMPLETED
                                                           -> FAILED (any step)

Fire-and-forget via asyncio.create_task, same pattern as every other
background executor in this codebase (execution/pipeline_job_executor.py
etc.) — start_migration() returns the PENDING job immediately; the
frontend polls GET .../migrations/{id} and .../migrations/{id}/logs for
live progress, same as Pipeline Jobs.

Table-by-table org scoping is resolved GENERICALLY via SQLAlchemy schema
introspection rather than a hand-maintained table list: every TenantBase
table either has its own `organization_id` column, or (for the ~11
child tables that don't — PipelineVersion, PipelineJobLog, etc.) has
exactly one foreign key chain that leads back to a table that does. See
_resolve_org_scope() below. This is more robust than a hardcoded list —
it can't silently go stale if a future table follows the same shape.

Copy is CHUNKED by primary key (id > :after ORDER BY id LIMIT :chunk,
never OFFSET — same pagination discipline execution/worker.py's
key-range extraction already uses) and processed in
TenantBase.metadata.sorted_tables order (parents before children — the
same topological order SQLAlchemy already computes from the real FK
graph, so target-side FK constraints are always satisfied on insert).

Only PostgreSQL and MySQL targets are supported — same restriction
database/tenant_engine_registry.py already discloses for live routing
(SQL_SERVER has no real async driver wired up); start_migration()
refuses a SQL_SERVER TenantDatabase row up front rather than failing
confusingly mid-run.

Cutover pause: CUTTING_OVER briefly gates writes for this org via
tenant_engine_registry.mark_migrating() (checked by
database/tenant_routing.get_tenant_db() — 503s a non-GET request) while
a bounded delta-copy captures anything written during the COPYING/
VERIFYING window (which was NOT gated — the org's users keep working
normally right up until this point). Only once the delta lands and
counts reconcile does is_active flip True and the gate lift.

Audit trail: for an org-level migration (organization_id is a real int),
written through the existing AuditLog (TenantBase, module="Settings") —
deliberately via the SOURCE session, before the is_active flip, exactly
once per the plan: "an org's audit history naturally splits at the
migration date, which is accurate, not a bug."

Phase 6 — platform-level migration (organization_id is None): this is
NOT "migrate the NULL-org's own rows" (there generally are none) — it's
"move the entire remaining shared platform database to a new one, and
make that new database the default fallback for every org that has no
individual override" (see database/tenant_engine_registry.py's cascade
in get_sessionmaker()). Three things differ from the org-level path:
  - Copy/verify EXCLUDE any org that already has its own individually-
    active TenantDatabase — see _excluded_org_ids(). Phase 4's copy step
    never deletes source rows, so an already-migrated org's rows are
    still physically sitting in the shared database; copying them here
    would silently resurrect data that org already moved away from.
  - The audit event goes through SellerAuditLog/record_seller_event
    (PlatformBase, already platform-only) instead of AuditLog — there's
    no single "organization" this event belongs to.
  - Cutover calls tenant_engine_registry.evict_all() instead of
    evict(organization_id) — every org without its own override may
    already have cached the (now-stale) fallback sessionmaker under its
    OWN cache key, not under the None key a plain evict() would clear.

Rollback: a FAILED job before CUTTING_OVER touched nothing live — the
org was never paused, target DB is left in place (not auto-dropped) for
inspection, and calling start_migration() again is a real retry (the
COPYING step unconditionally clears the target's tables first, so a
partial previous attempt's rows are safely discarded, never duplicated
or double-counted). A failure DURING CUTTING_OVER reverts the pause
immediately (see except-block) — the org resumes on the source having
paused only as long as that one step actually ran. Rollback of a
COMPLETED cutover is a documented manual runbook, not automated tooling,
per the plan.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import scrub_secrets
from app.core import crypto
from app.database import tenant_engine_registry
from app.database.connection import AsyncSessionLocal, TenantBase
from app.models.audit_log import AuditStatus, record_event
from app.models.seller_audit_log import record_seller_event
from app.models.tenant_database import TenantDatabase, TenantDatabaseStatus
from app.models.tenant_migration_job import TenantMigrationJob, TenantMigrationJobLog, TenantMigrationStatus
from app.services import tenant_database_service

CHUNK_SIZE = 2000
_ACTIVE_STATUSES = {
    TenantMigrationStatus.PENDING, TenantMigrationStatus.PROVISIONING_SCHEMA,
    TenantMigrationStatus.COPYING, TenantMigrationStatus.VERIFYING, TenantMigrationStatus.CUTTING_OVER,
}


class TenantMigrationValidationError(Exception):
    """Raised for a bad start-migration request — no configured/tested
    database, already active, unsupported dialect, or one already running."""


# ── Kickoff ──────────────────────────────────────────────────────

async def start_migration(db: AsyncSession, *, organization_id: int | None, actor_id: int, ip_address: str | None = None) -> TenantMigrationJob:
    row = await tenant_database_service.get_row(db, organization_id)
    if row is None:
        raise TenantMigrationValidationError("No backend database is configured for this organization yet.")
    if row.is_active:
        raise TenantMigrationValidationError("This organization is already running on its configured backend database.")
    if not row.last_test_success:
        raise TenantMigrationValidationError("Test Connection must succeed before a migration can start.")
    if row.connection_type not in ("POSTGRESQL", "MYSQL"):
        raise TenantMigrationValidationError(
            f"Live migration to '{row.connection_type}' isn't supported yet — only PostgreSQL and MySQL have a "
            f"real async driver wired up (see database/tenant_engine_registry.py)."
        )

    existing = await db.execute(
        select(TenantMigrationJob.id).where(
            TenantMigrationJob.tenant_database_id == row.id,
            TenantMigrationJob.status.in_(_ACTIVE_STATUSES),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise TenantMigrationValidationError("A migration is already in progress for this organization.")

    job = TenantMigrationJob(
        organization_id=organization_id, tenant_database_id=row.id, status=TenantMigrationStatus.PENDING,
        tables_total=len(TenantBase.metadata.sorted_tables), created_by=actor_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    asyncio.create_task(_run(job.id, organization_id, row.id, actor_id, ip_address))
    return job


# ── Org-scope resolution (generic — see module docstring) ──────────

def _resolve_org_scope(table, memo: dict):
    """Returns (organization_id_column, selectable) for `table`, or None
    if it can't be scoped at all (never expected in this schema — see
    module docstring; treated as a hard error if it ever happens, rather
    than silently copying every org's rows)."""
    if table.name in memo:
        return memo[table.name]
    result = None
    if "organization_id" in table.columns:
        result = (table.c.organization_id, table)
    else:
        for fk in table.foreign_keys:
            parent_result = _resolve_org_scope(fk.column.table, memo)
            if parent_result is not None:
                parent_col, parent_selectable = parent_result
                result = (parent_col, table.join(parent_selectable, fk.parent == fk.column))
                break
    memo[table.name] = result
    return result


def _org_filter(stmt, org_col, organization_id: int | None, excluded_org_ids: frozenset[int] = frozenset()):
    """Org-level migration (organization_id is a real int): plain
    equality, unchanged from Phase 4. Platform-level migration
    (organization_id is None, Phase 6): NOT "org_col IS NULL" — that
    would only catch the rare row explicitly created with no org at all
    and miss every real org's data, which is exactly what a platform
    migration needs to move. Instead it's every row EXCEPT the ones
    belonging to an org in excluded_org_ids (already individually
    migrated — see _excluded_org_ids()). NULL-org rows are kept
    explicitly: SQL's `col NOT IN (...)` is UNKNOWN, not TRUE, for a NULL
    col, so a plain `.not_in()` would silently drop them."""
    if org_col is None:
        return stmt
    if organization_id is not None:
        return stmt.where(org_col == organization_id)
    if not excluded_org_ids:
        return stmt
    return stmt.where(or_(org_col.is_(None), org_col.not_in(excluded_org_ids)))


async def _excluded_org_ids() -> frozenset[int]:
    """Orgs to leave out of a platform-level (organization_id=None) copy
    — every org with its OWN individually-active TenantDatabase row,
    i.e. already cut over to their own database via an earlier org-level
    migration. TenantDatabase is a PlatformBase table (always on
    AsyncSessionLocal, regardless of any tenant-tier migration state),
    so this doesn't need a sessionmaker parameter."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TenantDatabase.organization_id).where(
                TenantDatabase.organization_id.is_not(None),
                TenantDatabase.is_active.is_(True),
            )
        )
        return frozenset(result.scalars().all())


# ── The state machine ────────────────────────────────────────────

async def _run(job_id: int, organization_id: int | None, tenant_database_id: int, actor_id: int, ip_address: str | None) -> None:
    target_engine = None
    row = None  # populated inside the try — kept in scope for the except block's secret-scrubbing
    try:
        async with AsyncSessionLocal() as db:
            job = await db.get(TenantMigrationJob, job_id)
            row = await db.get(TenantDatabase, tenant_database_id)
            job.status = TenantMigrationStatus.PROVISIONING_SCHEMA
            job.started_at = datetime.now(timezone.utc)
            await db.commit()
        await _log(job_id, "INFO", f"Starting migration to {row.connection_type} — building target connection.")

        target_engine, target_sessionmaker = await tenant_engine_registry.build_engine_and_sessionmaker(row)
        source_sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)

        await _log(job_id, "INFO", "Provisioning schema on target database.")
        async with target_engine.begin() as conn:
            await conn.run_sync(TenantBase.metadata.create_all)
        await _log(job_id, "INFO", f"Schema provisioned — {len(TenantBase.metadata.tables)} tables.")

        await _set_status(job_id, TenantMigrationStatus.COPYING)

        memo: dict = {}
        scopes = {}
        for table in TenantBase.metadata.sorted_tables:
            scope = _resolve_org_scope(table, memo)
            if scope is None:
                raise RuntimeError(f"Table '{table.name}' has no organization_id column and no foreign-key path to one — refusing to migrate it (would risk copying every organization's rows).")
            scopes[table.name] = scope

        excluded_org_ids: frozenset[int] = frozenset()
        if organization_id is None:
            excluded_org_ids = await _excluded_org_ids()
            if excluded_org_ids:
                await _log(
                    job_id, "INFO",
                    f"Platform migration — excluding {len(excluded_org_ids)} organization(s) already on their own individual database.",
                )

        # Clear the target first (children-before-parents, respecting
        # FK constraints on the target) — makes a retry after a failed
        # attempt safe: never duplicates or double-counts rows left over
        # from a previous partial run.
        async with target_sessionmaker() as session:
            for table in reversed(TenantBase.metadata.sorted_tables):
                await session.execute(delete(table))
            await session.commit()

        watermarks: dict[str, int] = {}
        tables_done = 0
        rows_copied = 0
        for table in TenantBase.metadata.sorted_tables:
            org_col, selectable = scopes[table.name]
            last_id, row_count = await _copy_chunked(source_sessionmaker, target_sessionmaker, table, org_col, selectable, organization_id, after_id=0, excluded_org_ids=excluded_org_ids)
            watermarks[table.name] = last_id
            tables_done += 1
            rows_copied += row_count
            await _advance_copy(job_id, table.name, tables_done, rows_copied)

        await _log(job_id, "INFO", "Row copy complete — verifying counts.")
        await _set_status(job_id, TenantMigrationStatus.VERIFYING)

        mismatches = await _verify_counts(source_sessionmaker, target_sessionmaker, scopes, organization_id, excluded_org_ids)
        if mismatches:
            raise RuntimeError("Row count mismatch after copy: " + "; ".join(mismatches))
        await _log(job_id, "INFO", "Row counts verified — every table matches.")

        # ── Cutover: brief pause, delta copy, re-verify, flip ──────
        await _set_status(job_id, TenantMigrationStatus.CUTTING_OVER)
        tenant_engine_registry.mark_migrating(organization_id)
        async with AsyncSessionLocal() as db:
            row = await db.get(TenantDatabase, tenant_database_id)
            row.status = TenantDatabaseStatus.MIGRATING
            await db.commit()
        if organization_id is not None:
            await _log(job_id, "WARN", "Cutting over — writes for this organization are briefly paused.")
        else:
            await _log(job_id, "WARN", "Cutting over — writes for every organization without its own individual database are briefly paused.")

        try:
            delta_rows = 0
            for table in TenantBase.metadata.sorted_tables:
                org_col, selectable = scopes[table.name]
                _, row_count = await _copy_chunked(source_sessionmaker, target_sessionmaker, table, org_col, selectable, organization_id, after_id=watermarks[table.name], excluded_org_ids=excluded_org_ids)
                delta_rows += row_count
            if delta_rows:
                rows_copied += delta_rows
                await _advance_copy(job_id, None, tables_done, rows_copied)
                await _log(job_id, "INFO", f"Delta copy picked up {delta_rows} row(s) written during the copy/verify window.")
            mismatches = await _verify_counts(source_sessionmaker, target_sessionmaker, scopes, organization_id, excluded_org_ids)
            if mismatches:
                raise RuntimeError("Row count mismatch after delta copy: " + "; ".join(mismatches))
            await _log(job_id, "INFO", "Delta copy verified — cutting over now.")

            if organization_id is not None:
                # Audit BEFORE the flip — written to the SOURCE (still the
                # active database for this org at this exact moment), per
                # the plan: an org's audit history intentionally splits at
                # the migration date.
                async with source_sessionmaker() as src_db:
                    await record_event(
                        src_db, organization_id=organization_id, actor_id=actor_id, action="Backend Database Migration Completed",
                        module="Settings", resource="Backend Database", status=AuditStatus.SUCCESS,
                        ip_address=ip_address, details=f"Cut over to {row.connection_type} (tenant_database_id={tenant_database_id}).",
                    )
            else:
                # Platform-level cutover — logged through the Seller's own
                # audit trail (SellerAuditLog, PlatformBase). There's no
                # single "organization" this event belongs to, and
                # PlatformBase tables never move databases in the first
                # place, so there's no before/after-the-flip session
                # distinction to make the way the org-level path has.
                async with AsyncSessionLocal() as plat_db:
                    await record_seller_event(
                        plat_db, seller_user_id=actor_id, customer_id=None, action="Platform Database Migration Completed",
                        module="Settings", resource="Backend Database",
                        ip_address=ip_address,
                        details=f"Cut over the platform's shared backend database to {row.connection_type} "
                                f"(tenant_database_id={tenant_database_id}, {len(excluded_org_ids)} org(s) excluded as already individually migrated).",
                    )

            async with AsyncSessionLocal() as db:
                row = await db.get(TenantDatabase, tenant_database_id)
                row.is_active = True
                row.status = TenantDatabaseStatus.ACTIVE
                row.activated_at = datetime.now(timezone.utc)
                await db.commit()
            if organization_id is None:
                # Every org without its own individual override may have
                # already cached the (now-stale) fallback sessionmaker
                # under its OWN cache key — a plain evict(None) only
                # clears the platform row's own key. See
                # tenant_engine_registry.evict_all()'s docstring.
                tenant_engine_registry.evict_all()
            else:
                tenant_engine_registry.evict(organization_id)
        finally:
            tenant_engine_registry.clear_migrating(organization_id)

        async with AsyncSessionLocal() as db:
            job = await db.get(TenantMigrationJob, job_id)
            job.status = TenantMigrationStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
        if organization_id is not None:
            await _log(job_id, "INFO", "Migration complete — this organization now routes to its own database.")
        else:
            await _log(job_id, "INFO", "Migration complete — every organization without its own individual database now routes to the platform database.")

    except Exception as exc:
        message = scrub_secrets(str(exc), _secrets_of(row))
        async with AsyncSessionLocal() as db:
            job = await db.get(TenantMigrationJob, job_id)
            was_cutting_over = job.status == TenantMigrationStatus.CUTTING_OVER
            job.status = TenantMigrationStatus.FAILED
            job.error_message = message[:2000]
            job.completed_at = datetime.now(timezone.utc)
            if was_cutting_over:
                # Revert the pause immediately — the org resumes on the
                # still-fully-correct source, having paused only as long
                # as the cutover step itself ran.
                tr = await db.get(TenantDatabase, tenant_database_id)
                tr.status = TenantDatabaseStatus.ACTIVE
            await db.commit()
        tenant_engine_registry.clear_migrating(organization_id)
        await _log(job_id, "ERROR", f"Migration failed: {message}")
    finally:
        if target_engine is not None:
            await target_engine.dispose()


def _secrets_of(row) -> dict:
    if row is None:
        return {}
    return {k: crypto.decrypt_secret(t) for k, t in (row.encrypted_secrets or {}).items()}


# ── Copy / verify primitives ─────────────────────────────────────

async def _copy_chunked(source_sessionmaker, target_sessionmaker, table, org_col, selectable, organization_id, *, after_id: int, excluded_org_ids: frozenset[int] = frozenset()) -> tuple[int, int]:
    """Returns (last_id_copied, row_count) — last_id_copied is the new
    watermark (unchanged from `after_id` if nothing was copied), row_count
    is how many rows this call actually moved."""
    pk = table.c.id
    last_id = after_id
    total = 0
    while True:
        stmt = select(table).select_from(selectable).where(pk > last_id)
        stmt = _org_filter(stmt, org_col, organization_id, excluded_org_ids).order_by(pk).limit(CHUNK_SIZE)
        async with source_sessionmaker() as src:
            rows = (await src.execute(stmt)).mappings().all()
        if not rows:
            break
        payload = [dict(r) for r in rows]
        async with target_sessionmaker() as tgt:
            await tgt.execute(table.insert(), payload)
            await tgt.commit()
        last_id = rows[-1]["id"]
        total += len(rows)
        if len(rows) < CHUNK_SIZE:
            break
    return last_id, total


async def _verify_counts(source_sessionmaker, target_sessionmaker, scopes: dict, organization_id: int | None, excluded_org_ids: frozenset[int] = frozenset()) -> list[str]:
    """Source counts are org-filtered (the source is the shared platform
    database — every other org's rows live there too). Target counts are
    deliberately UNFILTERED — the target is this one org's own dedicated
    database (or, for a platform-level migration, the new platform
    database, which by construction only ever received non-excluded
    rows), so every row already belongs there; for a child table (org_col
    resolved from a parent via a join) the target's plain table has no
    organization_id column of its own to filter by at all, so a bare
    COUNT(*) is both correct and the only option."""
    mismatches = []
    for table_name, (org_col, selectable) in scopes.items():
        table = TenantBase.metadata.tables[table_name]
        stmt = _org_filter(select(func.count()).select_from(selectable), org_col, organization_id, excluded_org_ids)
        async with source_sessionmaker() as src:
            source_count = (await src.execute(stmt)).scalar_one()
        async with target_sessionmaker() as tgt:
            target_count = (await tgt.execute(select(func.count()).select_from(table))).scalar_one()
        if source_count != target_count:
            mismatches.append(f"{table_name}: source={source_count} target={target_count}")
    return mismatches


# ── Job/log helpers ──────────────────────────────────────────────

async def _set_status(job_id: int, status: TenantMigrationStatus) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(TenantMigrationJob, job_id)
        job.status = status
        await db.commit()


async def _advance_copy(job_id: int, table_name: str | None, tables_done: int, rows_copied: int) -> None:
    """table_name is None for the post-cutover delta-copy update, which
    doesn't correspond to any single table (it swept all of them again) —
    tables_done stays at its already-final value in that call."""
    async with AsyncSessionLocal() as db:
        job = await db.get(TenantMigrationJob, job_id)
        if table_name is not None:
            job.current_table = table_name
        job.tables_done = tables_done
        job.rows_copied = rows_copied
        tables_total = job.tables_total
        await db.commit()
    if table_name is not None:
        await _log(job_id, "INFO", f"Copied '{table_name}' ({tables_done}/{tables_total}).")


async def _log(job_id: int, level: str, message: str) -> None:
    async with AsyncSessionLocal() as db:
        db.add(TenantMigrationJobLog(job_id=job_id, level=level, message=str(message)[:2000]))
        await db.commit()


# ── Read helpers for the API layer ───────────────────────────────

async def list_jobs(db: AsyncSession, organization_id: int | None) -> list[TenantMigrationJob]:
    stmt = select(TenantMigrationJob).order_by(TenantMigrationJob.created_at.desc())
    if organization_id is not None:
        stmt = stmt.where(TenantMigrationJob.organization_id == organization_id)
    else:
        stmt = stmt.where(TenantMigrationJob.organization_id.is_(None))
    return list((await db.execute(stmt)).scalars().all())


async def get_job(db: AsyncSession, job_id: int, organization_id: int | None) -> TenantMigrationJob | None:
    stmt = select(TenantMigrationJob).where(TenantMigrationJob.id == job_id)
    if organization_id is not None:
        stmt = stmt.where(TenantMigrationJob.organization_id == organization_id)
    else:
        stmt = stmt.where(TenantMigrationJob.organization_id.is_(None))
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_logs(db: AsyncSession, job_id: int, since_id: int | None = None) -> list[TenantMigrationJobLog]:
    stmt = select(TenantMigrationJobLog).where(TenantMigrationJobLog.job_id == job_id)
    if since_id is not None:
        stmt = stmt.where(TenantMigrationJobLog.id > since_id)
    stmt = stmt.order_by(TenantMigrationJobLog.id)
    return list((await db.execute(stmt)).scalars().all())
