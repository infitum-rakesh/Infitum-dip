"""
EmailService — the one place this codebase resolves Seller Settings ->
Email's configured provider and sends through it. Used by:
  - services/seller/customer_service.create_customer() — welcomes a new
    company's auto-created Admin user with their login credentials.
  - (future) any module that wants a real email instead of duplicating
    this.

Configuration lives on the singleton Seller row (models/seller.py's
smtp_config JSON column — kept under its original name even though it
now holds any provider's config, to avoid an unnecessary migration;
`provider` inside that JSON discriminates which one). The SELLER
operates the platform and is the one actually sending "your company's
account is ready" emails, so their Settings page (not each customer
Organization's Settings) is where credentials belong. Secrets (the SMTP
password, or a provider's API key) are Fernet-encrypted at rest via
app.core.crypto, same mechanism Connection.encrypted_secrets already uses.

Provider resolution: email_providers/registry.py maps the stored
`provider` string ("SMTP" default, or "RESEND") to a real
email_providers.base.EmailProvider implementation — see that package's
docstrings for why there's more than one (Railway blocks outbound SMTP
ports for non-Pro workspaces; an HTTP-API provider is the one guaranteed
to work regardless of that policy).

Delivery honesty: if no provider is configured, send() returns
delivered=False with a clear reason — never silently pretends to have
sent something, matching notification_channels.EmailChannel's contract.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.email_providers import registry
from app.email_providers.base import EmailDeliveryResult
from app.models.seller import Seller

__all__ = [
    "EmailDeliveryResult", "get_smtp_config", "send_email", "send_test_email",
    "build_sales_inquiry_email", "build_welcome_email", "build_invite_email", "build_password_reset_email",
]


async def get_smtp_config(db: AsyncSession) -> dict:
    result = await db.execute(select(Seller).limit(1))
    seller = result.scalar_one_or_none()
    return (seller.smtp_config or {}) if seller else {}


def _decrypted_provider_config(cfg: dict) -> dict:
    """Resolves the right secret for whichever provider is configured —
    password_encrypted for SMTP, api_key_encrypted for Resend — into the
    plaintext field each EmailProvider implementation actually reads."""
    provider = (cfg.get("provider") or "SMTP").upper()
    resolved = dict(cfg)
    if provider == "RESEND":
        token = cfg.get("api_key_encrypted")
        resolved["api_key"] = (crypto.decrypt_secret(token) if token else None) or ""
    else:
        token = cfg.get("password_encrypted")
        resolved["password"] = (crypto.decrypt_secret(token) if token else None) or ""
    return resolved


async def send_email(db: AsyncSession, *, to: str, subject: str, body: str) -> EmailDeliveryResult:
    cfg = await get_smtp_config(db)
    provider_type = (cfg.get("provider") or "SMTP").upper()
    provider = registry.get_provider(provider_type, _decrypted_provider_config(cfg))
    if provider is None:
        return EmailDeliveryResult(False, f"Unknown email provider '{provider_type}'.")
    return await provider.send(to=to, subject=subject, body=body)


async def send_test_email(db: AsyncSession, *, to: str) -> EmailDeliveryResult:
    return await send_email(
        db, to=to, subject="DataIntelligence Platform — SMTP test",
        body="This is a test email from your Seller Settings → Email configuration. If you received this, outbound email is working.",
    )


def build_sales_inquiry_email(
    *, org_name: str, module_interest: str | None, contact_name: str, contact_email: str,
    contact_phone: str | None, message: str,
) -> tuple[str, str]:
    subject = f"Module purchase inquiry — {org_name}"
    body = (
        f"A customer has requested to purchase or expand modules.\n\n"
        f"Organization: {org_name}\n"
        f"Module(s) of interest: {module_interest or 'Not specified'}\n"
        f"Contact name: {contact_name}\n"
        f"Contact email: {contact_email}\n"
        f"Contact phone: {contact_phone or 'Not provided'}\n\n"
        f"Message:\n{message}\n\n"
        f"— Sent from Administration → Licenses"
    )
    return subject, body


def build_welcome_email(*, org_name: str, full_name: str, email: str, temp_password: str, login_url: str) -> tuple[str, str]:
    subject = f"Your DataIntelligence Platform account for {org_name} is ready"
    body = (
        f"Hi {full_name},\n\n"
        f"An administrator account has been created for you at {org_name} on the DataIntelligence Platform.\n\n"
        f"Login: {login_url}\n"
        f"Email: {email}\n"
        f"Temporary password: {temp_password}\n\n"
        f"You'll be asked to sign in with this password — we recommend changing it after your first login "
        f"(Administration → Users → your profile).\n\n"
        f"— DataIntelligence Platform"
    )
    return subject, body


def build_invite_email(*, full_name: str, email: str, temp_password: str, login_url: str, role_label: str, org_name: str | None = None) -> tuple[str, str]:
    """Shared by both real invite flows that generate a temp password —
    services/user_admin_service.invite_user() (an org-scoped user) and
    services/seller/seller_user_service.invite_user() (a Seller Portal
    user, org_name=None). Deliberately separate from build_welcome_email()
    above (the Seller-onboards-a-customer-org flow) since this one has no
    org to introduce the recipient to when org_name is None."""
    where = f" at {org_name}" if org_name else ""
    subject = f"Your DataIntelligence Platform account{where} is ready"
    body = (
        f"Hi {full_name},\n\n"
        f"An account has been created for you{where} on the DataIntelligence Platform with the {role_label} role.\n\n"
        f"Login: {login_url}\n"
        f"Email: {email}\n"
        f"Temporary password: {temp_password}\n\n"
        f"You'll be asked to sign in with this password — we recommend changing it after your first login.\n\n"
        f"— DataIntelligence Platform"
    )
    return subject, body


def build_password_reset_email(*, full_name: str, email: str, temp_password: str, login_url: str) -> tuple[str, str]:
    """Shared by both "Reset Password" admin actions — same gap as
    build_invite_email() above (Administration -> Users and Seller
    Portal -> Seller Users both only ever returned the one-time
    temp_password with no attempt to email it, found live right after
    fixing the Invite flows). No org_name/role — a password reset
    doesn't need to re-introduce the account, just the new credential."""
    subject = "Your DataIntelligence Platform password has been reset"
    body = (
        f"Hi {full_name},\n\n"
        f"An administrator has reset your password on the DataIntelligence Platform.\n\n"
        f"Login: {login_url}\n"
        f"Email: {email}\n"
        f"New temporary password: {temp_password}\n\n"
        f"You'll be asked to sign in with this password — we recommend changing it after you sign in.\n\n"
        f"If you weren't expecting this, contact your administrator.\n\n"
        f"— DataIntelligence Platform"
    )
    return subject, body
