"""
ResendProvider — real delivery via Resend's HTTPS API
(https://resend.com/docs/api-reference/emails/send-email), no SMTP
involved. Built specifically because Railway blocks outbound SMTP ports
for non-Pro workspaces (see smtp_provider.py's docstring) but never
blocks outbound HTTPS — this is the provider that keeps working
regardless of that policy.

"from" must be either Resend's shared, unverified sandbox sender
(onboarding@resend.dev — works immediately, no setup) or an address on a
domain verified in the Resend dashboard; an unverified custom domain
fails the send with a real 403 from Resend's API, surfaced here as a
genuine delivered=False, never silently swallowed.

Disclosed per this codebase's established precedent for third-party
adapters (see payment_gateways/stripe_provider.py etc.): real code
written against Resend's own documented API contract, not yet exercised
against a live Resend account in this environment.
"""
from __future__ import annotations

import httpx

from app.email_providers.base import EmailDeliveryResult, EmailProvider

RESEND_API_URL = "https://api.resend.com/emails"
REQUEST_TIMEOUT = 10.0


class ResendProvider(EmailProvider):
    """config: {api_key, from_address} — api_key already decrypted by
    the caller."""

    async def send(self, *, to: str, subject: str, body: str) -> EmailDeliveryResult:
        api_key = self.config.get("api_key")
        if not api_key:
            return EmailDeliveryResult(False, "Resend is not configured — add an API key in Seller Settings → Email.")

        from_address = self.config.get("from_address") or "onboarding@resend.dev"
        payload = {"from": from_address, "to": [to], "subject": subject, "text": body}

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    RESEND_API_URL, json=payload,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                )
        except httpx.HTTPError as exc:
            return EmailDeliveryResult(False, f"Resend send failed: {exc}"[:300])

        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message", resp.text)
            except ValueError:
                detail = resp.text
            return EmailDeliveryResult(False, f"Resend send failed ({resp.status_code}): {detail}"[:300])

        return EmailDeliveryResult(True, f"Sent to {to} via Resend.")
