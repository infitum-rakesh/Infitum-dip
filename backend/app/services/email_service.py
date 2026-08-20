"""
EmailService — the one place this codebase sends real outbound email via
SMTP. Used by:
  - services/seller/customer_service.create_customer() — welcomes a new
    company's auto-created Admin user with their login credentials.
  - (future) any module that wants a real email instead of duplicating
    the same smtplib boilerplate app/monitoring/notification_channels.py's
    EmailChannel already has for AlertRule delivery.

Configuration lives on the singleton Seller row (models/seller.py's
smtp_config JSON column) — the SELLER operates the platform and is the
one actually sending "your company's account is ready" emails, so their
Settings page (not each customer Organization's Settings) is where SMTP
credentials belong. The password is Fernet-encrypted at rest via
app.core.crypto, same mechanism Connection.encrypted_secrets already uses.

Delivery honesty: if no SMTP host is configured, send() returns
delivered=False with a clear reason — never silently pretends to have
sent something, matching notification_channels.EmailChannel's contract.
"""
from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.models.seller import Seller


class EmailDeliveryResult:
    def __init__(self, delivered: bool, detail: str):
        self.delivered = delivered
        self.detail = detail

    def to_dict(self) -> dict:
        return {"delivered": self.delivered, "detail": self.detail}


async def get_smtp_config(db: AsyncSession) -> dict:
    result = await db.execute(select(Seller).limit(1))
    seller = result.scalar_one_or_none()
    return (seller.smtp_config or {}) if seller else {}


def _decrypt_password(smtp_config: dict) -> str:
    token = smtp_config.get("password_encrypted")
    return (crypto.decrypt_secret(token) if token else None) or ""


async def send_email(db: AsyncSession, *, to: str, subject: str, body: str) -> EmailDeliveryResult:
    cfg = await get_smtp_config(db)
    host = cfg.get("host")
    if not host:
        return EmailDeliveryResult(False, "SMTP is not configured — set it up in Seller Settings → Email.")

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = cfg.get("from_address") or "no-reply@dip.local"
        msg["To"] = to
        with smtplib.SMTP(host, int(cfg.get("port", 587)), timeout=10) as server:
            if cfg.get("use_tls", True):
                server.starttls()
            username = cfg.get("username")
            if username:
                server.login(username, _decrypt_password(cfg))
            server.sendmail(msg["From"], [to], msg.as_string())
        return EmailDeliveryResult(True, f"Sent to {to}.")
    except Exception as exc:
        return EmailDeliveryResult(False, f"SMTP send failed: {exc}"[:300])


async def send_test_email(db: AsyncSession, *, to: str) -> EmailDeliveryResult:
    return await send_email(
        db, to=to, subject="DataIntelligence Platform — SMTP test",
        body="This is a test email from your Seller Settings → Email configuration. If you received this, outbound email is working.",
    )


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
