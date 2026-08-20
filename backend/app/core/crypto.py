"""
Shared secret encryption — Fernet, keyed off the app's existing
settings.secret_key (sha256 -> base64), so no second secret needs to be
provisioned. Originally lived only in services/connection_service.py
(Connection.encrypted_secrets); extracted here so every module that needs
to store one encrypted value at rest — Connections, and now Seller SMTP
credentials (services/seller/seller_settings_service.py) — shares one
implementation instead of three copies drifting apart.

In production, rotate via a dedicated ENCRYPTION_KEY env var if the app
secret ever needs to change independently of stored credentials.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


def _fernet() -> Fernet:
    settings = get_settings()
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token: str) -> str | None:
    """Returns None (never raises) on a token that doesn't decrypt — e.g.
    encrypted under an app secret that has since rotated."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
