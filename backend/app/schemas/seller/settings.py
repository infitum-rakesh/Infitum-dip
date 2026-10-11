"""Pydantic models for /api/seller/settings (Phase 9). Wraps the singleton
Seller row — see models/seller.py + services/seller/seller_settings_service.py."""
from datetime import datetime

from pydantic import BaseModel


class SellerBrandingOut(BaseModel):
    logo_url: str | None = None
    primary_color: str | None = None


class SmtpConfigOut(BaseModel):
    """Never carries a real secret — `password_set`/`api_key_set` just
    tell the UI whether one is already stored, same convention as
    Connection's masked secret fields (see connection_service._masked_config).
    `provider` picks which email_providers/ adapter send_email() resolves
    to; host/port/username/password_set are SMTP-only, api_key_set is
    Resend-only — the unused half is just left at its default for
    whichever provider isn't selected."""
    provider: str = "SMTP"  # "SMTP" | "RESEND"
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password_set: bool = False
    api_key_set: bool = False
    from_address: str | None = None
    use_tls: bool = True


class SellerSettingsOut(BaseModel):
    id: int
    name: str
    legal_name: str | None
    support_email: str | None
    billing_email: str | None
    website: str | None
    # SB30 — the real Sales contact surfaced by GET /api/access/contact
    # for the Contact Sales action on expired-subscription messaging.
    sales_contact_name: str | None = None
    sales_email: str | None = None
    sales_phone: str | None = None
    sales_url: str | None = None
    branding: dict
    # Uploaded signature image (base64 data: URI) stamped on every invoice
    # PDF alongside the invoice's Company (see models/company.py). Kept as
    # a plain string, not a dict, to mirror Company.digital_signature.
    digital_signature: str | None = None
    default_trial_days: int
    smtp: SmtpConfigOut
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UpdateSellerSettingsRequest(BaseModel):
    name: str | None = None
    legal_name: str | None = None
    support_email: str | None = None
    billing_email: str | None = None
    website: str | None = None
    sales_contact_name: str | None = None
    sales_email: str | None = None
    sales_phone: str | None = None
    sales_url: str | None = None
    branding: dict | None = None
    digital_signature: str | None = None
    default_trial_days: int | None = None


class UpdateSmtpConfigRequest(BaseModel):
    provider: str | None = None  # "SMTP" | "RESEND" — omit to leave the currently-selected provider unchanged
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None  # plaintext in, encrypted before storage; omit/blank = leave unchanged
    api_key: str | None = None  # Resend — plaintext in, encrypted before storage; omit/blank = leave unchanged
    from_address: str | None = None
    use_tls: bool | None = None


class SendTestEmailRequest(BaseModel):
    to: str
