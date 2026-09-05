"""Seller Portal -> Purchased Modules router — /api/seller/modules/* (Phase 4)."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.seller.modules import (
    CustomerModulesOut,
    ModuleCatalogOut,
    ModulePriceListOut,
    SetModuleOut,
    SetModulePriceOut,
    SetModulePriceRequest,
    SetModuleRequest,
)
from app.services.seller import module_service as svc

router = APIRouter(prefix="/seller/modules", tags=["seller-modules"])

_view = require_seller_action("VIEW", "Modules")
_edit = require_seller_action("EDIT", "Modules")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("", response_model=ModuleCatalogOut)
async def get_catalog(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    items = await svc.list_catalog(db)
    return ModuleCatalogOut(items=items)


@router.get("/customers/{customer_id}", response_model=CustomerModulesOut)
async def get_customer_modules(customer_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    try:
        items = await svc.get_customer_modules(db, customer_id)
    except svc.ModuleServiceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return CustomerModulesOut(organization_id=customer_id, items=items)


@router.put("/customers/{customer_id}/{module}", response_model=SetModuleOut)
async def set_module(customer_id: int, module: str, body: SetModuleRequest, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    try:
        return await svc.set_module(db, organization_id=customer_id, module=module, body=body, actor=user, ip_address=_client_ip(request))
    except svc.ModuleServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# ── Module price catalog (Settings -> Module Prices) ────────────────
# Registered after the more specific /customers/... routes but as its own
# literal path segment, so no ordering conflict with {module} above.

@router.get("/prices", response_model=ModulePriceListOut)
async def get_module_prices(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    items = await svc.list_module_prices(db)
    return ModulePriceListOut(items=items)


@router.put("/prices/{module}", response_model=SetModulePriceOut)
async def set_module_price(module: str, body: SetModulePriceRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    try:
        return await svc.set_module_price(db, module=module, price=body.price, currency=body.currency, actor=user)
    except svc.ModuleServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
