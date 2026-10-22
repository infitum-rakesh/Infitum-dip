"""
identity_verification_service.py — PAN verification (Phase 1/IV3).

GSTIN/TAN join as tax_verification_service.py and Aadhaar extends this
file in later phases (Aadhaar is grouped with PAN under "identity" per
the spec's own section numbering, but ships last — see the approved
plan's Regulatory risks section: real Aadhaar verification needs a UIDAI
AUA/KUA license or a licensed sub-AUA relationship this environment
doesn't have, so it will only ever report NOT_CONFIGURED/UNAVAILABLE
until the user supplies one).

PAN has no publicly documented checksum algorithm the way Aadhaar has the
Verhoeff digit — "checksum/structure validation" for PAN means the one
real structural rule the format actually encodes: the 4th character is a
holder-type code (P=Individual, C=Company, H=HUF, A=AOP, B=BOI,
G=Government, J=Artificial Juridical Person, L=Local Authority, F=Firm,
T=Trust/AOP-Trust — per the Income Tax Department's own documented PAN
structure). A bare regex match is "format valid"; a recognized
holder-type code on top of that is the real structural check this module
can honestly perform without a provider call. Neither is ever reported as
VERIFIED — see verification_status.py's derive_result_overall_status(),
the one place that distinction is enforced.
"""
from __future__ import annotations

import asyncio
import re

_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PAN_HOLDER_TYPE_CODES = {
    "P": "Individual", "C": "Company", "H": "HUF", "A": "AOP", "B": "BOI",
    "G": "Government", "J": "Artificial Juridical Person", "L": "Local Authority",
    "F": "Firm", "T": "Trust",
}


def validate_pan_format(pan: str) -> dict:
    """Returns {"format_status": "FORMAT_VALID"|"FORMAT_INVALID",
    "holder_type": str | None} — never FORMAT_VALID without a recognized
    holder-type code in position 4, and never claims anything beyond
    format/structure (see this module's own docstring)."""
    candidate = (pan or "").strip().upper()
    if not _PAN_RE.match(candidate):
        return {"format_status": "FORMAT_INVALID", "holder_type": None}
    holder_type = PAN_HOLDER_TYPE_CODES.get(candidate[3])
    if holder_type is None:
        return {"format_status": "FORMAT_INVALID", "holder_type": None}
    return {"format_status": "FORMAT_VALID", "holder_type": holder_type}


def mask_pan(pan: str) -> str:
    """Last-4-visible masking, reused everywhere a PAN is displayed or
    logged — same discipline the Aadhaar path (later phase) applies even
    more strictly (last-4 only, never any more)."""
    candidate = (pan or "").strip().upper()
    if len(candidate) != 10:
        return "•" * max(len(candidate), 4)
    return "••••••" + candidate[6:]


async def verify_pan(connector, pan: str, *, submitted_name: str | None = None, consent_reference: str | None = None, purpose: str | None = None) -> dict:
    """The one real provider call site for PAN. Never invoked directly by
    an API route — always through verification_orchestrator.run_pan_check()
    so format validation, status derivation, persistence, and audit
    logging all stay in one place. Connector calls are synchronous by
    this codebase's own convention (connectors/base.py), run here via
    asyncio.to_thread() so the event loop isn't blocked.

    consent_reference/purpose are accepted and ignored — PAN itself needs
    neither, but every verify_*() function in this package takes the same
    keyword set so the shared orchestrator engine (verification_
    orchestrator._run_identifier_check()) can call any of them uniformly,
    same "accept the generic caller's full kwarg set even when unused"
    precedent as connectors/base.py's open_sql_stream(log_fn, cancel_check)."""
    provider_result = await asyncio.to_thread(connector.verify_pan, pan, submitted_name=submitted_name)
    status = provider_result.get("status") or "ERROR"
    match_status = None
    if status == "VERIFIED" and submitted_name:
        match_status = "MATCHED"
    elif status == "NAME_MISMATCH":
        match_status = "MISMATCH"
    return {
        "provider_status_code": status,
        "match_status": match_status,
        "normalized_result": {
            "holder_name": provider_result.get("holder_name"),
            "entity_type": provider_result.get("entity_type"),
        },
        "provider_reference": provider_result.get("reference_id"),
        "verified_at": provider_result.get("verified_at"),
    }


