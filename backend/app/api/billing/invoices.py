"""
GET /api/billing/invoices, /api/billing/invoices/{id}, /api/billing/invoices/{id}/pdf,
GET /api/billing/payments, /api/billing/payments/{id},
POST /api/billing/invoices/{id}/payment-link (spec sections 44/45/56, SB14/SB26).
Gated on role tier only (require_action's module=None) — Billing/Invoices
must stay reachable even when every subscription has expired (spec
section 60's explicit non-billable-route allowlist), so this
deliberately does NOT gate on any module license.

Org-scoped users (org_admin, etc.) only ever see their own organization's
invoices/payments; Seller/Super Admin (organization_id is None) may view
any organization's via the `organization_id` query param, or see the
requested row regardless of org once resolved by id. Generating a
payment link is Seller-only (require_seller_action) — it's a Seller
Billing screen action, not a customer self-service one yet (that's
SB29's "Pay Now" on the Customer Billing screens, wired separately).
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_organization, get_current_user, require_action, require_seller_action
from app.database.connection import get_db
from app.models.billing.invoice import Invoice, InvoiceStatus
from app.models.billing.payment import Payment
from app.models.billing.payment_gateway import GatewayProvider, PaymentGateway
from app.models.organization import Organization
from app.models.seller import Seller
from app.models.seller_audit_log import record_seller_event
from app.models.user import User
from app.payment_gateways.base import PaymentGatewayError
from app.schemas.billing.invoices import InvoiceLineItemOut, InvoiceListOut, InvoiceOut
from app.schemas.billing.payments import PaymentLinkOut, PaymentListOut, PaymentOut
from app.services.billing import invoice_service, payment_gateway_service
from app.services.billing.invoice_pdf import generate_invoice_pdf
from app.services.seller import company_service
from sqlalchemy import select

router = APIRouter(prefix="/billing", tags=["billing"])

_view = require_action("VIEW", module=None)
_seller_generate_link = require_seller_action("EDIT", "Billing")
# SB29 — customer self-service "Pay Now": EDIT tier (not bare VIEW) since
# it's a real write action, but no module license required, matching
# every other billing route in this file (Billing must stay reachable
# even with every subscription expired).
_customer_pay = require_action("EDIT", module=None)


async def _resolve_org_id(user: User, org: Organization | None, requested: int | None) -> int | None:
    if org is not None:
        return org.id  # org-scoped user — always their own org, requested param ignored
    return requested  # Seller/Super Admin — None means "all orgs"


def _require_org_scoped(org: Organization | None) -> Organization:
    if org is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This action is for customer organizations; Sellers generate payment links from the Seller Billing screen instead.")
    return org


@router.get("/invoices", response_model=InvoiceListOut)
async def list_invoices(
    organization_id: int | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    page: int = 1, page_size: int = 20,
    user: User = Depends(_view), org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    scoped_org_id = await _resolve_org_id(user, org, organization_id)
    items, total = await invoice_service.list_invoices(db, organization_id=scoped_org_id, status_filter=status_filter, page=page, page_size=page_size)
    return InvoiceListOut(items=items, total=total, page=page, page_size=page_size)


async def _get_invoice_or_404(db: AsyncSession, invoice_id: int, user: User, org: Organization | None):
    invoice = await invoice_service.get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    if org is not None and invoice.organization_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return invoice


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: int, user: User = Depends(_view), org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, invoice_id, user, org)
    line_items = await invoice_service.get_invoice_line_items(db, invoice_id)
    invoice_org = await db.get(Organization, invoice.organization_id)
    return InvoiceOut(
        id=invoice.id, invoice_number=invoice.invoice_number, organization_id=invoice.organization_id,
        organization_name=invoice_org.name if invoice_org else None, subscription_id=invoice.subscription_id,
        status=invoice.status.value, currency=invoice.currency, subtotal=invoice.subtotal, discount=invoice.discount,
        tax=invoice.tax, total=invoice.total, billing_period_start=invoice.billing_period_start,
        billing_period_end=invoice.billing_period_end, issue_date=invoice.issue_date, due_date=invoice.due_date,
        paid_at=invoice.paid_at, reason=invoice.reason, created_at=invoice.created_at,
        line_items=[InvoiceLineItemOut.model_validate(li) for li in line_items],
    )


@router.get("/invoices/{invoice_id}/pdf")
async def download_invoice_pdf(
    invoice_id: int, user: User = Depends(_view), org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    invoice = await _get_invoice_or_404(db, invoice_id, user, org)
    line_items = await invoice_service.get_invoice_line_items(db, invoice_id)
    invoice_org = await db.get(Organization, invoice.organization_id)
    seller_row = (await db.execute(select(Seller).limit(1))).scalar_one_or_none()
    company_row = await company_service.get_effective_company(db, invoice_org.company_id if invoice_org else None)
    pdf_bytes = generate_invoice_pdf(invoice, line_items, invoice_org, seller_row, company_row)
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{invoice.invoice_number}.pdf"'},
    )


async def _create_payment_link_for_invoice(db: AsyncSession, invoice: Invoice, *, return_url: str, actor: User) -> PaymentLinkOut:
    """Shared by the Seller "Generate Link" action and the customer
    self-service "Pay Now" button (SB29) — same gateway, same idempotent
    PaymentLink row, only the caller's authorization differs."""
    if invoice.status == InvoiceStatus.PAID:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This invoice is already paid.")

    gateway = (await db.execute(
        select(PaymentGateway).where(PaymentGateway.provider == GatewayProvider.CUSTOM)
    )).scalar_one_or_none()
    if gateway is None or not gateway.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No active payment gateway is configured. Activate one in Payment Gateways.")

    try:
        provider = await payment_gateway_service.build_provider(db, gateway, environment=gateway.environment.value)
        link = await provider.create_payment_link(invoice=invoice, return_url=return_url)
    except (payment_gateway_service.PaymentGatewayServiceError, PaymentGatewayError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    from app.models.billing.payment_link import PaymentLink, PaymentLinkStatus
    db.add(PaymentLink(
        invoice_id=invoice.id, gateway_id=gateway.id, gateway_reference=link.gateway_reference,
        payment_url=link.payment_url, token=link.gateway_reference, status=PaymentLinkStatus.ACTIVE, expires_at=link.expires_at,
    ))
    await db.commit()

    # SB32, found live: "Payment Link Created" has been a declared
    # SELLER_AUDIT_ACTIONS entry since SB1, but nothing ever actually
    # fired it — every real payment link (Seller "Generate Link" and
    # customer "Pay Now" alike) was created with zero audit trail.
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=invoice.organization_id, action="Payment Link Created",
        module="Billing", resource=invoice.invoice_number, resource_id=str(invoice.id),
        details=f"{invoice.currency} {invoice.total:,.2f} via {gateway.provider.value}",
    )

    return PaymentLinkOut(payment_url=link.payment_url, gateway_reference=link.gateway_reference, expires_at=link.expires_at)


