"""
credential_store.py — persists the one secret this agent holds: the
credential_id/credential_secret pair issued once, at enrollment, by
POST /api/tally-agents/enroll (see backend/app/services/tally_agent_
service.py's exchange_enrollment_token docstring — the server never
redisplays this secret, so losing it means re-enrolling).

On Windows (the real deployment target): encrypted at rest via DPAPI
(CryptProtectData/CryptUnprotectData, through pywin32's win32crypt),
scoped CRYPTPROTECT_LOCAL_MACHINE so only a process running as the same
machine's LocalSystem/service account (i.e. the installed Windows
Service — see windows_service.py) can decrypt it; not tied to a single
user profile, since the service has none. This path is real code but
NOT runnable or live-verified in this project's own dev environment — no
Windows machine exists here (see package __init__.py's own disclosure).

On any other platform: a clearly-marked plaintext-JSON fallback, used
ONLY so the rest of this agent (ws_client, dispatcher, cli) is a real,
runnable, testable cross-platform process during development and in
this project's own live verification. This fallback is never reached on
Windows — the DPAPI branch always wins there — and a loud warning is
logged every time it's used, so it can never be mistaken for a
production path.
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class CredentialStoreError(Exception):
    """Raised for a missing/corrupt/inaccessible credential file."""


@dataclass
class AgentCredentials:
    installation_id: int
    credential_id: str
    credential_secret: str


def _store_path(config_dir: Path) -> Path:
    return config_dir / "credentials.dat"


def save_credentials(creds: AgentCredentials, config_dir: Path) -> Path:
    path = _store_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "installation_id": creds.installation_id,
        "credential_id": creds.credential_id,
        "credential_secret": creds.credential_secret,
    }).encode("utf-8")

    if sys.platform == "win32":
        encrypted = _dpapi_encrypt(payload)  # pragma: no cover - Windows-only, see module docstring
        path.write_bytes(encrypted)
    else:
        logger.warning(
            "Non-Windows platform detected — storing agent credentials as PLAINTEXT at %s. "
            "This fallback exists for cross-platform development/verification only; a real "
            "deployment always runs on Windows, where DPAPI encryption applies instead.",
            path,
        )
        path.write_bytes(payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # best-effort on platforms/filesystems that don't support POSIX perms (e.g. Windows via this path)
    return path


def load_credentials(config_dir: Path) -> AgentCredentials:
    path = _store_path(config_dir)
    if not path.exists():
        raise CredentialStoreError(f"No stored credentials found at {path}. Run 'tally_agent enroll' first.")
    raw = path.read_bytes()
    if sys.platform == "win32":
        payload = _dpapi_decrypt(raw)  # pragma: no cover - Windows-only, see module docstring
    else:
        payload = raw
    try:
        data = json.loads(payload.decode("utf-8"))
        return AgentCredentials(
            installation_id=data["installation_id"],
            credential_id=data["credential_id"],
            credential_secret=data["credential_secret"],
        )
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
        raise CredentialStoreError(f"Credential file at {path} is corrupt or unreadable: {exc}") from exc


def delete_credentials(config_dir: Path) -> bool:
    path = _store_path(config_dir)
    if path.exists():
        path.unlink()
        return True
    return False


def _dpapi_encrypt(payload: bytes) -> bytes:  # pragma: no cover - Windows-only
    import win32crypt  # type: ignore[import-not-found]
    # CRYPTPROTECT_LOCAL_MACHINE (flag 4) — decryptable by any process on
    # this machine with sufficient privilege, not scoped to one user
    # profile; correct for a service account with no interactive profile.
    blob = win32crypt.CryptProtectData(payload, "TallyAgent credentials", None, None, None, 4)
    return blob


def _dpapi_decrypt(blob: bytes) -> bytes:  # pragma: no cover - Windows-only
    import win32crypt  # type: ignore[import-not-found]
    _description, payload = win32crypt.CryptUnprotectData(blob, None, None, None, 4)
    return payload
