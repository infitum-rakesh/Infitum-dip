"""
reconciliation_service — the API-facing layer for Phase 7 reconciliation.
Owns ReconciliationJob CRUD and orchestrates a real run: count/checksum
via streamed queries, key/field-level via a bounded in-memory diff — see
reconciliation/engine.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal

from app.connectors.base import ConnectorError, scrub_secrets
from app.models.reconciliation import (
    ExceptionType,
    ReconciliationException,
    ReconciliationJob,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.models.user import User
from app.reconciliation.engine import (
    ConnectorRowSource,
    FileRowSource,
    ReconciliationEngineError,
    compare_counts,
    compute_checksum,
    diff_keys_and_fields,
    require_matching_columns,
)
from app.services import connection_service, mapping_service, uploaded_file_service

MAX_PERSISTED_EXCEPTIONS = 1000

# connection_id=0 is the established sentinel (matches custom_engine.py's
# AI-free extraction path) meaning "this side is a real UploadedDatasetFile,
# not a real Connection row" — see *_uploaded_file_id.
FILE_BACKED_SENTINEL = 0


class ReconciliationServiceError(Exception):
    """Bad request — a referenced connection/object doesn't exist, or the
    job can't be run as configured."""


async def _verify_connection_owned(db: AsyncSession, connection_id: int, organization_id: int | None) -> None:
    """A connection_id in a request body can't be trusted as-is — verify
    it actually belongs to this org (organization_id is None only for a
    true platform-wide/super-admin context) before ever storing it.
    Without this, any user could point a job's source or target at
    another tenant's connection and run real queries against it."""
    if connection_id == FILE_BACKED_SENTINEL or organization_id is None:
        return
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise ReconciliationServiceError(f"Connection {connection_id} was not found.")


async def _verify_uploaded_file_owned(db: AsyncSession, uploaded_file_id: int | None, organization_id: int | None) -> None:
    if uploaded_file_id is None:
        return
    record = await uploaded_file_service.get_by_id(db, uploaded_file_id, organization_id)
    if record is None:
        raise ReconciliationServiceError(f"Uploaded file {uploaded_file_id} was not found.")


async def _assert_name_available(
    db: AsyncSession, *, organization_id: int | None, name: str, exclude_id: int | None = None,
) -> None:
    """Job names only have to be unique within one organization (or within
    the platform-wide/super-admin scope, when organization_id is None) —
    the UI's "Untitled Reconciliation Job" default name otherwise lets a
    user save the same name over and over with no way to tell jobs apart
    in the list. Case-insensitive/trimmed so "Daily Recon " and "daily
    recon" collide too, not just an exact byte match."""
    stmt = select(ReconciliationJob.id).where(
        func.lower(func.trim(ReconciliationJob.name)) == name.strip().lower()
    )
    stmt = stmt.where(ReconciliationJob.organization_id.is_(None)) if organization_id is None \
        else stmt.where(ReconciliationJob.organization_id == organization_id)
    if exclude_id is not None:
        stmt = stmt.where(ReconciliationJob.id != exclude_id)
    existing = (await db.execute(stmt)).first()
    if existing is not None:
        raise ReconciliationServiceError(f"A reconciliation job named \"{name.strip()}\" already exists.")


# ── CRUD ─────────────────────────────────────────────────────────

async def derive_from_column_mapping(
    db: AsyncSession, *, mapping_id: int, mapping_version: int, organization_id: int | None,
) -> dict:
    """CM10 — pure convenience: reads a real, exact ColumnMappingVersion's
    rows and turns them into what ReconciliationJob's own fields already
    understand: column_mapping ({source_column: target_column}, only for
    rows that actually rename — reconciliation/engine.py's
    require_matching_columns() already handles same-named columns without
    any mapping entry) and key_columns (target columns the mapping itself
    flagged is_key, same CM7 precedent as the pipeline target node's
    "Use business keys from Column Mapping" button). Never touches
    diff_keys_and_fields() or any other part of the real comparison
    engine — this only ever prefills two plain fields on the job row
    before it's created, exactly like every other convenience wiring in
    this build.

    Only pure renames (a single source column, no lookup/transformation/
    constant_value) become a column_mapping entry — reconciliation
    compares raw values under an assumed-equal identity, so a lookup or
    transformation row (whose target value is genuinely derived, not
    just renamed — e.g. a looked-up customer NAME sitting where the
    source had a customer_id) would make the comparison compare two
    structurally different values and always mismatch. Excluding those
    rows here is a correctness choice, not an oversight."""
    version = await mapping_service.get_version(db, mapping_id, mapping_version)
    if version is None:
        raise ReconciliationServiceError(f"Column Mapping {mapping_id} version {mapping_version} was not found.")
    column_mapping: dict[str, str] = {}
    key_columns: list[str] = []
    for row in (version.mappings or []):
        target = row.get("target_column")
        sources = row.get("source_columns") or []
        if not target:
            continue
        is_pure_rename = len(sources) == 1 and not row.get("lookup") and not row.get("transformation") and row.get("constant_value") is None
        # key_columns/compare_columns are always expressed in SOURCE-side
        # names — reconciliation_service.py's own key/compare-column
        # resolution (below) translates through column_mapping to find
        # the target-side equivalent, exactly like diff_keys_and_fields()'s
        # own docstring describes. Only a pure-rename row has a real
        # source-side name to use here; an is_key row that's constant/
        # looked-up/transformed has no source column that actually
        # identifies a record on both sides, so it can't become a key.
        if row.get("is_key") and is_pure_rename:
            key_columns.append(sources[0])
        if is_pure_rename and sources[0] != target:
            column_mapping[sources[0]] = target
    return {"column_mapping": column_mapping, "key_columns": key_columns}


