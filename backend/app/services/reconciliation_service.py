"""
reconciliation_service — the API-facing layer for Phase 7 reconciliation.
Owns ReconciliationJob CRUD and orchestrates a real run: count/checksum
via streamed queries, key/field-level via a bounded in-memory diff — see
reconciliation/engine.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, scrub_secrets
from app.models.reconciliation import (
    ExceptionType,
    ReconciliationException,
    ReconciliationJob,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.models.user import User
from app.reconciliation.engine import ReconciliationEngineError, compare_counts, compute_checksum, diff_keys_and_fields
from app.services import connection_service

MAX_PERSISTED_EXCEPTIONS = 1000


class ReconciliationServiceError(Exception):
    """Bad request — a referenced connection/object doesn't exist, or the
    job can't be run as configured."""


async def _verify_connection_owned(db: AsyncSession, connection_id: int, organization_id: int | None) -> None:
    """A connection_id in a request body can't be trusted as-is — verify
    it actually belongs to this org (organization_id is None only for a
    true platform-wide/super-admin context) before ever storing it.
    Without this, any user could point a job's source or target at
    another tenant's connection and run real queries against it."""
    if organization_id is None:
        return
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise ReconciliationServiceError(f"Connection {connection_id} was not found.")


# ── CRUD ─────────────────────────────────────────────────────────

async def create_job(
    db: AsyncSession, *, organization_id: int | None, name: str,
    source_connection_id: int, source_schema: str | None, source_object: str,
    target_connection_id: int, target_schema: str | None, target_object: str,
    key_columns: list[str], compare_columns: list[str], comparison_types: list[str],
    created_by: int | None,
) -> ReconciliationJob:
    await _verify_connection_owned(db, source_connection_id, organization_id)
    await _verify_connection_owned(db, target_connection_id, organization_id)
    row = ReconciliationJob(
        organization_id=organization_id, name=name,
        source_connection_id=source_connection_id, source_schema=source_schema, source_object=source_object,
        target_connection_id=target_connection_id, target_schema=target_schema, target_object=target_object,
        key_columns=key_columns, compare_columns=compare_columns, comparison_types=comparison_types,
        created_by=created_by, updated_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_job(
    db: AsyncSession, row: ReconciliationJob, *, name: str | None,
    key_columns: list[str] | None, compare_columns: list[str] | None, comparison_types: list[str] | None,
    updated_by: int | None,
) -> ReconciliationJob:
    if name:
        row.name = name
    if key_columns is not None:
        row.key_columns = key_columns
    if compare_columns is not None:
        row.compare_columns = compare_columns
    if comparison_types is not None:
        row.comparison_types = comparison_types
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


async def delete_job(db: AsyncSession, row: ReconciliationJob) -> None:
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
    result = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row.id: row.full_name for row in result.all()}


# ── Run ──────────────────────────────────────────────────────────

async def run_job(db: AsyncSession, *, organization_id: int | None, job: ReconciliationJob, created_by: int | None) -> ReconciliationRun:
    # Defense in depth: re-verify both connections against job's OWN
    # organization_id at run time too, not just at creation — protects
    # any row that predates this check, or reached the DB by another path.
    source_conn = await connection_service.get_connection(db, job.source_connection_id, job.organization_id)
    target_conn = await connection_service.get_connection(db, job.target_connection_id, job.organization_id)
    if source_conn is None or target_conn is None:
        raise ReconciliationServiceError("The source or target connection for this job no longer exists or no longer belongs to this organization.")
    source_connector = connection_service.build_connector(source_conn)
    target_connector = connection_service.build_connector(target_conn)

    run = ReconciliationRun(
        organization_id=organization_id, job_id=job.id,
        status=ReconciliationRunStatus.RUNNING, started_at=datetime.now(timezone.utc), created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    types = set(job.comparison_types or ["COUNT"])

    try:
        def _do():
            outcome: dict = {}
            if "COUNT" in types:
                outcome["source_count"] = compare_counts(source_connector, job.source_schema, job.source_object)
                outcome["target_count"] = compare_counts(target_connector, job.target_schema, job.target_object)
            if "CHECKSUM" in types:
                cols = job.compare_columns or job.key_columns
                s_hash, s_n = compute_checksum(source_connector, job.source_schema, job.source_object, cols)
                t_hash, t_n = compute_checksum(target_connector, job.target_schema, job.target_object, cols)
                outcome["checksum_match"] = (s_hash == t_hash)
                outcome.setdefault("source_count", s_n)
                outcome.setdefault("target_count", t_n)
            if "KEY_MATCH" in types or "FIELD_LEVEL" in types:
                compare_cols = job.compare_columns if "FIELD_LEVEL" in types else []
                diff = diff_keys_and_fields(
                    source_connector, job.source_schema, job.source_object,
                    target_connector, job.target_schema, job.target_object,
                    job.key_columns, compare_cols,
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
