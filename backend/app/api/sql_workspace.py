"""SQL Workspace router — /api/sql-workspace/... (Phase 9).

Real SQL execution against a live connection — see
connectors/database/base.py's run_sql() for how it's deliberately
broader than the Preview surfaces elsewhere (reads AND writes, one
statement, real commit/rollback). Nothing here is mocked.
"""
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_organization, get_current_user
from app.database.tenant_routing import get_tenant_db
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.schemas.sql_workspace import (
    AiGenerateSqlRequest,
    AiGenerateSqlResponse,
    CancelRunOut,
    CancelRunRequest,
    ExplainRequest,
    ExplainResponse,
    ExportCancelOut,
    ExportCreateRequest,
    ExportJobListOut,
    ExportJobOut,
    FavoriteToggleOut,
    HistoryEntryOut,
    HistoryListOut,
    QueryCreateRequest,
    QueryListOut,
    QueryOut,
    QueryUpdateRequest,
    RunSqlRequest,
    RunSqlResponse,
    SqlPermissionsOut,
)
from app.services import role_service, sql_ai_service, sql_export_service, sql_workspace_service as svc

_ELEVATED_ROLES = (UserRole.SUPER_ADMIN, UserRole.ORG_ADMIN)
_SQL_WORKSPACE_MODULE = "SQL_WORKSPACE"

router = APIRouter(prefix="/sql-workspace", tags=["sql-workspace"])


def require_sql_action(action: str):
    """Dependency factory — 403s unless the caller's REAL, granular SQL
    Workspace permission set (their custom Role's "SQL Workspace" grants,
    or a sensible fixed-tier default when no custom role is assigned —
    see role_service.effective_sql_permissions) includes `action`, AND the
    org is licensed for the SQL_WORKSPACE module. Use per-route:
    Depends(require_sql_action("EXECUTE")). Distinct from core.deps.
    require_action() — that one only knows the platform's generic 6-action
    vocabulary shared across every module; this one speaks SQL Workspace's
    own granular actions (VIEW/EXECUTE/WRITE/DELETE/EXPORT/AI_ASSISTANT/
    DDL/DML/PRODUCTION_ACCESS — see models/role.py)."""
    async def _checker(
        user: User = Depends(get_current_user),
        org: Organization | None = Depends(get_current_organization),
        db: AsyncSession = Depends(get_tenant_db),
    ) -> User:
        permissions = await role_service.effective_sql_permissions(db, user)
        if action not in permissions:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Your SQL Workspace permissions don't include '{action}'.")
        if org is not None and _SQL_WORKSPACE_MODULE not in (org.licensed_modules or []):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "SQL Workspace is not licensed for your organization.")
        return user
    return _checker


_view = require_sql_action("VIEW")
_write = require_sql_action("WRITE")     # save/edit a query definition
_delete = require_sql_action("DELETE")   # delete a saved query
_execute = require_sql_action("EXECUTE")  # base gate for /run + /explain — DML/DDL/PRODUCTION_ACCESS are checked per-statement inside svc.run_sql
_ai = require_sql_action("AI_ASSISTANT")
_export = require_sql_action("EXPORT")


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _to_out(db: AsyncSession, row, *, is_favorite: bool = False) -> dict:
    names = await svc.user_name_map(db, {i for i in (row.created_by, row.updated_by) if i})
    return {
        **{c.name: getattr(row, c.name) for c in row.__table__.columns},
        "created_by_name": names.get(row.created_by),
        "updated_by_name": names.get(row.updated_by),
        "is_favorite": is_favorite,
    }


