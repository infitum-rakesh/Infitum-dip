"""Connections router — /api/connections/...

Centralized connection management for databases, applications, storage,
API and cloud endpoints. See services/connection_service.py for the
encryption boundary — secret values never leave that module in plaintext.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, ConnectorUnsupportedError, ConnectorValidationError
from app.core.deps import ROLE_ACTIONS, require_action
from app.database.connection import get_db
from app.models.user import User, UserRole
from app.schemas.connections import (
    ApiPreviewRequest,
    ColumnListOut,
    ColumnOut,
    ConnectionCreateRequest,
    ConnectionListOut,
    ConnectionOut,
    ConnectionStatsOut,
    ConnectionTypeSchemaOut,
    ConnectionUpdateRequest,
    ObjectListOut,
    ObjectOut,
    PreviewOut,
    PreviewRequest,
    SchemaListOut,
    TestConnectionResult,
)
from app.services import connection_service as svc

router = APIRouter(prefix="/connections", tags=["connections"])

# Connections is a foundational, always-available module — no per-module
# license gate (module=None), just role-tier action checks. See
# app.core.deps.require_action for what module=None means.
_view = require_action("VIEW", module=None)
_create = require_action("CREATE", module=None)
_edit = require_action("EDIT", module=None)
_execute = require_action("EXECUTE", module=None)
_delete = require_action("DELETE", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _has_action(user: User, action: str) -> bool:
    return user.role == UserRole.SUPER_ADMIN or action in ROLE_ACTIONS.get(user.role, set())


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")


async def _get_or_404(db: AsyncSession, connection_id: int, user: User):
    conn = await svc.get_connection(db, connection_id, _effective_org_id(user))
    if conn is None:
        raise _not_found()
    return conn


def _require_secret_permission(user: User, connection_type: str, raw_config: dict) -> None:
    """CONNECTION_MANAGE_SECRET gate — only enforced when the payload
    actually attempts to set/rotate a secret field."""
    secret_keys = svc.secret_keys_for(connection_type)
    touches_secret = any(k in secret_keys and v not in (None, "") for k, v in raw_config.items())
    if touches_secret and not _has_action(user, "ADMIN"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Rotating or setting connection credentials requires secret-management permission.",
        )


# ── KPIs / type catalogue ─────────────────────────────────────────
# NOTE: literal-path routes must stay registered before /{connection_id}
# routes below — Starlette matches by position, not type, so /stats or
# /types would otherwise be captured as a connection_id and 422.

@router.get("/stats", response_model=ConnectionStatsOut)
async def stats(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await svc.get_stats(db, _effective_org_id(user))


@router.get("/types", response_model=ConnectionTypeSchemaOut)
async def types(user: User = Depends(_view)):
    """Single source of truth for the New Connection wizard + dynamic form."""
    return svc.get_type_catalogue()


# ── List / get ────────────────────────────────────────────────────

@router.get("", response_model=ConnectionListOut)
async def list_connections(
    q: str = Query("", description="Search name, type, category, environment, status, owner, host"),
    category: str | None = Query(None),
    connection_type: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    environment: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("created_at"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_db),
):
    conns, total, name_map = await svc.list_connections(
        db, organization_id=_effective_org_id(user), search=q, category=category,
        connection_type=connection_type, status_filter=status_filter, environment=environment,
        page=page, page_size=page_size, sort_by=sort_by, sort_dir=sort_dir,
    )
    items = [svc.to_out(c, name_map.get(c.owner_id)) for c in conns]
    return ConnectionListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    conn = await _get_or_404(db, connection_id, user)
    name_map = await svc.owner_name_map(db, {conn.owner_id} if conn.owner_id else set())
    return svc.to_out(conn, name_map.get(conn.owner_id))


# ── Create / update / delete ──────────────────────────────────────

@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: ConnectionCreateRequest,
    user: User = Depends(_create),
    db: AsyncSession = Depends(get_db),
):
    try:
        conn = await svc.create_connection(
            db, organization_id=user.organization_id, owner_id=user.id,
            name=body.name, category=body.category, connection_type=body.connection_type,
            environment=body.environment, raw_config=body.config,
        )
    except svc.ConnectionValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return svc.to_out(conn, user.full_name)


@router.put("/{connection_id}", response_model=ConnectionOut)
async def update_connection(
    connection_id: int,
    body: ConnectionUpdateRequest,
    user: User = Depends(_edit),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_or_404(db, connection_id, user)
    if body.config:
        _require_secret_permission(user, conn.connection_type.value, body.config)
    conn = await svc.update_connection(db, conn, name=body.name, environment=body.environment, raw_config=body.config)
    name_map = await svc.owner_name_map(db, {conn.owner_id} if conn.owner_id else set())
    return svc.to_out(conn, name_map.get(conn.owner_id))


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(connection_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_db)):
    conn = await _get_or_404(db, connection_id, user)
    await svc.delete_connection(db, conn)


# ── Actions ───────────────────────────────────────────────────────

@router.post("/{connection_id}/test", response_model=TestConnectionResult)
async def test_connection(connection_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_db)):
    conn = await _get_or_404(db, connection_id, user)
    result = await svc.test_connection(db, conn)
    return TestConnectionResult(**result)


@router.post("/{connection_id}/disable", response_model=ConnectionOut)
async def disable_connection(connection_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    conn = await _get_or_404(db, connection_id, user)
    conn = await svc.toggle_disabled(db, conn)
    name_map = await svc.owner_name_map(db, {conn.owner_id} if conn.owner_id else set())
    return svc.to_out(conn, name_map.get(conn.owner_id))


@router.post("/{connection_id}/clone", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
async def clone_connection(connection_id: int, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
    conn = await _get_or_404(db, connection_id, user)
    clone = await svc.clone_connection(db, conn, user.id)
    return svc.to_out(clone, user.full_name)


# ── Schema/object browsing — Source Dataset node (Phase 2) ────────
# All hit the live source (unlike the routes above, which only read
# stored Connection metadata), so gated the same as Test Connection.

@router.get("/{connection_id}/schemas", response_model=SchemaListOut)
async def get_schemas(connection_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_db)):
    conn = await _get_or_404(db, connection_id, user)
    try:
        schemas = await svc.list_schemas(conn)
    except ConnectorUnsupportedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    except ConnectorValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return SchemaListOut(schemas=schemas)


@router.get("/{connection_id}/objects", response_model=ObjectListOut)
async def get_objects(
    connection_id: int,
    schema: str | None = Query(None),
    q: str | None = Query(None, description="Search object names"),
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_or_404(db, connection_id, user)
    try:
        objects = await svc.list_objects(conn, schema, q)
    except ConnectorUnsupportedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    except ConnectorValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ObjectListOut(items=[ObjectOut(name=o.name, schema_name=o.schema, object_type=o.object_type) for o in objects])


@router.get("/{connection_id}/objects/{object_name}/columns", response_model=ColumnListOut)
async def get_columns(
    connection_id: int,
    object_name: str,
    schema: str | None = Query(None),
    include_row_count: bool = Query(False, description="Runs a COUNT(*) — off by default, can be slow on large tables"),
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_or_404(db, connection_id, user)
    try:
        columns, row_count = await svc.get_columns(conn, object_name, schema, include_row_count)
    except ConnectorUnsupportedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    except ConnectorValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ColumnListOut(
        columns=[ColumnOut(**vars(c)) for c in columns],
        row_count=row_count,
    )


@router.post("/{connection_id}/preview", response_model=PreviewOut)
async def preview_connection_data(
    connection_id: int,
    body: PreviewRequest,
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_db),
):
    conn = await _get_or_404(db, connection_id, user)
    try:
        result = await svc.preview(
            conn, schema=body.schema_name, object_name=body.object_name,
            sql=body.sql, parameters=body.parameters, limit=body.limit,
        )
    except ConnectorUnsupportedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    except ConnectorValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return PreviewOut(columns=result.columns, rows=result.rows, row_count=result.row_count, truncated=result.truncated)


@router.post("/{connection_id}/preview-api", response_model=PreviewOut)
async def preview_api_connection_data(
    connection_id: int,
    body: ApiPreviewRequest,
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_db),
):
    """Real preview for an API-category (REST) Source Dataset node — see
    services/connection_service.py's preview_api()."""
    conn = await _get_or_404(db, connection_id, user)
    try:
        result = await svc.preview_api(
            conn, endpoint=body.endpoint, method=body.method, query_params=body.query_params,
            headers=body.headers, body=body.body, json_path=body.json_path,
            pagination=body.pagination, limit=body.limit,
        )
    except ConnectorUnsupportedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    except ConnectorValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    return PreviewOut(columns=result.columns, rows=result.rows, row_count=result.row_count, truncated=result.truncated)
