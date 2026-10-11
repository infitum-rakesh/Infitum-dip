"""
Email provider registry — the single place that maps a stored "provider"
string -> a real EmailProvider implementation. Mirrors
payment_gateways/registry.py's own shape exactly: starts with whichever
adapters exist, gains one import + one dict entry per new provider,
never a placeholder/fake entry.
"""
from app.email_providers.base import EmailProvider
from app.email_providers.resend_provider import ResendProvider
from app.email_providers.smtp_provider import SMTPProvider

_REGISTRY: dict[str, type[EmailProvider]] = {
    "SMTP": SMTPProvider,
    "RESEND": ResendProvider,
}


def get_provider(provider_type: str, config: dict) -> EmailProvider | None:
    cls = _REGISTRY.get((provider_type or "SMTP").upper())
    if cls is None:
        return None
    return cls(config)


def registered_providers() -> list[str]:
    return list(_REGISTRY.keys())
