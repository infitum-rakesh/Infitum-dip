import enum
from datetime import datetime

from sqlalchemy import String, Enum, Boolean, DateTime, Integer, JSON, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.connection import PlatformBase


class LicenseStatus(str, enum.Enum):
    TRIAL = "trial"
    ACTIVE = "active"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    SUSPENDED = "suspended"


class Organization(PlatformBase):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Seller Portal -> Customers (Phase 2). Set at Create Customer time (or
    # editable after); the org's own users don't see/edit these — they're
    # Seller-facing account metadata, distinct from any individual User row.
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    primary_contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    primary_contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    primary_contact_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(60), nullable=True)

    # Billing address (spec: invoice "Bill To" detail) — separate from the
    # primary_contact_* trio above, which is a person, not a mailing
    # address. "branch" names the specific office/location this org's
    # relationship is billed through (e.g. "Mumbai Branch", "HQ") when the
    # customer has more than one — free text, not a foreign key.
    address_line: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Which Company (models/company.py) this org's invoices are issued
    # under — a Seller can register more than one Company; this is what
    # picks whose logo/address/signature appears on THIS org's invoice
    # PDFs. Plain unconstrained int, same "no literal FK across this
    # boundary" convention already established by seller_id below (this
    # column was added long after that precedent, kept consistent with it
    # rather than introducing a real ForeignKey wrapper here alone).
    company_id: Mapped[int | None] = mapped_column(nullable=True)

    # License — set by the Seller (see IMPORTANT HIERARCHY in Administration ->
    # Licenses: Seller -> Organization -> Org Admin -> Users). An org admin can
    # never grant a user a module that isn't in licensed_modules; see
    # services/license_service.py and Administration -> Users' Module Access tab.
    license_status: Mapped[LicenseStatus] = mapped_column(
        Enum(LicenseStatus), default=LicenseStatus.TRIAL
    )
    plan: Mapped[str] = mapped_column(String(30), default="trial")  # trial|basic|professional|enterprise
    licensed_modules: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["OCR","AI_EXTRACTION"]
    max_users: Mapped[int] = mapped_column(Integer, default=5)
    trial_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    license_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Seller link (org belongs to a seller)
    seller_id: Mapped[int | None] = mapped_column(nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    users: Mapped[list["User"]] = relationship(back_populates="organization")  # type: ignore[name-defined]  # noqa: F821

    def __repr__(self) -> str:
        return f"<Organization {self.name}>"
