"""
udyam_verification_service.py — MSME/Udyam registration verification
(Phase Group 2, IV10).

Udyam Registration Number format (per the Udyam Registration Portal's
own published shape): "UDYAM-XX-00-0000000" — XX a 2-letter state code,
00 a 2-digit district code, 0000000 a 7-digit sequence number. No public
checksum exists beyond this structure.

Per spec section 10, three distinct concepts this module never conflates:
  1. MSME classification (micro/small/medium) — comes ONLY from a real
     Udyam registration lookup, never inferred from a company's name,
     PAN, GSTIN, or a submitted claim.
  2. Company incorporation (MCA) — a separate check entirely (see
     company_verification_service.py); an enterprise can be Udyam-
     registered without being an incorporated company (e.g. a sole
     proprietorship), and an incorporated company is not automatically
     an MSME.
  3. Startup recognition (DPIIT) — explicitly out of scope for this
     phase (spec section 10's own instruction: "if startup recognition
     is needed in a later phase, make it a separate configurable
     verification capability" — not folded into Udyam here).
"""
from __future__ import annotations

import asyncio
import re

_UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")


def validate_udyam_format(udyam_number: str) -> dict:
    candidate = (udyam_number or "").strip().upper()
    if not _UDYAM_RE.match(candidate):
        return {"format_status": "FORMAT_INVALID"}
    return {"format_status": "FORMAT_VALID", "state_code": candidate.split("-")[1]}


def mask_udyam(udyam_number: str) -> str:
    """Udyam numbers are a public registry identifier (not sensitive
    like PAN/Aadhaar) — shown in full rather than masked, same
    treatment as GSTIN's state-code prefix."""
    return (udyam_number or "").strip().upper()


async def verify_udyam(connector, udyam_number: str, *, submitted_name: str | None = None, consent_reference: str | None = None, purpose: str | None = None) -> dict:
    provider_result = await asyncio.to_thread(connector.verify_udyam, udyam_number, submitted_name=submitted_name)
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
            "enterprise_name": provider_result.get("enterprise_name"),
            "classification": provider_result.get("classification"),
            "registration_date": provider_result.get("registration_date"),
        },
        "provider_reference": provider_result.get("reference_id"),
        "verified_at": provider_result.get("verified_at"),
    }
