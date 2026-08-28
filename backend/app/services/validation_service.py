"""
validation_service — the API-facing layer for Phase 7 data validation.
Owns ValidationRuleSet CRUD and is the seam between the pure validation/
engine (no I/O) and the real connectors that supply live data.
"""
from __future__ import annotations

from datetime import datetime, timezone

import asyncio

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal

from app.connectors.base import ConnectorError
from app.models.user import User
from app.models.validation import ValidationRun, ValidationRuleResult, ValidationRuleSet, ValidationRunStatus
from app.reconciliation.engine import ConnectorRowSource, FileRowSource
from app.services import connection_service, uploaded_file_service
from app.validation import engine as validation_engine
from app.validation import file_engine as validation_file_engine
from app.validation.engine import failed_records_sql
from app.validation.rules import RuleValidationError

FILE_BACKED_SENTINEL = 0


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
    if connection_id == FILE_BACKED_SENTINEL or organization_id is None:
        return
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise ValidationServiceError(f"Connection {connection_id} was not found.")


async def _verify_uploaded_file_owned(db: AsyncSession, uploaded_file_id: int | None, organization_id: int | None) -> None:
    if uploaded_file_id is None:
        return
    record = await uploaded_file_service.get_by_id(db, uploaded_file_id, organization_id)
    if record is None:
        raise ValidationServiceError(f"Uploaded file {uploaded_file_id} was not found.")


async def _assert_name_available(
    db: AsyncSession, *, organization_id: int | None, name: str, exclude_id: int | None = None,
) -> None:
    """Rule set names only have to be unique within one organization (or
    within the platform-wide/super-admin scope, when organization_id is
    None) — the UI's "Untitled Validation Rule Set" default name
    otherwise lets a user save the same name over and over with no way to
    tell rule sets apart in the list. Case-insensitive/trimmed so "Daily
    Check " and "daily check" collide too, not just an exact byte match."""
    stmt = select(ValidationRuleSet.id).where(
        func.lower(func.trim(ValidationRuleSet.name)) == name.strip().lower()
    )
    stmt = stmt.where(ValidationRuleSet.organization_id.is_(None)) if organization_id is None \
        else stmt.where(ValidationRuleSet.organization_id == organization_id)
    if exclude_id is not None:
        stmt = stmt.where(ValidationRuleSet.id != exclude_id)
    existing = (await db.execute(stmt)).first()
    if existing is not None:
        raise ValidationServiceError(f"A validation rule set named \"{name.strip()}\" already exists.")


