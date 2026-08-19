"""Seller Portal -> Customers router — /api/seller/customers/* (Phase 2)."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.seller.customers import (
    CreateCustomerOut,
    CreateCustomerRequest,
    CustomerAuditOut,
    CustomerDetailOut,
    CustomerListOut,
    CustomerStats,
    ImpersonateOut,
    UpdateCustomerRequest,
)
from app.services.seller import customer_service as svc

router = APIRouter(prefix="/seller/customers", tags=["seller-customers"])

_view = require_seller_action("VIEW", "Customers")
_create = require_seller_action("CREATE", "Customers")
_edit = require_seller_action("EDIT", "Customers")


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _get_org_or_404(db: AsyncSession, customer_id: int) -> Organization:
    org = await db.get(Organization, customer_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    return org


@router.get("/stats", response_model=CustomerStats)
async def get_stats(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    return await svc.get_stats(db)


@router.get("", response_model=CustomerListOut)
async def list_customers(
    q: str = "", status_filter: str | None = None, plan: str | None = None,
    page: int = 1, page_size: int = 20,
    user: User = Depends(_view), db: AsyncSession = Depends(get_db),
):
    items, total = await svc.list_customers(db, search=q, status_filter=status_filter, plan_filter=plan, page=page, page_size=page_size)
    return CustomerListOut(items=items, total=total, page=page, page_size=page_size)


@router.post("", response_model=CreateCustomerOut, status_code=status.HTTP_201_CREATED)
async def create_customer(body: CreateCustomerRequest, request: Request, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
    try:
        org, temp_password = await svc.create_customer(db, body=body, actor=user, ip_address=_client_ip(request))
    except svc.CustomerServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CreateCustomerOut(id=org.id, name=org.name, slug=org.slug, temp_password=temp_password)


@router.get("/{customer_id}", response_model=CustomerDetailOut)
async def get_customer(customer_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    detail = await svc.get_customer_detail(db, customer_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    return detail


@router.put("/{customer_id}", response_model=CustomerDetailOut)
async def update_customer(customer_id: int, body: UpdateCustomerRequest, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    org = await _get_org_or_404(db, customer_id)
    await svc.update_customer(db, org, body, actor=user, ip_address=_client_ip(request))
    return await svc.get_customer_detail(db, customer_id)


@router.post("/{customer_id}/activate", response_model=CustomerDetailOut)
async def activate_customer(customer_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    org = await _get_org_or_404(db, customer_id)
    await svc.set_status(db, org, active=True, actor=user, ip_address=_client_ip(request))
    return await svc.get_customer_detail(db, customer_id)


@router.post("/{customer_id}/suspend", response_model=CustomerDetailOut)
async def suspend_customer(customer_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    org = await _get_org_or_404(db, customer_id)
    await svc.set_status(db, org, active=False, actor=user, ip_address=_client_ip(request))
    return await svc.get_customer_detail(db, customer_id)


@router.post("/{customer_id}/impersonate", response_model=ImpersonateOut)
async def impersonate_customer(customer_id: int, request: Request, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    org = await _get_org_or_404(db, customer_id)
    try:
        result = await svc.impersonate(db, org, actor=user, ip_address=_client_ip(request), user_agent=request.headers.get("user-agent"))
    except svc.CustomerServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return result


@router.get("/{customer_id}/audit", response_model=CustomerAuditOut)
async def get_customer_audit(customer_id: int, page: int = 1, page_size: int = 20, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    await _get_org_or_404(db, customer_id)
    items, total = await svc.get_customer_audit(db, customer_id, page=page, page_size=page_size)
    return CustomerAuditOut(items=items, total=total, page=page, page_size=page_size)
