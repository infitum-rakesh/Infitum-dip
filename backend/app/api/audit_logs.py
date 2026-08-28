"""Audit Logs router — /api/audit-logs/...

Administration -> Audit Logs: a merged, read-only view over every audit
trail this codebase already keeps (see services/audit_log_service.py for
the full source map). Same ADMIN-action-tier gate as the sibling admin
pages (Users/Roles/Licenses) — "Respect permissions" for export means the
export route requires the exact same gate as viewing, nothing looser.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.schemas.audit_logs import AuditLogDetailOut, AuditLogListOut
from app.services import audit_log_service as svc

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])

_admin = require_action("ADMIN", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _parse_date(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid date: {v}")


@router.get("", response_model=AuditLogListOut)
async def list_audit_logs(
    q: str = Query("", description="Free-text search across user/action/module/resource/details"),
    user: str | None = Query(None),
    module: str | None = Query(None),
    action: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    resource: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    current_user: User = Depends(_admin),
    db: AsyncSession = Depends(get_tenant_db),
):
    items, total = await svc.list_entries(
        db, organization_id=_effective_org_id(current_user), user=user, module=module, action=action,
        status=status_filter, resource=resource, date_from=_parse_date(date_from), date_to=_parse_date(date_to),
        search=q, page=page, page_size=page_size,
    )
    return AuditLogListOut(items=items, total=total, page=page, page_size=page_size, modules=svc.MODULES)


@router.get("/export")
async def export_audit_logs(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    q: str = Query(""),
    user: str | None = Query(None),
    module: str | None = Query(None),
    action: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    resource: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    current_user: User = Depends(_admin),
    db: AsyncSession = Depends(get_tenant_db),
):
    content, media_type, filename = await svc.export_entries(
        db, organization_id=_effective_org_id(current_user), fmt=format, user=user, module=module, action=action,
        status=status_filter, resource=resource, date_from=_parse_date(date_from), date_to=_parse_date(date_to), search=q,
    )
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{entry_id}", response_model=AuditLogDetailOut)
async def get_audit_log(entry_id: str, current_user: User = Depends(_admin), db: AsyncSession = Depends(get_tenant_db)):
    entry = await svc.get_entry(db, _effective_org_id(current_user), entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audit log entry not found")
    return AuditLogDetailOut(**entry)
