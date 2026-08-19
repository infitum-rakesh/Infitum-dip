"""
validation_service — the API-facing layer for Phase 7 data validation.
Owns ValidationRuleSet CRUD and is the seam between the pure validation/
engine (no I/O) and the real connectors that supply live data.
"""
from __future__ import annotations

from datetime import datetime, timezone

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError
from app.models.user import User
from app.models.validation import ValidationRun, ValidationRuleResult, ValidationRuleSet, ValidationRunStatus
from app.services import connection_service
from app.validation import engine as validation_engine
from app.validation.engine import failed_records_sql
from app.validation.rules import RuleValidationError


class ValidationServiceError(Exception):
    """Bad request — a referenced connection/object doesn't exist, or the
    rule set can't be turned into SQL."""


# ── CRUD ─────────────────────────────────────────────────────────

async def _verify_connection_owned(db: AsyncSession, connection_id: int, organization_id: int | None) -> None:
    """A connection_id in a request body can't be trusted as-is — verify
    it actually belongs to this org (organization_id is None only for a
    true platform-wide/super-admin context) before ever storing it.
    Without this, any user could point a rule set at another tenant's
    connection and run real queries against it."""
    if organization_id is None:
        return
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise ValidationServiceError(f"Connection {connection_id} was not found.")


async def create_rule_set(
    db: AsyncSession, *, organization_id: int | None, name: str, connection_id: int,
    schema_name: str | None, object_name: str, rules: list[dict], created_by: int | None,
) -> ValidationRuleSet:
    await _verify_connection_owned(db, connection_id, organization_id)
    row = ValidationRuleSet(
        organization_id=organization_id, name=name, connection_id=connection_id,
        schema_name=schema_name, object_name=object_name, rules=rules,
        created_by=created_by, updated_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_rule_set(db: AsyncSession, row: ValidationRuleSet, *, name: str | None, rules: list[dict] | None, updated_by: int | None) -> ValidationRuleSet:
    if name:
        row.name = name
    if rules is not None:
        row.rules = rules
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


async def delete_rule_set(db: AsyncSession, row: ValidationRuleSet) -> None:
    await db.delete(row)
    await db.commit()


async def get_rule_set(db: AsyncSession, rule_set_id: int, organization_id: int | None) -> ValidationRuleSet | None:
    stmt = select(ValidationRuleSet).where(ValidationRuleSet.id == rule_set_id)
    if organization_id is not None:
        stmt = stmt.where(ValidationRuleSet.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_rule_sets(db: AsyncSession, organization_id: int | None) -> tuple[list[ValidationRuleSet], int]:
    stmt = select(ValidationRuleSet)
    if organization_id is not None:
        stmt = stmt.where(ValidationRuleSet.organization_id == organization_id)
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

async def run_rule_set(db: AsyncSession, *, organization_id: int | None, rule_set: ValidationRuleSet, created_by: int | None) -> ValidationRun:
    # Defense in depth: re-verify ownership against rule_set's OWN
    # organization_id (not the caller's, though they're the same in
    # practice) at run time too, not just at creation — protects any row
    # that predates this check, or reached the DB by another path.
    conn = await connection_service.get_connection(db, rule_set.connection_id, rule_set.organization_id)
    if conn is None:
        raise ValidationServiceError(f"Connection {rule_set.connection_id} was not found or no longer belongs to this organization.")
    connector = connection_service.build_connector(conn)

    run = ValidationRun(
        organization_id=organization_id, rule_set_id=rule_set.id,
        status=ValidationRunStatus.RUNNING, started_at=datetime.now(timezone.utc), created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    try:
        def _run():
            return validation_engine.run_rule_set(connector, rule_set.schema_name, rule_set.object_name, rule_set.rules)
        outcome = await asyncio.to_thread(_run)
    except (RuleValidationError, ConnectorError) as exc:
        run.status = ValidationRunStatus.FAILED
        run.error_message = connector._scrub(str(exc))
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)
        return run
    except Exception as exc:  # a real DB error (e.g. bad column name) — surfaced, not swallowed
        run.status = ValidationRunStatus.FAILED
        run.error_message = connector._scrub(str(exc))
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)
        return run

    run.status = ValidationRunStatus.SUCCEEDED
    run.total_records = outcome["total_records"]
    run.completed_at = datetime.now(timezone.utc)
    for r in outcome["results"]:
        db.add(ValidationRuleResult(
            run_id=run.id, rule_id=r["rule_id"], rule_name=r["rule_name"], rule_type=r["rule_type"],
            columns=r["columns"], severity=r["severity"], status=r["status"],
            pass_count=r["pass_count"], fail_count=r["fail_count"], pass_rate=r["pass_rate"],
            sql_condition=r["sql_condition"], sql_joins=r["sql_joins"],
        ))
    await db.commit()
    await db.refresh(run)
    return run


async def get_run(db: AsyncSession, run_id: int, organization_id: int | None) -> ValidationRun | None:
    stmt = select(ValidationRun).where(ValidationRun.id == run_id)
    if organization_id is not None:
        stmt = stmt.where(ValidationRun.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_results(db: AsyncSession, run_id: int) -> list[ValidationRuleResult]:
    result = await db.execute(select(ValidationRuleResult).where(ValidationRuleResult.run_id == run_id))
    return list(result.scalars().all())


async def list_runs(db: AsyncSession, rule_set_id: int, organization_id: int | None) -> tuple[list[ValidationRun], int]:
    stmt = select(ValidationRun).where(ValidationRun.rule_set_id == rule_set_id)
    if organization_id is not None:
        stmt = stmt.where(ValidationRun.organization_id == organization_id)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.created_at, reverse=True)
    return rows, len(rows)


async def get_failed_records(
    db: AsyncSession, *, rule_result: ValidationRuleResult, rule_set: ValidationRuleSet, organization_id: int | None, limit: int,
):
    conn = await connection_service.get_connection(db, rule_set.connection_id, rule_set.organization_id)
    if conn is None:
        raise ValidationServiceError(f"Connection {rule_set.connection_id} was not found or no longer belongs to this organization.")
    connector = connection_service.build_connector(conn)
    bounded_limit = max(1, min(limit or 50, 1000))

    def _fetch():
        sql = failed_records_sql(connector, rule_set.schema_name, rule_set.object_name, rule_result.sql_condition, rule_result.sql_joins)
        return connector.preview_sql(sql, {}, bounded_limit)

    try:
        return await asyncio.to_thread(_fetch)
    except ConnectorError as exc:
        raise ValidationServiceError(str(exc)) from exc
