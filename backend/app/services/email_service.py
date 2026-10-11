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
import socket
from email.mime.text import MIMEText

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.models.seller import Seller


class IPv4SMTP(smtplib.SMTP):
    """smtplib.SMTP, but the connection is forced over IPv4.

    Found live on Railway: smtp.gmail.com (and most real SMTP hosts)
    publish both A and AAAA records. Plain smtplib.SMTP() resolves via
    socket.create_connection(), which tries whatever address family
    getaddrinfo() returns first — on Railway's network (no outbound IPv6
    route) an AAAA hit fails immediately with [Errno 101] Network is
    unreachable, the exact error surfaced in Seller Settings -> Email's
    "Send Test" even though the same code works fine locally (where IPv6
    either isn't offered or actually routes). The hostname itself is
    still passed to the SMTP constructor unchanged, so STARTTLS
    certificate verification (which checks the connection's hostname,
    not its IP) is unaffected."""

    def _get_socket(self, host, port, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        last_err: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
            sock = socket.socket(family, socktype, proto)
            try:
                if timeout is not None:
                    sock.settimeout(timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                sock.close()
                last_err = exc
        raise last_err or OSError(f"No IPv4 address found for {host}")


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
        with IPv4SMTP(host, int(cfg.get("port", 587)), timeout=10) as server:
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
