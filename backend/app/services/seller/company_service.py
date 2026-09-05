"""
Company CRUD (models/company.py) — the billing-entity layer under the
singleton Seller account. A Seller can register more than one Company
(e.g. separate legal entities/regional brands); each customer
Organization is assigned to exactly one (Organization.company_id), which
determines whose logo/address/signature appears on that org's invoices
(see services/billing/invoice_pdf.py).

Partial update uses body.model_fields_set (not `is not None`) — same
explicit-null-clearing convention as seller_settings_service.update_settings().
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.seller import Seller
from app.models.seller_audit_log import record_seller_event
from app.models.user import User

_TRACKED_FIELDS = (
    "name", "legal_name", "email", "phone", "website",
    "address_line", "city", "state", "postal_code", "country",
)


class CompanyServiceError(Exception):
    pass


def to_out(company: Company) -> dict:
    return {
        "id": company.id, "name": company.name, "legal_name": company.legal_name,
        "email": company.email, "phone": company.phone, "website": company.website,
        "address_line": company.address_line, "city": company.city, "state": company.state,
        "postal_code": company.postal_code, "country": company.country,
        "logo": company.logo, "has_signature": bool(company.digital_signature),
        "is_default": company.is_default, "is_active": company.is_active,
        "created_at": company.created_at, "updated_at": company.updated_at,
    }


def to_detail_out(company: Company) -> dict:
    return {**to_out(company), "digital_signature": company.digital_signature}


async def _get_seller(db: AsyncSession) -> Seller:
    result = await db.execute(select(Seller).limit(1))
    seller = result.scalar_one_or_none()
    if seller is None:
        raise CompanyServiceError("No Seller account exists yet.")
    return seller


async def list_companies(db: AsyncSession, *, include_inactive: bool = True) -> list[Company]:
    seller = await _get_seller(db)
    stmt = select(Company).where(Company.seller_id == seller.id)
    if not include_inactive:
        stmt = stmt.where(Company.is_active.is_(True))
    result = await db.execute(stmt.order_by(Company.is_default.desc(), Company.name))
    return list(result.scalars().all())


async def get_company(db: AsyncSession, company_id: int) -> Company | None:
    return await db.get(Company, company_id)


async def get_default_company(db: AsyncSession) -> Company | None:
    result = await db.execute(
        select(Company).where(Company.is_default.is_(True), Company.is_active.is_(True)).limit(1)
    )
    return result.scalar_one_or_none()


async def get_effective_company(db: AsyncSession, org_company_id: int | None) -> Company | None:
    """Resolve which Company's branding should appear on an org's invoice.

    Prefers the org's explicitly assigned Company, but only while it's
    still active — an org whose assigned Company gets deactivated (e.g.
    after a Seller registers a new default and retires the old one)
    falls back to the seller's current active default Company instead
    of continuing to show the retired one's logo/signature/address.
    Found live: deactivating the previous default Company left its
    branding stuck on invoices for orgs still pointing at its id.
    """
    if org_company_id:
        company = await db.get(Company, org_company_id)
        if company is not None and company.is_active:
            return company
    return await get_default_company(db)


async def _clear_other_defaults(db: AsyncSession, seller_id: int, *, except_id: int | None = None) -> None:
    result = await db.execute(select(Company).where(Company.seller_id == seller_id, Company.is_default.is_(True)))
    for other in result.scalars().all():
        if other.id != except_id:
            other.is_default = False


async def create_company(db: AsyncSession, *, body, actor: User) -> Company:
    seller = await _get_seller(db)
    existing = await list_companies(db)
    is_default = body.is_default or not existing  # the very first Company is always the default

    company = Company(
        seller_id=seller.id, name=body.name, legal_name=body.legal_name,
        email=body.email, phone=body.phone, website=body.website,
        address_line=body.address_line, city=body.city, state=body.state,
        postal_code=body.postal_code, country=body.country,
        logo=body.logo, digital_signature=body.digital_signature,
        is_default=is_default, is_active=True,
    )
    db.add(company)
    await db.flush()

    if is_default:
        await _clear_other_defaults(db, seller.id, except_id=company.id)

    await db.commit()
    await db.refresh(company)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Company Created",
        module="Billing", resource=company.name, resource_id=str(company.id),
        details=f"Company '{company.name}' created" + (" and set as default" if is_default else ""),
    )
    return company


async def update_company(db: AsyncSession, company: Company, *, body, actor: User) -> Company:
    provided = body.model_fields_set
    before = {field: getattr(company, field) for field in _TRACKED_FIELDS}
    changed = []
    for field in _TRACKED_FIELDS:
        if field in provided:
            value = getattr(body, field)
            if value != getattr(company, field):
                setattr(company, field, value)
                changed.append(field)

    if "logo" in provided and body.logo != company.logo:
        company.logo = body.logo
        changed.append("logo")
    if "digital_signature" in provided and body.digital_signature != company.digital_signature:
        company.digital_signature = body.digital_signature
        changed.append("digital_signature")

    if not changed:
        return company

    await db.commit()
    await db.refresh(company)
    after = {field: getattr(company, field) for field in _TRACKED_FIELDS}
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Company Updated",
        module="Billing", resource=company.name, resource_id=str(company.id),
        details=f"Updated {', '.join(changed)}", before_value=before, after_value=after,
    )
    return company


async def set_active(db: AsyncSession, company: Company, *, active: bool, actor: User) -> Company:
    if company.is_default and not active:
        raise CompanyServiceError("Cannot deactivate the default Company — set a different Company as default first.")
    company.is_active = active
    await db.commit()
    await db.refresh(company)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None,
        action="Company Activated" if active else "Company Deactivated",
        module="Billing", resource=company.name, resource_id=str(company.id),
    )
    return company


async def set_default(db: AsyncSession, company: Company, *, actor: User) -> Company:
    if not company.is_active:
        raise CompanyServiceError("An inactive Company cannot be set as default.")
    if company.is_default:
        return company
    await _clear_other_defaults(db, company.seller_id, except_id=company.id)
    company.is_default = True
    await db.commit()
    await db.refresh(company)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Company Set Default",
        module="Billing", resource=company.name, resource_id=str(company.id),
    )
    return company