async def _get_query_or_404(db: AsyncSession, query_id: int, user: User):
    row = await svc.get_query(db, query_id, _effective_org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Saved query not found")
    return row


def _visible(row, user: User) -> bool:
    """Can this user see the query at all? Their own, any shared query in
    the org, or (elevated roles) anything. NOT the same as being allowed
    to edit/delete it — see svc.ensure_owner_or_elevated for that."""
    return row.created_by == user.id or row.is_shared or user.role in _ELEVATED_ROLES


async def _get_visible_query_or_404(db: AsyncSession, query_id: int, user: User):
    row = await _get_query_or_404(db, query_id, user)
    if not _visible(row, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Saved query not found")
    return row


# ── Saved queries CRUD ───────────────────────────────────────────

_VALID_SCOPES = {"mine", "shared", "favorites"}


@router.get("/queries", response_model=QueryListOut)
async def list_queries(scope: str = "mine", user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    if scope not in _VALID_SCOPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"scope must be one of {sorted(_VALID_SCOPES)}.")
    rows, total = await svc.list_queries(db, _effective_org_id(user), scope=scope, user_id=user.id)
    favorite_ids = await svc.favorited_query_ids(db, user.id, {r.id for r in rows})
    items = [await _to_out(db, r, is_favorite=r.id in favorite_ids) for r in rows]
    return QueryListOut(items=items, total=total)


@router.post("/queries", response_model=QueryOut, status_code=status.HTTP_201_CREATED)
async def create_query(body: QueryCreateRequest, user: User = Depends(_write), db: AsyncSession = Depends(get_tenant_db)):
    try:
        row = await svc.create_query(
            db, organization_id=user.organization_id, name=body.name, connection_id=body.connection_id,
            schema_name=body.schema_name, sql_text=body.sql_text, is_shared=body.is_shared, created_by=user.id,
        )
    except svc.SqlWorkspaceServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, row)


@router.get("/queries/{query_id}", response_model=QueryOut)
async def get_query(query_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_visible_query_or_404(db, query_id, user)
    favorite_ids = await svc.favorited_query_ids(db, user.id, {row.id})
    return await _to_out(db, row, is_favorite=row.id in favorite_ids)


@router.put("/queries/{query_id}", response_model=QueryOut)
async def update_query(query_id: int, body: QueryUpdateRequest, user: User = Depends(_write), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_visible_query_or_404(db, query_id, user)
    try:
        svc.ensure_owner_or_elevated(row, user.id, user.role in _ELEVATED_ROLES)
    except svc.SqlQueryNotOwned as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    row = await svc.update_query(db, row, name=body.name, sql_text=body.sql_text, is_shared=body.is_shared, updated_by=user.id)
    favorite_ids = await svc.favorited_query_ids(db, user.id, {row.id})
    return await _to_out(db, row, is_favorite=row.id in favorite_ids)


@router.delete("/queries/{query_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_query(query_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_visible_query_or_404(db, query_id, user)
    try:
        svc.ensure_owner_or_elevated(row, user.id, user.role in _ELEVATED_ROLES)
    except svc.SqlQueryNotOwned as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    await svc.delete_query(db, row)


@router.post("/queries/{query_id}/favorite", response_model=FavoriteToggleOut)
async def toggle_favorite(query_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """Anyone who can VIEW a query (their own, or a shared one) can
    favorite/unfavorite it for themselves — favoriting doesn't require
    WRITE, it's a personal bookmark, not a change to the query itself."""
    row = await _get_visible_query_or_404(db, query_id, user)
    is_favorite = await svc.toggle_favorite(db, row, user.id)
    return FavoriteToggleOut(is_favorite=is_favorite)


# ── Run ──────────────────────────────────────────────────────────

@router.post("/connections/{connection_id}/run", response_model=RunSqlResponse)
async def run_sql(connection_id: int, body: RunSqlRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    # Route-level EXECUTE gate above only covers "can this user run
    # anything at all" — the ACTUAL statement (READ vs a write vs DDL) and
    # the target connection's real environment are checked per-statement
    # inside svc.run_sql() against this same permission set, since only
    # the SQL text itself (not the route) reveals what it really needs.
    permissions = await role_service.effective_sql_permissions(db, user)
    start = time.monotonic()
    try:
        result = await svc.run_sql(
            db, organization_id=_effective_org_id(user), connection_id=connection_id, user_id=user.id,
            sql=body.sql, parameters=body.parameters, limit=body.limit, source=body.source,
            permissions=permissions, client_run_id=body.client_run_id, timeout_seconds=body.timeout_seconds,
        )
    except svc.SqlPermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    except svc.SqlWorkspaceServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    return RunSqlResponse(
        is_result_set=result.is_result_set, columns=result.columns, rows=result.rows,
        truncated=result.truncated, rowcount=result.rowcount, elapsed_ms=elapsed_ms,
    )


# ── Cancel ───────────────────────────────────────────────────────
# A separate, independent HTTP request from the still-in-flight /run —
# the browser can have both open at once. Gated the same as Run itself
# (cancelling a query you're not allowed to run in the first place isn't
# a meaningful action), and further restricted to the run's own owner
# (or an elevated role) inside svc.cancel_run().

@router.post("/connections/{connection_id}/cancel", response_model=CancelRunOut)
async def cancel_run(connection_id: int, body: CancelRunRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    try:
        await svc.cancel_run(
            client_run_id=body.client_run_id, organization_id=_effective_org_id(user),
            user_id=user.id, elevated=user.role in _ELEVATED_ROLES,
        )
    except svc.SqlRunNotCancellable as exc:
        return CancelRunOut(cancelled=False, message=str(exc))
    except svc.SqlWorkspaceServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CancelRunOut(cancelled=True, message="Cancellation requested.")


# ── Explain ──────────────────────────────────────────────────────

@router.post("/connections/{connection_id}/explain", response_model=ExplainResponse)
async def explain_sql(connection_id: int, body: ExplainRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    start = time.monotonic()
    try:
        result = await svc.explain_sql(
            db, organization_id=_effective_org_id(user), connection_id=connection_id,
            sql=body.sql, parameters=body.parameters,
        )
    except svc.SqlWorkspaceServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    return ExplainResponse(plan_text=result.plan_text, columns=result.columns, rows=result.rows, elapsed_ms=elapsed_ms)


# ── AI Assistant (NL → SQL) ─────────────────────────────────────────
# The AI never executes SQL itself — this only proposes it; running it is
# a separate, explicit user action against the existing /run route above,
# gated there on EXECUTE (+ DML/DDL/PRODUCTION_ACCESS per-statement).
# Gated here on its own AI_ASSISTANT grant — a role can have EXECUTE
# without AI_ASSISTANT (or vice versa), they're independent grants.

@router.post("/connections/{connection_id}/ai/generate-sql", response_model=AiGenerateSqlResponse)
async def ai_generate_sql(connection_id: int, body: AiGenerateSqlRequest, user: User = Depends(_ai), db: AsyncSession = Depends(get_tenant_db)):
    start = time.monotonic()
    try:
        result = await sql_ai_service.generate_sql(
            db, organization_id=_effective_org_id(user), connection_id=connection_id,
            ai_connection_id=body.ai_connection_id, question=body.question,
            schema_name=body.schema_name, current_sql=body.current_sql,
        )
    except sql_ai_service.SqlAiError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    return AiGenerateSqlResponse(**result, elapsed_ms=elapsed_ms)


# ── History ──────────────────────────────────────────────────────
# Every entry is a real past /run execution (see sql_workspace_service.
# _log_execution) — non-elevated roles only ever see their own history;
# SUPER_ADMIN/ORG_ADMIN see the whole org by default and can narrow to
# scope=mine. A non-elevated caller passing scope=all is silently
# treated as scope=mine server-side, never honored as a privilege
# escalation via query string.

@router.get("/history", response_model=HistoryListOut)
async def list_history(
    connection_id: int | None = None,
    run_status: str | None = Query(None, alias="status"),
    scope: str = "default",
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    elevated = user.role in _ELEVATED_ROLES
    own_only = (not elevated) or (scope == "mine")
    rows, total = await svc.list_history(
        db, organization_id=_effective_org_id(user), user_id=user.id,
        connection_id=connection_id, status_filter=run_status, own_only=own_only,
        page=page, page_size=page_size,
    )
    conn_ids = {r.connection_id for r in rows}
    user_ids = {r.user_id for r in rows if r.user_id}
    conn_names = await svc.connection_name_map(db, conn_ids)
    user_names = await svc.user_name_map(db, user_ids)
    items = [
        HistoryEntryOut(
            **{c.name: getattr(r, c.name) for c in r.__table__.columns},
            connection_name=conn_names.get(r.connection_id),
            user_name=user_names.get(r.user_id),
        )
        for r in rows
    ]
    return HistoryListOut(items=items, total=total)


# ── Background export ────────────────────────────────────────────
# Full-result export, deliberately separate from the browser-bounded
# Export CSV/JSON/Excel on the Result Grid (max 1000 rows — see
# frontend/exportUtils.js). Streams the query server-side via
# connector.open_sql_stream() (bounded memory, same primitive Phase 3's
# pipeline extraction uses) straight to a real file — see
# execution/sql_export_executor.py. Gated on EXPORT, same permission as
# the bounded grid export.

def _export_job_to_out(job, name_map: dict[int, str]) -> ExportJobOut:
    return ExportJobOut(
        id=job.id, connection_id=job.connection_id, format=job.format.value, status=job.status.value,
        row_count=job.row_count, file_size_bytes=job.file_size_bytes, error_message=job.error_message,
        created_by=job.created_by, created_by_name=name_map.get(job.created_by),
        started_at=job.started_at, completed_at=job.completed_at, created_at=job.created_at,
    )


async def _get_export_job_or_404(db: AsyncSession, job_id: int, user: User):
    job = await sql_export_service.get_export_job(db, job_id, _effective_org_id(user))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export job not found")
    return job


@router.post("/connections/{connection_id}/export", response_model=ExportJobOut, status_code=status.HTTP_201_CREATED)
async def create_export(connection_id: int, body: ExportCreateRequest, user: User = Depends(_export), db: AsyncSession = Depends(get_tenant_db)):
    try:
        job = await sql_export_service.create_export_job(
            db, organization_id=_effective_org_id(user), connection_id=connection_id, user_id=user.id,
            sql_text=body.sql, export_format=body.format,
        )
    except sql_export_service.SqlExportServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name_map = await sql_export_service.user_name_map(db, {user.id})
    return _export_job_to_out(job, name_map)


@router.get("/exports", response_model=ExportJobListOut)
async def list_exports(
    connection_id: int | None = None, page: int = 1, page_size: int = 20,
    user: User = Depends(_export), db: AsyncSession = Depends(get_tenant_db),
):
    elevated = user.role in _ELEVATED_ROLES
    rows, total = await sql_export_service.list_export_jobs(
        db, organization_id=_effective_org_id(user), user_id=user.id, own_only=not elevated,
        connection_id=connection_id, page=page, page_size=page_size,
    )
    name_map = await sql_export_service.user_name_map(db, {r.created_by for r in rows if r.created_by})
    return ExportJobListOut(items=[_export_job_to_out(r, name_map) for r in rows], total=total)


@router.get("/exports/{job_id}", response_model=ExportJobOut)
async def get_export(job_id: int, user: User = Depends(_export), db: AsyncSession = Depends(get_tenant_db)):
    job = await _get_export_job_or_404(db, job_id, user)
    name_map = await sql_export_service.user_name_map(db, {job.created_by} if job.created_by else set())
    return _export_job_to_out(job, name_map)


@router.post("/exports/{job_id}/cancel", response_model=ExportCancelOut)
async def cancel_export(job_id: int, user: User = Depends(_export), db: AsyncSession = Depends(get_tenant_db)):
    job = await _get_export_job_or_404(db, job_id, user)
    try:
        sql_export_service.ensure_owner_or_elevated(job, user.id, user.role in _ELEVATED_ROLES)
    except sql_export_service.SqlExportServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    cancelled = await sql_export_service.cancel_export_job(db, job)
    return ExportCancelOut(
        cancelled=cancelled,
        message="Cancellation requested." if cancelled else "This export already finished — nothing to cancel.",
    )


@router.get("/exports/{job_id}/download")
async def download_export(job_id: int, user: User = Depends(_export), db: AsyncSession = Depends(get_tenant_db)):
    job = await _get_export_job_or_404(db, job_id, user)
    try:
        sql_export_service.ensure_owner_or_elevated(job, user.id, user.role in _ELEVATED_ROLES)
    except sql_export_service.SqlExportServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    resolved = sql_export_service.resolve_download_path(job)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This export isn't downloadable — it hasn't succeeded, or the file is no longer on disk.")
    return FileResponse(resolved, filename=resolved.name)


# ── Effective permissions ────────────────────────────────────────
# The caller's own real, computed SQL Workspace permission set — lets the
# frontend hide/disable actions it already knows will 403 (Save, Delete,
# Export, the AI tab) instead of discovering that only after a failed
# request. Always safe to expose to a user: it's their own access, and
# every action it lists is independently re-checked server-side anyway —
# this endpoint informs the UI, it doesn't grant anything by itself.

@router.get("/permissions", response_model=SqlPermissionsOut)
async def get_my_permissions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_tenant_db)):
    permissions = await role_service.effective_sql_permissions(db, user)
    return SqlPermissionsOut(actions=sorted(permissions))