async def create_job(
    db: AsyncSession, *, organization_id: int | None, name: str,
    source_connection_id: int, source_schema: str | None, source_object: str,
    target_connection_id: int, target_schema: str | None, target_object: str,
    key_columns: list[str], compare_columns: list[str], comparison_types: list[str],
    created_by: int | None, column_mapping: dict[str, str] | None = None,
    source_uploaded_file_id: int | None = None, target_uploaded_file_id: int | None = None,
    source_mapping_id: int | None = None, source_mapping_version: int | None = None,
) -> ReconciliationJob:
    await _assert_name_available(db, organization_id=organization_id, name=name)
    await _verify_connection_owned(db, source_connection_id, organization_id)
    await _verify_connection_owned(db, target_connection_id, organization_id)
    await _verify_uploaded_file_owned(db, source_uploaded_file_id, organization_id)
    await _verify_uploaded_file_owned(db, target_uploaded_file_id, organization_id)
    # CM10 — only fills in what the caller left empty; an explicitly
    # provided column_mapping/key_columns is never overridden by the
    # mapping-derived values.
    if source_mapping_id is not None and source_mapping_version is not None:
        derived = await derive_from_column_mapping(
            db, mapping_id=source_mapping_id, mapping_version=source_mapping_version, organization_id=organization_id,
        )
        if not column_mapping:
            column_mapping = derived["column_mapping"]
        if not key_columns:
            key_columns = derived["key_columns"]
    row = ReconciliationJob(
        organization_id=organization_id, name=name,
        source_connection_id=source_connection_id, source_schema=source_schema, source_object=source_object,
        source_uploaded_file_id=source_uploaded_file_id,
        target_connection_id=target_connection_id, target_schema=target_schema, target_object=target_object,
        target_uploaded_file_id=target_uploaded_file_id,
        key_columns=key_columns, compare_columns=compare_columns, comparison_types=comparison_types,
        column_mapping=column_mapping or {},
        created_by=created_by, updated_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_job(
    db: AsyncSession, row: ReconciliationJob, *, name: str | None,
    key_columns: list[str] | None, compare_columns: list[str] | None, comparison_types: list[str] | None,
    updated_by: int | None, column_mapping: dict[str, str] | None = None,
) -> ReconciliationJob:
    # Source/target (connection-or-file) are set once at creation and
    # locked in the UI thereafter — same existing convention as
    # source_object/target_object, which update_job has never accepted
    # either. Only name/rules/comparison config are editable post-create.
    if name and name.strip() != row.name:
        await _assert_name_available(db, organization_id=row.organization_id, name=name, exclude_id=row.id)
        row.name = name
    if key_columns is not None:
        row.key_columns = key_columns
    if compare_columns is not None:
        row.compare_columns = compare_columns
    if comparison_types is not None:
        row.comparison_types = comparison_types
    if column_mapping is not None:
        row.column_mapping = column_mapping
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


async def delete_job(db: AsyncSession, row: ReconciliationJob) -> None:
    # Explicit cascade — the models declare no ORM/DB-level cascade, so a
    # bare delete(row) would leave this job's ReconciliationRun rows (and
    # their ReconciliationException children) orphaned in the table,
    # job_id still pointing at the now-deleted id. This app's own
    # control-plane DB is SQLite, which recycles integer primary keys
    # after a delete (no AUTOINCREMENT keyword) — so a *later* job can be
    # assigned that exact same id and, via nothing but a matching job_id,
    # silently inherit the deleted job's entire run history. The new job
    # then looks like it already has a "last run" the instant it's
    # created, before anyone has clicked Run.
    run_ids = (await db.execute(select(ReconciliationRun.id).where(ReconciliationRun.job_id == row.id))).scalars().all()
    if run_ids:
        await db.execute(delete(ReconciliationException).where(ReconciliationException.run_id.in_(run_ids)))
        await db.execute(delete(ReconciliationRun).where(ReconciliationRun.job_id == row.id))
    await db.delete(row)
    await db.commit()


async def get_job(db: AsyncSession, job_id: int, organization_id: int | None) -> ReconciliationJob | None:
    stmt = select(ReconciliationJob).where(ReconciliationJob.id == job_id)
    if organization_id is not None:
        stmt = stmt.where(ReconciliationJob.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_jobs(db: AsyncSession, organization_id: int | None) -> tuple[list[ReconciliationJob], int]:
    stmt = select(ReconciliationJob)
    if organization_id is not None:
        stmt = stmt.where(ReconciliationJob.organization_id == organization_id)
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


# ── Run ──────────────────────────────────────────────────────────

async def _build_row_source(db: AsyncSession, *, connection_id: int, schema: str | None, object_name: str,
                             uploaded_file_id: int | None, organization_id: int | None, side: str):
    """One side (source or target) of a job — either a real DB connection
    or a real uploaded file, resolved to a RowSource plus the raw
    connector underneath (needed by callers for secret-scrubbing on
    error). `side` is only used to name which one in error messages."""
    if uploaded_file_id is not None:
        record = await uploaded_file_service.get_by_id(db, uploaded_file_id, organization_id)
        if record is None:
            raise ReconciliationServiceError(f"The uploaded {side} file for this job no longer exists or no longer belongs to this organization.")
        connector = uploaded_file_service.build_connector(record)
        return FileRowSource(connector, record.filename), connector
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise ReconciliationServiceError(f"The {side} connection for this job no longer exists or no longer belongs to this organization.")
    connector = connection_service.build_connector(conn)
    return ConnectorRowSource(connector, schema, object_name), connector


async def run_job(db: AsyncSession, *, organization_id: int | None, job: ReconciliationJob, created_by: int | None) -> ReconciliationRun:
    # Defense in depth: re-verify both sides against job's OWN
    # organization_id at run time too, not just at creation — protects
    # any row that predates this check, or reached the DB by another path.
    source, source_connector = await _build_row_source(
        db, connection_id=job.source_connection_id, schema=job.source_schema, object_name=job.source_object,
        uploaded_file_id=job.source_uploaded_file_id, organization_id=job.organization_id, side="source",
    )
    target, target_connector = await _build_row_source(
        db, connection_id=job.target_connection_id, schema=job.target_schema, object_name=job.target_object,
        uploaded_file_id=job.target_uploaded_file_id, organization_id=job.organization_id, side="target",
    )

    run = ReconciliationRun(
        organization_id=organization_id, job_id=job.id,
        status=ReconciliationRunStatus.RUNNING, started_at=datetime.now(timezone.utc), created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    types = set(job.comparison_types or ["COUNT"])
    # {source_column: target_column} — a source column with no entry here
    # falls back to the same name on the target (the pre-existing
    # assumption), which is exactly "if not mapped then find same column
    # name in target." Every column list below is resolved through this
    # once, source-side names stay canonical (used for display/keys),
    # target-side names are whatever _map() resolves them to.
    column_mapping = job.column_mapping or {}

    def _map(cols):
        return [column_mapping.get(c, c) for c in cols]

    try:
        def _do():
            outcome: dict = {}
            if "COUNT" in types:
                outcome["source_count"] = compare_counts(source)
                outcome["target_count"] = compare_counts(target)
            if "CHECKSUM" in types:
                cols = job.compare_columns or job.key_columns
                if not cols:
                    # Neither compare columns nor key columns were picked —
                    # rather than failing with "needs at least one column"
                    # (compute_checksum's real, disclosed guard), fall back
                    # to every real column on the source so a Checksum-only
                    # job still works with zero setup beyond picking
                    # source/target — a real describe() call, not a
                    # guessed/fabricated field list. Column mapping still
                    # applies to resolve each of those names on the target.
                    cols = source.get_columns()
                target_cols = _map(cols)
                require_matching_columns(source, cols, target, target_cols)
                s_hash, s_n = compute_checksum(source, cols)
                t_hash, t_n = compute_checksum(target, target_cols)
                outcome["checksum_match"] = (s_hash == t_hash)
                outcome.setdefault("source_count", s_n)
                outcome.setdefault("target_count", t_n)
            if "KEY_MATCH" in types or "FIELD_LEVEL" in types:
                compare_cols = job.compare_columns if "FIELD_LEVEL" in types else []
                needed_cols = list(job.key_columns or []) + [c for c in compare_cols if c not in (job.key_columns or [])]
                require_matching_columns(source, needed_cols, target, _map(needed_cols))
                diff = diff_keys_and_fields(
                    source, target,
                    job.key_columns, compare_cols,
                    target_key_columns=_map(job.key_columns or []), target_compare_columns=_map(compare_cols),
                )
                outcome["source_count"] = diff["source_count"]
                outcome["target_count"] = diff["target_count"]
                outcome["matched_count"] = diff["matched_count"]
                outcome["missing_keys"] = diff["missing_keys"]
                outcome["extra_keys"] = diff["extra_keys"]
                outcome["mismatches"] = diff["mismatches"]
            return outcome
        outcome = await asyncio.to_thread(_do)
    except (ReconciliationEngineError, ConnectorError) as exc:
        run.status = ReconciliationRunStatus.FAILED
        run.error_message = scrub_secrets(str(exc), source_connector.secrets, target_connector.secrets)
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)
        return run
    except Exception as exc:
        run.status = ReconciliationRunStatus.FAILED
        run.error_message = scrub_secrets(str(exc), source_connector.secrets, target_connector.secrets)
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)
        return run

    run.status = ReconciliationRunStatus.SUCCEEDED
    run.source_count = outcome.get("source_count")
    run.target_count = outcome.get("target_count")
    run.matched_count = outcome.get("matched_count")
    run.checksum_match = outcome.get("checksum_match")
    missing_keys = outcome.get("missing_keys") or set()
    extra_keys = outcome.get("extra_keys") or set()
    mismatches = outcome.get("mismatches") or []
    if "missing_keys" in outcome:
        run.missing_count = len(missing_keys)
        run.extra_count = len(extra_keys)
        run.mismatched_count = len(mismatches)
    run.completed_at = datetime.now(timezone.utc)

    persisted = 0
    truncated = False

    def _persist(exc_type: ExceptionType, key_tuple, column=None, source_value=None, target_value=None) -> bool:
        nonlocal persisted, truncated
        if persisted >= MAX_PERSISTED_EXCEPTIONS:
            truncated = True
            return False
        # Raw DB values (Decimal/date/datetime/...) aren't all JSON-
        # serializable as-is — stringify for storage, same as
        # source_value/target_value below.
        key_value = {col: (str(v) if v is not None else None) for col, v in zip(job.key_columns, key_tuple)}
        db.add(ReconciliationException(
            run_id=run.id, exception_type=exc_type, key_value=key_value,
            column=column,
            source_value=str(source_value) if source_value is not None else None,
            target_value=str(target_value) if target_value is not None else None,
        ))
        persisted += 1
        return True

    for k in missing_keys:
        if not _persist(ExceptionType.MISSING, k):
            break
    for k in extra_keys:
        if not _persist(ExceptionType.EXTRA, k):
            break
    for m in mismatches:
        if not _persist(ExceptionType.MISMATCH, m["key"], column=m["column"], source_value=m["source_value"], target_value=m["target_value"]):
            break

    run.exceptions_truncated = truncated
    await db.commit()
    await db.refresh(run)
    return run


async def get_run(db: AsyncSession, run_id: int, organization_id: int | None) -> ReconciliationRun | None:
    stmt = select(ReconciliationRun).where(ReconciliationRun.id == run_id)
    if organization_id is not None:
        stmt = stmt.where(ReconciliationRun.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_runs(db: AsyncSession, job_id: int, organization_id: int | None) -> tuple[list[ReconciliationRun], int]:
    stmt = select(ReconciliationRun).where(ReconciliationRun.job_id == job_id)
    if organization_id is not None:
        stmt = stmt.where(ReconciliationRun.organization_id == organization_id)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return rows, len(rows)


async def list_exceptions(db: AsyncSession, run_id: int, exception_type: str | None = None) -> tuple[list[ReconciliationException], int]:
    stmt = select(ReconciliationException).where(ReconciliationException.run_id == run_id)
    if exception_type:
        stmt = stmt.where(ReconciliationException.exception_type == exception_type)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    return rows, len(rows)
