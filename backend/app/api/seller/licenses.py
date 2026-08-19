"""Seller Portal -> License Management router — /api/seller/licenses/* (Phase 5)."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.seller.licenses import (
    ChangeLicenseTypeRequest,
    CreateLicenseRequest,
    ExtendLicenseRequest,
    LicenseListOut,
    LicenseOut,
    LicenseStats,
)
from app.services.seller import license_service as svc

router = APIRouter(prefix="/seller/licenses", tags=["seller-licenses"])

_view = require_seller_action("VIEW", "Licenses")
_create = require_seller_action("CREATE", "Licenses")
_edit = require_seller_action("EDIT", "Licenses")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _get_or_404(db: AsyncSession, license_id: int):
    row = await svc.get_license(db, license_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "License not found")
    return row


@router.get("/stats", response_model=LicenseStats)
async def get_stats(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await svc.get_stats(db)


@router.get("", response_model=LicenseListOut)
async def list_licenses(
    customer_id: int | None = None, status_filter: str | None = None, module: str | None = None,
    page: int = 1, page_size: int = 20,
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    items, total = await svc.list_licenses(db, customer_id=customer_id, status_filter=status_filter, module_filter=module, page=page, page_size=page_size)
    return LicenseListOut(items=items, total=total, page=page, page_size=page_size)


@router.post("", response_model=LicenseOut, status_code=status.HTTP_201_CREATED)
async def create_license(body: CreateLicenseRequest, request: Request, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
    try:
        return await svc.create_license(db, body=body, actor=user, ip_address=_client_ip(request))
    except svc.LicenseServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/{license_id}", response_model=LicenseOut)
async def get_license(license_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await _get_or_404(db, license_id)


@router.post("/{license_id}/activate", response_model=LicenseOut)
async def activate_license(license_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, license_id)
    try:
        return await svc.activate_license(db, row, actor=user, ip_address=_client_ip(request))
    except svc.LicenseServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/{license_id}/suspend", response_model=LicenseOut)
async def suspend_license(license_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, license_id)
    return await svc.suspend_license(db, row, actor=user, ip_address=_client_ip(request))


@router.post("/{license_id}/revoke", response_model=LicenseOut)
async def revoke_license(license_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, license_id)
    return await svc.revoke_license(db, row, actor=user, ip_address=_client_ip(request))


@router.post("/{license_id}/extend", response_model=LicenseOut)
async def extend_license(license_id: int, body: ExtendLicenseRequest, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, license_id)
    return await svc.extend_license(db, row, days=body.days, actor=user, ip_address=_client_ip(request))


@router.post("/{license_id}/renew", response_model=LicenseOut)
async def renew_license(license_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, license_id)
    return await svc.renew_license(db, row, actor=user, ip_address=_client_ip(request))


@router.post("/{license_id}/change-type", response_model=LicenseOut)
async def change_license_type(license_id: int, body: ChangeLicenseTypeRequest, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, license_id)
    return await svc.change_license_type(db, row, new_type=body.license_type, actor=user, ip_address=_client_ip(request))
