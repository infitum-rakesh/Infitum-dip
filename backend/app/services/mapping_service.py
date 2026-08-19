"""
mapping_service — the API-facing layer for Object & Column Mapping
(Phase 4). Owns ColumnMapping CRUD and is the seam between the pure
mapping/ engine (auto_mapper, mapper, datatype_mapper — no I/O) and the
real connectors that supply live source/target column lists.

Source/target schema is always re-fetched live here, never cached from
the stored mapping row — see models/column_mapping.py module docstring.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError
from app.mapping import auto_mapper, mapper
from app.models.column_mapping import ColumnMapping
from app.models.connection import Connection
from app.models.user import User
from app.services import connection_service
from app.schemas.mapping import ObjectRef


class MappingValidationError(Exception):
    """Bad request — a referenced connection/object doesn't exist, or the
    mapping definition can't be turned into SQL."""


async def _resolve_connection(db: AsyncSession, connection_id: int, organization_id: int | None) -> Connection:
    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise MappingValidationError(f"Connection {connection_id} was not found.")
    return conn


async def fetch_columns(db: AsyncSession, ref: ObjectRef, organization_id: int | None) -> list:
    conn = await _resolve_connection(db, ref.connection_id, organization_id)
    try:
        columns, _ = await connection_service.get_columns(conn, ref.object_name, ref.schema_name, False)
    except ConnectorError as exc:
        raise MappingValidationError(str(exc)) from exc
    return columns


def columns_to_dicts(columns: list) -> list[dict]:
    return [{"name": c.name, "data_type": c.data_type} for c in columns]


# ── CRUD ─────────────────────────────────────────────────────────

async def create_mapping(
    db: AsyncSession, *, organization_id: int | None, name: str, source: ObjectRef, target: ObjectRef,
    mappings: list[dict], created_by: int | None,
) -> ColumnMapping:
    # Both connections must genuinely belong to this org (or organization_id
    # is None — a true platform-wide/super-admin context) — never trust a
    # connection_id from the request body as-is. Without this, any
    # authenticated user could save a mapping pointing at another
    # tenant's connection and later run Preview/Validate/Load against it.
    if organization_id is not None:
        await _resolve_connection(db, source.connection_id, organization_id)
        await _resolve_connection(db, target.connection_id, organization_id)
    row = ColumnMapping(
        organization_id=organization_id, name=name,
        source_connection_id=source.connection_id, source_schema=source.schema_name, source_object=source.object_name,
        target_connection_id=target.connection_id, target_schema=target.schema_name, target_object=target.object_name,
        mappings=mappings, status="DRAFT", created_by=created_by, updated_by=created_by,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_mapping(db: AsyncSession, row: ColumnMapping, *, name: str | None, mappings: list[dict] | None, updated_by: int | None) -> ColumnMapping:
    if name:
        row.name = name
    if mappings is not None:
        row.mappings = mappings
        row.status = "DRAFT"  # any edit invalidates the last validate() outcome until re-run
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


async def delete_mapping(db: AsyncSession, row: ColumnMapping) -> None:
    await db.delete(row)
    await db.commit()


async def get_mapping(db: AsyncSession, mapping_id: int, organization_id: int | None) -> ColumnMapping | None:
    stmt = select(ColumnMapping).where(ColumnMapping.id == mapping_id)
    if organization_id is not None:
        stmt = stmt.where(ColumnMapping.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_mappings(db: AsyncSession, organization_id: int | None) -> tuple[list[ColumnMapping], int]:
    stmt = select(ColumnMapping)
    if organization_id is not None:
        stmt = stmt.where(ColumnMapping.organization_id == organization_id)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    rows.sort(key=lambda r: r.updated_at, reverse=True)
    return rows, len(rows)


async def user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    result = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row.id: row.full_name for row in result.all()}


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
