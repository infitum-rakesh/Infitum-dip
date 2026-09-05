"""Seller Portal -> Companies router — /api/seller/companies. Manages
the Company entities (models/company.py) a Seller issues invoices under —
see services/seller/company_service.py."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.user import User
from app.schemas.seller.companies import (
    CompanyDetailOut,
    CompanyListOut,
    CompanyOut,
    CreateCompanyRequest,
    UpdateCompanyRequest,
)
from app.services.seller import company_service as svc

router = APIRouter(prefix="/seller/companies", tags=["seller-companies"])

_view = require_seller_action("VIEW", "Settings")
_edit = require_seller_action("EDIT", "Settings")


async def _get_or_404(db: AsyncSession, company_id: int):
    company = await svc.get_company(db, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company


@router.get("", response_model=CompanyListOut)
async def list_companies(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    companies = await svc.list_companies(db)
    return CompanyListOut(items=[svc.to_out(c) for c in companies])


@router.get("/{company_id}", response_model=CompanyDetailOut)
async def get_company(company_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    company = await _get_or_404(db, company_id)
    return svc.to_detail_out(company)


@router.post("", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
async def create_company(body: CreateCompanyRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    try:
        company = await svc.create_company(db, body=body, actor=user)
    except svc.CompanyServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return svc.to_out(company)


@router.put("/{company_id}", response_model=CompanyOut)
async def update_company(company_id: int, body: UpdateCompanyRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    company = await _get_or_404(db, company_id)
    company = await svc.update_company(db, company, body=body, actor=user)
    return svc.to_out(company)


@router.post("/{company_id}/activate", response_model=CompanyOut)
async def activate_company(company_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    company = await _get_or_404(db, company_id)
    company = await svc.set_active(db, company, active=True, actor=user)
    return svc.to_out(company)


@router.post("/{company_id}/deactivate", response_model=CompanyOut)
async def deactivate_company(company_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    company = await _get_or_404(db, company_id)
    try:
        company = await svc.set_active(db, company, active=False, actor=user)
    except svc.CompanyServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return svc.to_out(company)


@router.post("/{company_id}/set-default", response_model=CompanyOut)
async def set_default_company(company_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    company = await _get_or_404(db, company_id)
    try:
        company = await svc.set_default(db, company, actor=user)
    except svc.CompanyServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return svc.to_out(company)
