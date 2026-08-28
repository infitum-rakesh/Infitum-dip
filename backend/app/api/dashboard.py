"""Dashboard router — /api/dashboard. Real aggregation only; see
services/dashboard_service.py."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.schemas.dashboard import DashboardOut
from app.services import dashboard_service as svc

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_view = require_action("VIEW", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


@router.get("", response_model=DashboardOut)
async def get_dashboard(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    data = await svc.get_dashboard(db, _effective_org_id(user))
    return DashboardOut(**data)