async def create_rule_set(
    db: AsyncSession, *, organization_id: int | None, name: str, connection_id: int,
    schema_name: str | None, object_name: str, uploaded_file_id: int | None, rules: list[dict], created_by: int | None,
) -> ValidationRuleSet:
    await _assert_name_available(db, organization_id=organization_id, name=name)
    await _verify_connection_owned(db, connection_id, organization_id)
    await _verify_uploaded_file_owned(db, uploaded_file_id, organization_id)
    if uploaded_file_id is not None and rules:
        try:
            validation_file_engine.assert_rules_supported(rules)
        except RuleValidationError as exc:
            raise ValidationServiceError(str(exc)) from exc
    row = ValidationRuleSet(
        organization_id=organization_id, name=name, connection_id=connection_id,
        schema_name=schema_name, object_name=object_name, uploaded_file_id=uploaded_file_id, rules=rules,
        created_by=created_by, updated_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_rule_set(db: AsyncSession, row: ValidationRuleSet, *, name: str | None, rules: list[dict] | None, updated_by: int | None) -> ValidationRuleSet:
    if name and name.strip() != row.name:
        await _assert_name_available(db, organization_id=row.organization_id, name=name, exclude_id=row.id)
        row.name = name
    if rules is not None:
        if row.uploaded_file_id is not None:
            try:
                validation_file_engine.assert_rules_supported(rules)
            except RuleValidationError as exc:
                raise ValidationServiceError(str(exc)) from exc
        row.rules = rules
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


async def delete_rule_set(db: AsyncSession, row: ValidationRuleSet) -> None:
    # Explicit cascade — same reasoning as reconciliation_service.delete_job:
    # the models declare no ORM/DB-level cascade, so a bare delete(row)
    # would leave this rule set's ValidationRun rows (and their
    # ValidationRuleResult children) orphaned, rule_set_id still pointing
    # at the now-deleted id. This app's own control-plane DB is SQLite,
    # which recycles integer primary keys after a delete (no AUTOINCREMENT
    # keyword) — so a *later* rule set can be assigned that exact same id
    # and inherit the deleted one's entire run history through nothing but
    # a matching rule_set_id, looking like it already ran the instant it's
    # created.
    run_ids = (await db.execute(select(ValidationRun.id).where(ValidationRun.rule_set_id == row.id))).scalars().all()
    if run_ids:
        await db.execute(delete(ValidationRuleResult).where(ValidationRuleResult.run_id.in_(run_ids)))
        await db.execute(delete(ValidationRun).where(ValidationRun.rule_set_id == row.id))
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
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


# ── Run ──────────────────────────────────────────────────────────

async def _build_validation_source(db: AsyncSession, rule_set: ValidationRuleSet):
    """Returns (kind, callable) where kind is "file" or "db" and calling
    the returned zero-arg function actually runs the rule set. Kept as one
    helper so run_rule_set() below has a single try/except regardless of
    which kind ran — a file-backed rule set never touches ConnectorError
    (no SQL involved) but can still raise RuleValidationError, same as a
    DB-backed one."""
    if rule_set.uploaded_file_id is not None:
        record = await uploaded_file_service.get_by_id(db, rule_set.uploaded_file_id, rule_set.organization_id)
        if record is None:
            raise ValidationServiceError(
                f"The uploaded file for this rule set no longer exists or no longer belongs to this organization."
            )
        connector = uploaded_file_service.build_connector(record)
        source = FileRowSource(connector, record.filename)
        return "file", connector, lambda: validation_file_engine.run_rule_set(source, rule_set.rules)

    conn = await connection_service.get_connection(db, rule_set.connection_id, rule_set.organization_id)
    if conn is None:
        raise ValidationServiceError(f"Connection {rule_set.connection_id} was not found or no longer belongs to this organization.")
    connector = connection_service.build_connector(conn)
    return "db", connector, lambda: validation_engine.run_rule_set(connector, rule_set.schema_name, rule_set.object_name, rule_set.rules)


async def run_rule_set(db: AsyncSession, *, organization_id: int | None, rule_set: ValidationRuleSet, created_by: int | None) -> ValidationRun:
    # Defense in depth: re-verify ownership against rule_set's OWN
    # organization_id (not the caller's, though they're the same in
    # practice) at run time too, not just at creation — protects any row
    # that predates this check, or reached the DB by another path.
    kind, connector, _run_fn = await _build_validation_source(db, rule_set)

    run = ValidationRun(
        organization_id=organization_id, rule_set_id=rule_set.id,
        status=ValidationRunStatus.RUNNING, started_at=datetime.now(timezone.utc), created_by=created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    def _scrub(msg: str) -> str:
        # File-backed connectors carry no DB secrets, but reuse the same
        # scrub path for a uniform code shape — connector._scrub() is a
        # no-op when there's nothing to redact.
        return connector._scrub(msg) if hasattr(connector, "_scrub") else msg

    try:
        outcome = await asyncio.to_thread(_run_fn)
    except (RuleValidationError, ConnectorError) as exc:
        run.status = ValidationRunStatus.FAILED
        run.error_message = _scrub(str(exc))
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)
        return run
    except Exception as exc:  # a real DB/file error (e.g. bad column name) — surfaced, not swallowed
        run.status = ValidationRunStatus.FAILED
        run.error_message = _scrub(str(exc))
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


class _FailedRecordsResult:
    """Mirrors the shape connector.preview_sql() returns (.columns/.rows/
    .truncated) so api/validation.py's get_failed_records route works
    identically for a file-backed rule set, no branching needed there."""

    def __init__(self, columns: list[str], rows: list[list], truncated: bool):
        self.columns = columns
        self.rows = rows
        self.truncated = truncated


async def get_failed_records(
    db: AsyncSession, *, rule_result: ValidationRuleResult, rule_set: ValidationRuleSet, organization_id: int | None, limit: int,
):
    bounded_limit = max(1, min(limit or 50, 1000))

    if rule_set.uploaded_file_id is not None:
        record = await uploaded_file_service.get_by_id(db, rule_set.uploaded_file_id, rule_set.organization_id)
        if record is None:
            raise ValidationServiceError(
                "The uploaded file for this rule set no longer exists or no longer belongs to this organization."
            )
        connector = uploaded_file_service.build_connector(record)
        source = FileRowSource(connector, record.filename)
        rule = {"id": rule_result.rule_id, "name": rule_result.rule_name, "type": rule_result.rule_type, "columns": rule_result.columns}

        def _fetch_file():
            cols, rows = validation_file_engine.failed_records(source, rule, bounded_limit + 1)
            truncated = len(rows) > bounded_limit
            return _FailedRecordsResult(cols, rows[:bounded_limit], truncated)

        try:
            return await asyncio.to_thread(_fetch_file)
        except RuleValidationError as exc:
            raise ValidationServiceError(str(exc)) from exc

    conn = await connection_service.get_connection(db, rule_set.connection_id, rule_set.organization_id)
    if conn is None:
        raise ValidationServiceError(f"Connection {rule_set.connection_id} was not found or no longer belongs to this organization.")
    connector = connection_service.build_connector(conn)

    def _fetch():
        sql = failed_records_sql(connector, rule_set.schema_name, rule_set.object_name, rule_result.sql_condition, rule_result.sql_joins)
        return connector.preview_sql(sql, {}, bounded_limit)

    try:
        return await asyncio.to_thread(_fetch)
    except ConnectorError as exc:
        raise ValidationServiceError(str(exc)) from exc
