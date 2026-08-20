"""Admin-configurable SSO settings — /api/admin/sso/... (SS4 backend half).
Same ADMIN-tier gate as api/sessions.py; scoped to the caller's own
organization (Settings -> Single Sign-On tab edits only your org's config).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.sso import SsoConfigurationOut, UpdateSsoConfigurationRequest
from app.services import sso_service
from app.services.auth_service import get_org_by_id

router = APIRouter(prefix="/admin/sso", tags=["sso-admin"])

_admin = require_action("ADMIN", module=None)


def _require_org(user: User) -> int:
    if not user.organization_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Platform admins have no organization-scoped SSO config to manage here.")
    return user.organization_id


@router.get("", response_model=SsoConfigurationOut)
async def get_sso_config(user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    org_id = _require_org(user)
    org = await get_org_by_id(db, org_id)
    cfg = await sso_service.get_or_create_config(db, org_id)
    await db.commit()
    return sso_service.to_out(cfg, org.slug)


@router.put("", response_model=SsoConfigurationOut)
async def update_sso_config(body: UpdateSsoConfigurationRequest, user: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    # Full IdP-fields-present validation happens at actual login time
    # (saml_service.build_settings raises SamlError there) rather than here
    # — an admin filling the form in one field at a time (e.g. flipping
    # "enabled" on before pasting the cert) shouldn't be blocked from saving.
    org_id = _require_org(user)
    org = await get_org_by_id(db, org_id)
    cfg = await sso_service.get_or_create_config(db, org_id)
    cfg = await sso_service.update_config(db, cfg, body, user)
    return sso_service.to_out(cfg, org.slug)