@router.post("/invoices/{invoice_id}/payment-link", response_model=PaymentLinkOut)
async def create_invoice_payment_link(
    invoice_id: int, user: User = Depends(_seller_generate_link), db: AsyncSession = Depends(get_db),
):
    invoice = await invoice_service.get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return await _create_payment_link_for_invoice(db, invoice, return_url="/app/seller/billing", actor=user)


@router.post("/invoices/{invoice_id}/pay", response_model=PaymentLinkOut)
async def pay_invoice(
    invoice_id: int, user: User = Depends(_customer_pay), org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    """SB29's customer self-service "Pay Now" — org-scoped only (a Seller
    generates links from the Seller Billing screen instead, via the route
    above). 404s rather than 403s on a foreign invoice, same as every
    other org-scoped lookup in this file, so it never confirms another
    org's invoice IDs exist."""
    org = _require_org_scoped(org)
    invoice = await invoice_service.get_invoice(db, invoice_id)
    if invoice is None or invoice.organization_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return await _create_payment_link_for_invoice(db, invoice, return_url="/app/billing", actor=user)


@router.get("/payments", response_model=PaymentListOut)
async def list_payments(
    organization_id: int | None = Query(None), page: int = 1, page_size: int = 20,
    user: User = Depends(_view), org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    scoped_org_id = await _resolve_org_id(user, org, organization_id)
    result = await db.execute(
        select(Payment, Organization, PaymentGateway, Invoice)
        .join(Organization, Organization.id == Payment.organization_id)
        .join(PaymentGateway, PaymentGateway.id == Payment.gateway_id)
        .join(Invoice, Invoice.id == Payment.invoice_id)
        .where(*([Payment.organization_id == scoped_org_id] if scoped_org_id is not None else []))
        .order_by(Payment.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    rows = result.all()

    count_stmt = select(Payment.id)
    if scoped_org_id is not None:
        count_stmt = count_stmt.where(Payment.organization_id == scoped_org_id)
    total = len((await db.execute(count_stmt)).all())

    items = [
        PaymentOut(
            id=p.id, invoice_id=p.invoice_id, invoice_number=inv.invoice_number, organization_id=p.organization_id,
            organization_name=org_row.name, gateway_id=p.gateway_id, gateway_provider=gw.provider.value,
            gateway_transaction_id=p.gateway_transaction_id, amount=p.amount, currency=p.currency,
            status=p.status.value, payment_method=p.payment_method, paid_at=p.paid_at, created_at=p.created_at,
        )
        for p, org_row, gw, inv in rows
    ]
    return PaymentListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/payments/{payment_id}", response_model=PaymentOut)
async def get_payment(
    payment_id: int, user: User = Depends(_view), org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
):
    payment = await db.get(Payment, payment_id)
    if payment is None or (org is not None and payment.organization_id != org.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    payment_org = await db.get(Organization, payment.organization_id)
    gateway = await db.get(PaymentGateway, payment.gateway_id)
    invoice = await db.get(Invoice, payment.invoice_id)
    return PaymentOut(
        id=payment.id, invoice_id=payment.invoice_id, invoice_number=invoice.invoice_number if invoice else None,
        organization_id=payment.organization_id, organization_name=payment_org.name if payment_org else None,
        gateway_id=payment.gateway_id, gateway_provider=gateway.provider.value if gateway else None,
        gateway_transaction_id=payment.gateway_transaction_id, amount=payment.amount, currency=payment.currency,
        status=payment.status.value, payment_method=payment.payment_method, paid_at=payment.paid_at, created_at=payment.created_at,
    )
