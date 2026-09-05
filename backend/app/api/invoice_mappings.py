"""Invoice ERP field mapping router — /api/invoice-mappings/... (INV9).

Canonical ApInvoice field -> ERP request field mapping, versioned. See
services/invoice/invoice_erp_mapping_service.py for the "never mutate a
published version in place, publish new" convention this follows.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.schemas.invoices import ApInvoiceErpMappingOut, ApInvoiceErpMappingPublishRequest
from app.services.invoice import invoice_erp_mapping_service as svc

router = APIRouter(prefix="/invoice-mappings", tags=["invoices"])

INVOICE_MODULE = "INVOICE_INTELLIGENCE"


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


@router.get("", response_model=list[ApInvoiceErpMappingOut])
async def get_mapping(
    erp_connection_id: int = Query(...),
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Returns the current mapping — seeding the real Oracle-field default
    set on first access (idempotent) so there's always something concrete
    to view/edit rather than an empty screen."""
    rows = await svc.seed_default_mapping(db, _effective_org_id(user), erp_connection_id)
    return [ApInvoiceErpMappingOut.model_validate(r) for r in rows]


@router.put("", response_model=list[ApInvoiceErpMappingOut], status_code=status.HTTP_201_CREATED)
async def publish_mapping(
    body: ApInvoiceErpMappingPublishRequest,
    user: User = Depends(require_action("EDIT", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    try:
        rows = await svc.publish_mapping(
            db, _effective_org_id(user), body.erp_connection_id, [r.model_dump() for r in body.rows], created_by=user.id,
        )
    except svc.InvoiceErpMappingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return [ApInvoiceErpMappingOut.model_validate(r) for r in rows]
