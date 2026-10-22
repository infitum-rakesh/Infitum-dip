"""
tax_verification_service.py — GSTIN + TAN verification (Phase Group 2,
IV7-IV8).

GSTIN checksum: a mod-36 Luhn-style check digit over the first 14
characters, mapped back through the base-36 alphabet "0-9A-Z". This
implementation is verified (not guessed) against two independently
published real GSTIN examples before being trusted here:
"27AAACR5055K1Z" -> check digit "7", and Central Warehousing
Corporation's real GSTIN "33AAACC1206D1Z" -> check digit "N" — both
confirmed programmatically (see this module's own test coverage). GSTIN
structure (per GSTN's own published format) is:
  [0:2]  state code (2 digits, see GST_STATE_CODES)
  [2:12] the registrant's 10-character PAN
  [12]   entity/registration count for this PAN within the state (1-9,A-Z)
  [13]   fixed "Z" (reserved by GSTN for current registrations)
  [14]   check digit (computed, see above)

TAN has no publicly documented checksum algorithm (same situation as
PAN — see identity_verification_service.py's own docstring) — "format
validation" for TAN means the one real structural rule the format
actually encodes: 4 letters (jurisdiction + initial of holder's name) +
5 digits (unique sequence) + 1 check letter the Income Tax Department
computes but does not publish an open algorithm for. A regex match is
"format valid"; nothing stronger is claimed.
"""
from __future__ import annotations

import asyncio
import re

_CODE_POINT_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _gstin_check_digit(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        digit = _CODE_POINT_CHARS.index(ch)
        factor = 1 if i % 2 == 0 else 2
        product = digit * factor
        total += product // 36 + product % 36
    return _CODE_POINT_CHARS[(36 - total % 36) % 36]


# Official GST state/UT jurisdiction codes (CBIC-published list).
GST_STATE_CODES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan",
    "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand",
    "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman & Diu", "26": "Dadra & Nagar Haveli", "27": "Maharashtra",
    "28": "Andhra Pradesh (Old)", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep",
    "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}

_GSTIN_RE = re.compile(r"^([0-9]{2})([A-Z0-9]{10})([0-9A-Z])Z([0-9A-Z])$")


def validate_gstin_format(gstin: str) -> dict:
    """Returns {"format_status", "state_code", "state_name",
    "embedded_pan", "entity_code"} — FORMAT_VALID requires the state
    code to be a real GST jurisdiction, the embedded 10 characters to be
    a structurally valid PAN (see identity_verification_service), AND
    the published check-digit algorithm to match the GSTIN's own 15th
    character. Any one failing means FORMAT_INVALID — never a partial
    pass reported as valid."""
    from app.services.compliance.identity_verification_service import validate_pan_format

    candidate = (gstin or "").strip().upper()
    match = _GSTIN_RE.match(candidate)
    if not match:
        return {"format_status": "FORMAT_INVALID", "state_code": None, "state_name": None, "embedded_pan": None, "entity_code": None}

    state_code, embedded_pan, entity_code, given_check = match.groups()
    if state_code not in GST_STATE_CODES:
        return {"format_status": "FORMAT_INVALID", "state_code": state_code, "state_name": None, "embedded_pan": embedded_pan, "entity_code": entity_code}

    pan_info = validate_pan_format(embedded_pan)
    if pan_info["format_status"] != "FORMAT_VALID":
        return {"format_status": "FORMAT_INVALID", "state_code": state_code, "state_name": GST_STATE_CODES[state_code], "embedded_pan": embedded_pan, "entity_code": entity_code}

    expected_check = _gstin_check_digit(candidate[:14])
    if expected_check != given_check:
        return {"format_status": "FORMAT_INVALID", "state_code": state_code, "state_name": GST_STATE_CODES[state_code], "embedded_pan": embedded_pan, "entity_code": entity_code}

    return {
        "format_status": "FORMAT_VALID", "state_code": state_code, "state_name": GST_STATE_CODES[state_code],
        "embedded_pan": embedded_pan, "entity_code": entity_code,
    }


def mask_gstin(gstin: str) -> str:
    """Keeps the state code (public jurisdiction info, not sensitive)
    and last 3 characters visible; masks the embedded PAN + entity code
    in between — the PAN component is exactly the sensitive part."""
    candidate = (gstin or "").strip().upper()
    if len(candidate) != 15:
        return "•" * max(len(candidate), 4)
    return candidate[:2] + "•" * 10 + candidate[12:]


# A found-and-confirmed GSTIN record whose registration_status is
# anything but one of these is not a currently-active registration —
# spec section 7's own explicit requirement to distinguish "live-
# verified" from "active registration" ("Suspended or cancelled
# registration, if confirmed by the provider"). Caught here rather than
# left to the caller: a provider genuinely returning status="VERIFIED"
# (record found) for a cancelled registration must never be reported as
# simply VERIFIED by this codebase.
_ACTIVE_GST_STATUSES = {"Active", "Provisional"}


async def verify_gstin(connector, gstin: str, *, submitted_name: str | None = None, consent_reference: str | None = None, purpose: str | None = None) -> dict:
    """The one real provider call site for GSTIN — see
    identity_verification_service.verify_pan()'s docstring for why this
    always goes through asyncio.to_thread()."""
    provider_result = await asyncio.to_thread(connector.verify_gstin, gstin, submitted_name=submitted_name)
    status = provider_result.get("status") or "ERROR"
    registration_status = provider_result.get("registration_status")
    if status == "VERIFIED" and registration_status is not None and registration_status not in _ACTIVE_GST_STATUSES:
        status = "INACTIVE_OR_INVALID"
    match_status = None
    if status == "VERIFIED" and submitted_name:
        match_status = "MATCHED"
    elif status == "NAME_MISMATCH":
        match_status = "MISMATCH"
    return {
        "provider_status_code": status,
        "match_status": match_status,
        "normalized_result": {
            "legal_name": provider_result.get("legal_name"),
            "trade_name": provider_result.get("trade_name"),
            "registration_status": provider_result.get("registration_status"),
            "registration_date": provider_result.get("registration_date"),
            "principal_place_of_business": provider_result.get("principal_place_of_business"),
        },
        "provider_reference": provider_result.get("reference_id"),
        "verified_at": provider_result.get("verified_at"),
    }


# ── TAN ──────────────────────────────────────────────────────────────

_TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")


def validate_tan_format(tan: str) -> dict:
    """Returns {"format_status"} — format-only, see this module's own
    docstring for why no checksum is claimed for TAN."""
    candidate = (tan or "").strip().upper()
    if not _TAN_RE.match(candidate):
        return {"format_status": "FORMAT_INVALID"}
    return {"format_status": "FORMAT_VALID"}


def mask_tan(tan: str) -> str:
    candidate = (tan or "").strip().upper()
    if len(candidate) != 10:
        return "•" * max(len(candidate), 4)
    return candidate[:3] + "•" * 6 + candidate[9:]


async def verify_tan(connector, tan: str, *, submitted_name: str | None = None, consent_reference: str | None = None, purpose: str | None = None) -> dict:
    provider_result = await asyncio.to_thread(connector.verify_tan, tan, submitted_name=submitted_name)
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
            "deductor_name": provider_result.get("deductor_name"),
            "entity_type": provider_result.get("entity_type"),
        },
        "provider_reference": provider_result.get("reference_id"),
        "verified_at": provider_result.get("verified_at"),
    }
