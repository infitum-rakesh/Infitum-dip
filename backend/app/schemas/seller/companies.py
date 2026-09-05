"""Pydantic models for /api/seller/companies/* — see models/company.py +
services/seller/company_service.py."""
from datetime import datetime

from pydantic import BaseModel, EmailStr


class CompanyOut(BaseModel):
    """List-shape — carries the logo (small, needed for list-row/picker
    previews) but only a has_signature flag, not the full signature image,
    to keep GET /seller/companies light. CompanyDetailOut below is the
    full shape, used by the single-company fetch the Edit form loads."""
    id: int
    name: str
    legal_name: str | None
    email: str | None
    phone: str | None
    website: str | None
    address_line: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None
    logo: str | None
    has_signature: bool
    is_default: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CompanyDetailOut(CompanyOut):
    digital_signature: str | None = None


class CompanyListOut(BaseModel):
    items: list[CompanyOut]


class CreateCompanyRequest(BaseModel):
    name: str
    legal_name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    website: str | None = None
    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    logo: str | None = None  # base64 data: URI, or None
    digital_signature: str | None = None  # base64 data: URI, or None
    is_default: bool = False


class UpdateCompanyRequest(BaseModel):
    name: str | None = None
    legal_name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    website: str | None = None
    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    logo: str | None = None
    digital_signature: str | None = None
