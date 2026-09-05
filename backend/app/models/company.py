"""
Company — a legal entity/brand a Seller issues invoices under. Added
because a single Seller (the platform vendor account, still a singleton
per models/seller.py) can operate under more than one registered company
name — e.g. regional subsidiaries or separate legal entities selling the
same platform. Every customer Organization is assigned to exactly one
Company (Organization.company_id); that assignment is what determines
whose name/logo/address/signature appears on that org's invoices.

Not a duplicate of Seller: Seller is the platform account (support/sales
contact, branding defaults, SMTP config) and stays a singleton; Company is
the billing-entity layer underneath it, and there can be many.
"""
from datetime import datetime

from sqlalchemy import String, DateTime, Text, Boolean, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class Company(PlatformBase):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)

    address_line: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Uploaded images, stored as data: URIs (base64) — same "no external
    # dependency, works offline" reasoning as everything else in this
    # codebase that isn't a real third-party integration. Kept as Text
    # (not JSON) since it's a single opaque string, not structured data.
    logo: Mapped[str | None] = mapped_column(Text, nullable=True)
    digital_signature: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Exactly one Company per Seller should be_default at a time —
    # enforced in services/seller/company_service.py (unsetting the
    # previous default in the same transaction), not at the DB level,
    # since SQLite has no partial-unique-index support this codebase
    # already leans on elsewhere.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Company {self.name}>"
