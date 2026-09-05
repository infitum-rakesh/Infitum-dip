"""
Seller — the operating company selling this platform to customers (see
SELLER PORTAL spec: "Seller represents the company/person that sells the
platform to customers"). Singleton in this build — exactly one Seller row
is seeded at startup (see services/seller/plan_seed.ensure_seller_seeded)
and every Seller-tier user (User.role == UserRole.SELLER) manages that one
row. Organization.seller_id (models/organization.py) already existed as an
unconstrained int before this module — it is the column that points here.

The schema itself doesn't forbid more than one Seller row (e.g. future
white-label resellers), it's just that no seller-switching UI exists yet.
"""
from datetime import datetime

from sqlalchemy import String, DateTime, JSON, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class Seller(PlatformBase):
    __tablename__ = "sellers"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    support_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    billing_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Multi-Subscription Billing & Payments Platform (2026) spec section 46 —
    # configurable Sales contact used by expired-subscription messaging
    # ("Please contact Sales at ...") instead of any hardcoded seller info.
    sales_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sales_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sales_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sales_contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # {logo_url, primary_color} — Seller Settings -> Branding (Phase 9)
    branding: Mapped[dict] = mapped_column(JSON, default=dict)
    # Uploaded signature image (base64 data: URI), same convention as
    # models/company.py's Company.digital_signature — stamped on every
    # invoice PDF alongside the invoice's assigned Company's own signature
    # (see services/billing/invoice_pdf.py). This is the platform Seller's
    # signature, shown regardless of which Company issued the invoice.
    digital_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_trial_days: Mapped[int] = mapped_column(Integer, default=14)
    # {host, port, username, password_encrypted, from_address, use_tls} —
    # Seller Settings -> Email. password_encrypted is Fernet-encrypted via
    # app.core.crypto (same mechanism as Connection.encrypted_secrets) —
    # never stored or returned in plaintext. See services/email_service.py.
    smtp_config: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Seller {self.name}>"
