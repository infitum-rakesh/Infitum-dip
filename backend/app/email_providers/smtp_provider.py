"""
SMTPProvider — real smtplib delivery, forced over IPv4.

Found live on Railway: smtp.gmail.com (and most real SMTP hosts) publish
both A and AAAA records. Plain smtplib.SMTP() lets socket.create_connection()
pick whatever address family getaddrinfo() returns first — on Railway's
network (no outbound IPv6 route) an AAAA hit fails immediately with
[Errno 101] Network is unreachable. IPv4SMTP below forces IPv4 so that
specific failure can't recur, on Railway or any other IPv6-incomplete host.

That fix alone wasn't enough, though: Railway was then found to block
ALL outbound SMTP ports (25/465/587/2525) at the network level unless the
workspace is on a Pro/Enterprise plan — a platform policy no client-side
code change can work around. SMTPProvider is still real, correct code
(works on a Pro/Enterprise Railway workspace, or literally any other
host), but resend_provider.py is the one guaranteed to work regardless
of that policy, since it goes over HTTPS instead.
"""
from __future__ import annotations

import smtplib
import socket
from email.mime.text import MIMEText

from app.email_providers.base import EmailDeliveryResult, EmailProvider


class IPv4SMTP(smtplib.SMTP):
    """smtplib.SMTP, but the connection is forced over IPv4 — see module
    docstring. The hostname is still passed to the SMTP constructor
    unchanged, so STARTTLS certificate verification (which checks the
    connection's hostname, not its IP) is unaffected."""

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


class SMTPProvider(EmailProvider):
    """config: {host, port, username, password, from_address, use_tls}
    — password already decrypted by the caller."""

    async def send(self, *, to: str, subject: str, body: str) -> EmailDeliveryResult:
        host = self.config.get("host")
        if not host:
            return EmailDeliveryResult(False, "SMTP is not configured — set it up in Seller Settings → Email.")
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = self.config.get("from_address") or "no-reply@dip.local"
            msg["To"] = to
            with IPv4SMTP(host, int(self.config.get("port", 587)), timeout=10) as server:
                if self.config.get("use_tls", True):
                    server.starttls()
                username = self.config.get("username")
                if username:
                    server.login(username, self.config.get("password") or "")
                server.sendmail(msg["From"], [to], msg.as_string())
            return EmailDeliveryResult(True, f"Sent to {to}.")
        except Exception as exc:
            return EmailDeliveryResult(False, f"SMTP send failed: {exc}"[:300])
