"""Connections router — /api/connections/...

Centralized connection management for databases, applications, storage,
API and cloud endpoints. See services/connection_service.py for the
encryption boundary — secret values never leave that module in plaintext.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, ConnectorUnsupportedError, ConnectorValidationError
from app.core.deps import ROLE_ACTIONS, require_action
from app.database.tenant_routing import get_tenant_db
from app.models.ai_connection import ORG_DEFAULT_USER_ID
from app.models.connection import Connection, ConnectionCategory
from app.models.user import User, UserRole
from app.services import ai_connection_permission_service as ai_access_svc
from app.services import ai_connection_resolver_service as ai_default_svc
from app.services.ai_connection_access_service import ai_connection_access_service
from app.schemas.ai_connection_access import (
    AiAccessListOut,
    AiDefaultsOut,
    GrantOut,
    GrantRequest,
    GranteeOptionsOut,
    SetDefaultRequest,
    SetPublicRequest,
    UsableAiConnectionOut,
    UsableAiConnectionsOut,
)
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


def _require_org(user: User) -> int:
    """AI Connection access management (AIC-P5) is an org-admin self-
    service feature — see ai_connection_permission_service.py's module
    docstring for why SUPER_ADMIN/SELLER (structurally org-less) are out
    of scope rather than silently resolved to some org."""
    if user.organization_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This feature is scoped to a single organization's own admin — not available for platform-level accounts.")
    return user.organization_id


def _require_secret_permission(user: User, conn, raw_config: dict) -> None:
    """CONNECTION_MANAGE_SECRET (ADMIN-tier) gate — only enforced when the
    payload actually attempts to set/rotate a secret field, AND the caller
    isn't the connection's own owner. The owner exemption matters: they
    already typed that secret in themselves at create time (CREATE has no
    ADMIN gate at all, by the same reasoning — see connection_service.py's
    create_connection()), so requiring ADMIN just to update it later
    protects nothing from them — it was only ever meant to stop a
    *different*, lower-privileged user from rotating a credential they
    never had. Without this exemption, a connection's own creator (e.g. a
    Data Engineer — the default trial-signup role, see QQ1) could set a
    connection's password once and then never touch it again — reported
    live as "not able to set connection password in edit mode" while
    editing a self-owned Fabric connection."""
    secret_keys = svc.secret_keys_for(conn.connection_type.value)
    touches_secret = any(k in secret_keys and v not in (None, "") for k, v in raw_config.items())
    is_owner = conn.owner_id is not None and conn.owner_id == user.id
    if touches_secret and not is_owner and not _has_action(user, "ADMIN"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Rotating or setting connection credentials requires secret-management permission.",
        )


def _require_ai_owner_or_admin(user: User, conn: Connection) -> None:
    """AI Provider connections only: being GRANTED view/use access
    (AIConnectionPermission, AIC-P5) never implies being allowed to manage
    the connection itself — only an admin-tier user (ORG_ADMIN/SUPER_ADMIN,
    via the existing "ADMIN" role action) or the connection's own creator
    may edit its config, change its sharing/grants, or remove it. Real gap
    found live: the plain Connections "EDIT" role action — held even by
    DATA_ENGINEER, the default non-admin role (see QQ1) — was the ONLY
    gate on these routes, so any user who could reach a connection id at
    all (anyone in the org with VIEW, since list/get aren't owner-scoped
    either) could rename/reconfigure a colleague's shared AI connection or
    reassign who else it's shared with, as long as the payload didn't
    touch a secret field (the one thing _require_secret_permission above
    already protected). Reported live: "when Admin created connection and
    shared to user then it should not be editable, only user can view the
    connection and use it.\""""
    is_owner = conn.owner_id is not None and conn.owner_id == user.id
    if not (is_owner or _has_action(user, "ADMIN")):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You don't own this AI connection and don't have admin access — you can view and use it "
            "(if granted), but only its owner or an organization admin can edit, share, or remove it.",
        )


def _require_ai_connection_manage(user: User, conn: Connection) -> None:
    """Stricter guard for the routes that actually mutate/remove an AI
    Provider connection's own config (edit/disable/delete/clone) — as
    opposed to managing who else can use it (see _require_ai_owner_or_admin
    above, used for the grant-management routes, where a seller-managed
    connection is a legitimate target). A seller-managed connection
    (organization_id IS NULL) is never editable/disableable/deletable/
    cloneable by ANY customer-org actor, not even an org admin — it can
    only be used. In practice `_get_or_404` already 404s a seller-managed
    id for every caller except SUPER_ADMIN (get_connection()'s org filter
    excludes NULL-org rows), so this is explicit defense-in-depth, not the
    only thing stopping it."""
    if conn.category != ConnectionCategory.AI_PROVIDER:
        return
    if conn.organization_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Seller-managed AI connections can only be used — they can't be edited, disabled, deleted, or cloned by a customer organization.",
        )
    _require_ai_owner_or_admin(user, conn)


# ── KPIs / type catalogue ─────────────────────────────────────────
# NOTE: literal-path routes must stay registered before /{connection_id}
# routes below — Starlette matches by position, not type, so /stats or
# /types would otherwise be captured as a connection_id and 422.

@router.get("/stats", response_model=ConnectionStatsOut)
async def stats(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
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
    db: AsyncSession = Depends(get_tenant_db),
):
    conns, total, name_map = await svc.list_connections(
        db, organization_id=_effective_org_id(user), search=q, category=category,
        connection_type=connection_type, status_filter=status_filter, environment=environment,
        page=page, page_size=page_size, sort_by=sort_by, sort_dir=sort_dir,
    )
    items = [svc.to_out(c, name_map.get(c.owner_id)) for c in conns]
    return ConnectionListOut(items=items, total=total, page=page, page_size=page_size)


# ── AI connection picker (AIC-P8) ──────────────────────────────────
# Real gap found live: the plain list route above (and connectionApi.list()
# on the frontend) filters organization_id == caller's org, so it can
# NEVER surface a seller-managed (NULL-org) connection — the exact same
# shape of bug AIC-P7 already fixed for the FETCH-by-id path. Every AI
# connection picker (Document Intelligence's AI Extraction stage, the
# Documents/Review Queue "Run AI Extraction" control, the SQL Workspace AI
# Assistant panel) must use this route instead, so a connection a user was
# actually granted access to (AIC-P5) is one they can actually select.

@router.get("/ai-usable", response_model=UsableAiConnectionsOut)
async def list_usable_ai_connections(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    connections = await ai_connection_access_service.list_usable_connections(db, organization_id=org_id, acting_user=user)
    return UsableAiConnectionsOut(items=[
        UsableAiConnectionOut(
            id=c.id, name=c.name or f"{c.connection_type.value} connection #{c.id}",
            connection_type=c.connection_type.value, status=c.status.value,
            is_seller_managed=c.organization_id is None,
        )
        for c in connections
    ])


# ── AI Connection access management (AIC-P5) ──────────────────────
# Org-admin self-service: who in my organization can see/use which AI
# connection, and (for connections my org owns) whether it's public to
# everyone in my org. See ai_connection_permission_service.py. These
# literal-path routes must stay registered before /{connection_id}
# below — same reasoning as /stats and /types above.

@router.get("/ai-access", response_model=AiAccessListOut)
async def list_ai_access(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    items = await ai_access_svc.list_access(db, organization_id=org_id)
    return AiAccessListOut(items=items)


@router.get("/ai-access/grantees", response_model=GranteeOptionsOut)
async def list_ai_access_grantees(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    options = await ai_access_svc.grantee_options(db, organization_id=org_id)
    return GranteeOptionsOut(**options)


# ── Default AI connection selection (AIC-P6) ──────────────────────
# Same literal-path-before-/{connection_id} requirement as /ai-access above.

@router.get("/ai-defaults", response_model=AiDefaultsOut)
async def get_ai_defaults(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    defaults = await ai_default_svc.get_defaults(db, organization_id=org_id, acting_user=user)
    return AiDefaultsOut(**defaults)


@router.put("/ai-defaults/mine", response_model=AiDefaultsOut)
async def set_my_ai_default(body: SetDefaultRequest, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    try:
        await ai_default_svc.set_user_default(db, organization_id=org_id, user_id=user.id, connection_id=body.connection_id, created_by=user.id)
    except ai_default_svc.AiDefaultError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    defaults = await ai_default_svc.get_defaults(db, organization_id=org_id, acting_user=user)
    return AiDefaultsOut(**defaults)


@router.delete("/ai-defaults/mine", response_model=AiDefaultsOut)
async def clear_my_ai_default(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    await ai_default_svc.clear_default(db, organization_id=org_id, user_id=user.id)
    defaults = await ai_default_svc.get_defaults(db, organization_id=org_id, acting_user=user)
    return AiDefaultsOut(**defaults)


@router.put("/ai-defaults/org", response_model=AiDefaultsOut)
async def set_org_ai_default(body: SetDefaultRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    try:
        await ai_default_svc.set_org_default(db, organization_id=org_id, connection_id=body.connection_id, created_by=user.id)
    except ai_default_svc.AiDefaultError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    defaults = await ai_default_svc.get_defaults(db, organization_id=org_id, acting_user=user)
    return AiDefaultsOut(**defaults)


@router.delete("/ai-defaults/org", response_model=AiDefaultsOut)
async def clear_org_ai_default(user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    await ai_default_svc.clear_default(db, organization_id=org_id, user_id=ORG_DEFAULT_USER_ID)
    defaults = await ai_default_svc.get_defaults(db, organization_id=org_id, acting_user=user)
    return AiDefaultsOut(**defaults)


@router.put("/{connection_id}/ai-access/public", response_model=AiAccessListOut)
async def set_ai_access_public(connection_id: int, body: SetPublicRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    conn = await db.get(Connection, connection_id)
    if conn is not None:
        _require_ai_owner_or_admin(user, conn)
    try:
        await ai_access_svc.set_public(db, connection_id=connection_id, organization_id=org_id, is_public=body.is_public)
    except ai_access_svc.AiAccessError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    items = await ai_access_svc.list_access(db, organization_id=org_id)
    return AiAccessListOut(items=items)


@router.post("/{connection_id}/ai-access/grants", response_model=GrantOut, status_code=status.HTTP_201_CREATED)
async def create_ai_access_grant(connection_id: int, body: GrantRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    conn = await db.get(Connection, connection_id)
    if conn is not None:
        _require_ai_owner_or_admin(user, conn)
    try:
        grant = await ai_access_svc.upsert_grant(
            db, connection_id=connection_id, organization_id=org_id, grantee_type=body.grantee_type,
            grantee_id=body.grantee_id, can_view=body.can_view, can_use=body.can_use, granted_by=user.id,
        )
    except ai_access_svc.AiAccessError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return GrantOut(**grant)


@router.delete("/{connection_id}/ai-access/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ai_access_grant(connection_id: int, grant_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _require_org(user)
    conn = await db.get(Connection, connection_id)
    if conn is not None:
        _require_ai_owner_or_admin(user, conn)
    try:
        await ai_access_svc.remove_grant(db, connection_id=connection_id, organization_id=org_id, grant_id=grant_id)
    except ai_access_svc.AiAccessError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/{connection_id}", response_model=ConnectionOut)
async def get_connection(connection_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    conn = await _get_or_404(db, connection_id, user)
    name_map = await svc.owner_name_map(db, {conn.owner_id} if conn.owner_id else set())
    return svc.to_out(conn, name_map.get(conn.owner_id))


# ── Create / update / delete ──────────────────────────────────────

@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: ConnectionCreateRequest,
    user: User = Depends(_create),
    db: AsyncSession = Depends(get_tenant_db),
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
    db: AsyncSession = Depends(get_tenant_db),
):
    conn = await _get_or_404(db, connection_id, user)
    _require_ai_connection_manage(user, conn)
    if body.config:
        _require_secret_permission(user, conn, body.config)
    conn = await svc.update_connection(db, conn, name=body.name, environment=body.environment, raw_config=body.config)
    name_map = await svc.owner_name_map(db, {conn.owner_id} if conn.owner_id else set())
    return svc.to_out(conn, name_map.get(conn.owner_id))


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(connection_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    conn = await _get_or_404(db, connection_id, user)
    _require_ai_connection_manage(user, conn)
    await svc.delete_connection(db, conn)


# ── Actions ───────────────────────────────────────────────────────

@router.post("/{connection_id}/test", response_model=TestConnectionResult)
async def test_connection(connection_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    conn = await _get_or_404(db, connection_id, user)
    result = await svc.test_connection(db, conn)
    return TestConnectionResult(**result)


@router.post("/{connection_id}/disable", response_model=ConnectionOut)
async def disable_connection(connection_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    conn = await _get_or_404(db, connection_id, user)
    _require_ai_connection_manage(user, conn)
    conn = await svc.toggle_disabled(db, conn)
    name_map = await svc.owner_name_map(db, {conn.owner_id} if conn.owner_id else set())
    return svc.to_out(conn, name_map.get(conn.owner_id))


@router.post("/{connection_id}/clone", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
async def clone_connection(connection_id: int, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    conn = await _get_or_404(db, connection_id, user)
    _require_ai_connection_manage(user, conn)
    clone = await svc.clone_connection(db, conn, user.id)
    return svc.to_out(clone, user.full_name)


# ── Schema/object browsing — Source Dataset node (Phase 2) ────────
# All hit the live source (unlike the routes above, which only read
# stored Connection metadata), so gated the same as Test Connection.

@router.get("/{connection_id}/schemas", response_model=SchemaListOut)
async def get_schemas(connection_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    conn = await _get_or_404(db, connection_id, user)
    try:
        schemas = await svc.list_schemas(conn)
    except ConnectorUnsupportedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    except ConnectorValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    return SchemaListOut(schemas=schemas)


@router.get("/{connection_id}/objects", response_model=ObjectListOut)
async def get_objects(
    connection_id: int,
    schema: str | None = Query(None),
    q: str | None = Query(None, description="Search object names"),
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_tenant_db),
):
    conn = await _get_or_404(db, connection_id, user)
    try:
        objects = await svc.list_objects(conn, schema, q)
    except ConnectorUnsupportedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    except ConnectorValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    return ObjectListOut(items=[ObjectOut(name=o.name, schema_name=o.schema, object_type=o.object_type) for o in objects])


@router.get("/{connection_id}/objects/{object_name}/columns", response_model=ColumnListOut)
async def get_columns(
    connection_id: int,
    object_name: str,
    schema: str | None = Query(None),
    include_row_count: bool = Query(False, description="Runs a COUNT(*) — off by default, can be slow on large tables"),
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_tenant_db),
):
    conn = await _get_or_404(db, connection_id, user)
    try:
        columns, row_count = await svc.get_columns(conn, object_name, schema, include_row_count)
    except ConnectorUnsupportedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    except ConnectorValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    return ColumnListOut(
        columns=[ColumnOut(**vars(c)) for c in columns],
        row_count=row_count,
    )


@router.post("/{connection_id}/preview", response_model=PreviewOut)
async def preview_connection_data(
    connection_id: int,
    body: PreviewRequest,
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_tenant_db),
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
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    return PreviewOut(columns=result.columns, rows=result.rows, row_count=result.row_count, truncated=result.truncated)


@router.post("/{connection_id}/preview-api", response_model=PreviewOut)
async def preview_api_connection_data(
    connection_id: int,
    body: ApiPreviewRequest,
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_tenant_db),
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
