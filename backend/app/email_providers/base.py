"""
Email provider interface — mirrors payment_gateways/base.py's established
shape exactly: a real provider makes a genuine call against the real
protocol/API it claims to (no simulated success/failure branch), resolved
by email_providers/registry.py, so services/email_service.py never
contains provider-specific code.

Built live after Railway was found to block all outbound SMTP ports
(25/465/587/2525) for non-Pro workspaces — smtplib-based delivery
(smtp_provider.py) still works for any host where the SMTP ports aren't
blocked (a Pro/Enterprise Railway plan, or any other host), but an
HTTP-API provider (resend_provider.py) is the one that's guaranteed to
work regardless of platform SMTP-port policy, since outbound HTTPS is
essentially never blocked. Both share this one interface so Seller
Settings -> Email can switch between them as a config change, never a
code change.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


class EmailProviderError(Exception):
    """A provider call failed for a real reason (network, auth, API
    error) — never silently swallowed into a fabricated success."""


@dataclass
class EmailDeliveryResult:
    delivered: bool
    detail: str

    def to_dict(self) -> dict:
        return {"delivered": self.delivered, "detail": self.detail}


@dataclass
class ConnectionTestResult:
    success: bool
    detail: str


class EmailProvider(abc.ABC):
    """One instance per (provider config) — constructed with
    already-decrypted credentials, never touches encrypted values itself
    (that's seller_settings_service.py's job, same separation
    payment_gateway_service.py keeps for PaymentGatewayProvider)."""

    def __init__(self, config: dict):
        self.config = config

    @abc.abstractmethod
    async def send(self, *, to: str, subject: str, body: str) -> EmailDeliveryResult:
        """Sends a real email. Returns delivered=False with an honest
        detail on any failure — never raises past this boundary, matching
        the Delivery honesty contract email_service.py's callers depend
        on (send_email() has never raised to its callers; every provider
        must preserve that)."""