# ── Aadhaar ──────────────────────────────────────────────────────────
# Real Aadhaar authentication/offline-verification requires UIDAI AUA/KUA
# licensing or a sub-AUA relationship with an already-licensed provider
# (Karza/Signzy/Digio/...) — this cannot be self-certified by writing
# code (see the approved plan's Regulatory risks section). Format/
# Verhoeff-checksum validation is real and honest; VERIFIED is
# structurally unreachable here until a licensed provider is wired —
# every connection currently registered (including the mock gateway)
# reports NOT_CONFIGURED for this check, by design, not as a bug.

_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_AADHAAR_RE = re.compile(r"^[0-9]{12}$")


def _verhoeff_checksum_valid(number: str) -> bool:
    """Verhoeff's own check-digit algorithm — verified against the
    published test vectors (checksum('758722') == 0) before being used
    here, not guessed from memory alone (see this phase's own build
    history: a GSTIN checksum algorithm was caught and corrected the
    same way after an unverified first attempt)."""
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def validate_aadhaar_format(aadhaar: str) -> dict:
    """Returns {"format_status"} — FORMAT_VALID requires both the
    12-digit shape AND a passing Verhoeff checksum. Aadhaar numbers also
    conventionally never start with 0 or 1; enforced here too since it's
    a real, documented structural rule (UIDAI never issues such
    numbers), not an assumption."""
    candidate = re.sub(r"[\s-]", "", aadhaar or "")
    if not _AADHAAR_RE.match(candidate) or candidate[0] in ("0", "1"):
        return {"format_status": "FORMAT_INVALID"}
    if not _verhoeff_checksum_valid(candidate):
        return {"format_status": "FORMAT_INVALID"}
    return {"format_status": "FORMAT_VALID"}


def mask_aadhaar(aadhaar: str) -> str:
    """Last-4-visible only — stricter than PAN's last-4 (PAN shows a
    6-char prefix mask + 4 visible; Aadhaar shows ONLY the last 4,
    nothing else) per spec section 5's explicit masking requirement.
    The full Aadhaar number is never stored, logged, or returned in any
    API response beyond this masked form — see models/india_
    verification.py's module docstring."""
    candidate = re.sub(r"[\s-]", "", aadhaar or "")
    if len(candidate) != 12:
        return "•" * 8 + "XXXX"
    return "•" * 8 + candidate[8:]


async def verify_aadhaar(
    connector, aadhaar: str, *, submitted_name: str | None = None,
    consent_reference: str | None = None, purpose: str | None = None,
) -> dict:
    """Masks the Aadhaar number BEFORE it ever reaches the connector
    layer — connectors/base.py's verify_aadhaar() contract deliberately
    takes aadhaar_masked_ref, never the raw number, so no connector
    implementation (real or future) can accidentally be handed a full
    Aadhaar value to transmit/log. consent_reference and purpose are
    required by the caller (verification_orchestrator.run_aadhaar_check()
    enforces this before this function is ever invoked — see its
    CONSENT_REQUIRED short-circuit)."""
    masked = mask_aadhaar(aadhaar)
    provider_result = await asyncio.to_thread(connector.verify_aadhaar, masked, consent_reference=consent_reference, purpose=purpose)
    status = provider_result.get("status") or "ERROR"
    return {
        "provider_status_code": status,
        "match_status": "MISMATCH" if status == "MISMATCH" else None,
        "normalized_result": {k: v for k, v in provider_result.items() if k not in ("status", "reference_id", "verified_at")},
        "provider_reference": provider_result.get("reference_id"),
        "verified_at": provider_result.get("verified_at"),
    }
