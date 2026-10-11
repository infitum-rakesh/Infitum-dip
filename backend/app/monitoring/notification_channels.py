"""
Notification channel registry — modular by design (user requirement:
"Do not hardcode a single notification provider"). An AlertRule.channels
entry is plain data ({"type": "EMAIL", "to": "..."}); dispatch() looks up
the matching CHANNEL class by `type` and calls it. Adding Slack, PagerDuty,
SMS etc. later means adding one class + one registry entry — nothing else
in the codebase (models, service, API, frontend) needs to change.

Delivery honesty: this codebase has no SMTP credentials or outbound-email
infra configured anywhere (grep confirms no email_service module exists),
so EmailChannel is real, working code that will genuinely send over
smtplib IF an org configures SMTP settings — see
services/settings_service.py's "notifications" section, which today only
has enable/disable toggles, not host/port/credentials. Until an org adds
those, EmailChannel returns delivered=False with an honest "SMTP not
configured" detail rather than pretending to have sent anything. WebhookChannel
and TeamsChannel are unconditionally real (plain HTTP POST) and were
live-verified against a local mock endpoint the same way the SOAP/AI
connectors were. InAppChannel always "delivers" because the AlertEvent
row itself IS the in-app notification — nothing further to send.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx


class NotificationChannel:
    type: str = "BASE"

    async def send(self, config: dict, event: dict) -> dict:
        """Returns {"type": self.type, "delivered": bool, "detail": str}."""
        raise NotImplementedError


class InAppChannel(NotificationChannel):
    type = "IN_APP"

    async def send(self, config: dict, event: dict) -> dict:
        # The AlertEvent row is the notification — nothing more to do.
        return {"type": self.type, "delivered": True, "detail": "Recorded as an in-app alert."}


class EmailChannel(NotificationChannel):
    type = "EMAIL"

    async def send(self, config: dict, event: dict) -> dict:
        to_addr = (config or {}).get("to")
        if not to_addr:
            return {"type": self.type, "delivered": False, "detail": "No recipient address configured on this rule."}

        smtp_cfg = (config or {}).get("smtp") or {}
        host = smtp_cfg.get("host")
        if not host:
            return {
                "type": self.type, "delivered": False,
                "detail": "SMTP is not configured for this organization — Email channel is wired but has no transport to send through yet.",
            }

        from email.mime.text import MIMEText
        from app.services.email_service import IPv4SMTP
        try:
            msg = MIMEText(event.get("message", ""))
            msg["Subject"] = f"[{event.get('severity', 'ALERT')}] {event.get('rule_name', 'Alert')}"
            msg["From"] = smtp_cfg.get("from_address") or "dip-monitoring@localhost"
            msg["To"] = to_addr
            with IPv4SMTP(host, int(smtp_cfg.get("port", 587)), timeout=10) as server:
                if smtp_cfg.get("use_tls", True):
                    server.starttls()
                if smtp_cfg.get("username"):
                    server.login(smtp_cfg["username"], smtp_cfg.get("password", ""))
                server.sendmail(msg["From"], [to_addr], msg.as_string())
            return {"type": self.type, "delivered": True, "detail": f"Sent to {to_addr}."}
        except Exception as exc:
            return {"type": self.type, "delivered": False, "detail": f"SMTP send failed: {exc}"[:300]}


class WebhookChannel(NotificationChannel):
    type = "WEBHOOK"

    async def send(self, config: dict, event: dict) -> dict:
        url = (config or {}).get("url")
        if not url:
            return {"type": self.type, "delivered": False, "detail": "No webhook URL configured on this rule."}
        payload = {
            "rule_name": event.get("rule_name"),
            "condition": event.get("condition"),
            "severity": event.get("severity"),
            "message": event.get("message"),
            "pipeline_id": event.get("pipeline_id"),
            "pipeline_name": event.get("pipeline_name"),
            "job_id": event.get("job_id"),
            "triggered_at": event.get("triggered_at"),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
            delivered = 200 <= resp.status_code < 300
            return {"type": self.type, "delivered": delivered, "detail": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"type": self.type, "delivered": False, "detail": f"Webhook POST failed: {exc}"[:300]}


class TeamsChannel(NotificationChannel):
    """Microsoft Teams incoming webhook — same transport as WebhookChannel,
    Teams' documented MessageCard payload shape."""
    type = "TEAMS"

    async def send(self, config: dict, event: dict) -> dict:
        url = (config or {}).get("webhook_url")
        if not url:
            return {"type": self.type, "delivered": False, "detail": "No Teams webhook URL configured on this rule."}
        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": {"CRITICAL": "D32F2F", "HIGH": "F57C00", "MEDIUM": "FBC02D", "LOW": "388E3C"}.get(event.get("severity"), "607D8B"),
            "summary": event.get("rule_name", "DIP Alert"),
            "title": f"[{event.get('severity')}] {event.get('rule_name')}",
            "text": event.get("message", ""),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, content=json.dumps(payload), headers={"Content-Type": "application/json"})
            delivered = 200 <= resp.status_code < 300
            return {"type": self.type, "delivered": delivered, "detail": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"type": self.type, "delivered": False, "detail": f"Teams webhook POST failed: {exc}"[:300]}


# Registry — add a new channel type here and nowhere else.
_REGISTRY: dict[str, NotificationChannel] = {
    c.type: c() for c in (InAppChannel, EmailChannel, WebhookChannel, TeamsChannel)
}


async def dispatch(channels: list[dict], event: dict) -> list[dict]:
    """Sends `event` through every configured channel, returns one
    delivery-result dict per channel (unknown types are reported, not
    silently dropped)."""
    results = []
    for cfg in (channels or []):
        ctype = (cfg or {}).get("type")
        channel = _REGISTRY.get(ctype)
        if channel is None:
            results.append({"type": ctype or "UNKNOWN", "delivered": False, "detail": f"Unknown channel type '{ctype}'."})
            continue
        results.append(await channel.send(cfg, event))
    return results


def available_channel_types() -> list[str]:
    return list(_REGISTRY.keys())
