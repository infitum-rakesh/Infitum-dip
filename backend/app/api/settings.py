"""Settings router — /api/settings

Administration -> Settings. Organization-level configuration. Same
role-tier gating convention as every other Administration router in this
codebase (Users/Roles/Licenses/Audit Logs): a single ADMIN-tier dependency
covers the whole router, and the frontend's SETTINGS_VIEW/EDIT/SECURITY/
AI/NOTIFICATIONS permission constants distinguish read from write and
section from section for the UI (which tab shows, which Save button is
enabled) the same way USER_*/ROLE_* do — action tier + role, no bespoke
per-field permission engine on either side.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.settings import OrganizationSettingsOut, UpdateSettingsRequest
from app.services import settings_service as svc

router = APIRouter(prefix="/settings", tags=["settings"])

_admin = require_action("ADMIN", module=None)


async def _get_org_or_404(db: AsyncSession, user: User) -> Organization:
    if not user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No organization is associated with this account.")
    result = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found.")
    return org


@router.get("", response_model=OrganizationSettingsOut)
async def get_settings(user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    org = await _get_org_or_404(db, user)
    row = await svc.get_or_create(db, org.id)
    return await svc.to_out(db, org, row)


@router.put("", response_model=OrganizationSettingsOut)
async def update_settings(
    body: UpdateSettingsRequest,
    request: Request,
    user: User = Depends(_admin),
    db: AsyncSession = Depends(get_db),
):
    org = await _get_org_or_404(db, user)
    row = await svc.get_or_create(db, org.id)
    row = await svc.update_settings(
        db, org, row, body, actor_id=user.id,
        ip_address=request.client.host if request.client else None,
    )
    return await svc.to_out(db, org, row)
