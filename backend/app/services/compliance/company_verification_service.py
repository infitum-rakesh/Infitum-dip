"""
company_verification_service.py — MCA company/LLP registration search
(Phase Group 2, IV9).

Unlike every other check in this module (PAN/GSTIN/TAN/Udyam/Aadhaar —
exactly one identifier, exactly one outcome), an MCA lookup by legal name
can genuinely return several real candidates. This service NEVER auto-
picks an uncertain match (spec section 9's explicit requirement) — a
legal-name search with more than one hit is reported as AMBIGUOUS_MATCH
with every candidate attached, and the caller (verification_orchestrator.
run_mca_check(), the API, the UI) must let a human choose.

CIN format (per MCA's own published structure):
  [0]     listing status: U (unlisted) or L (listed)
  [1:6]   5-digit industry/activity code
  [6:8]   2-letter state code (e.g. MH, DL — MCA's own abbreviations, a
          different code space from GST_STATE_CODES in
          tax_verification_service.py, which uses 2-digit GST codes)
  [8:12]  4-digit year of incorporation
  [12:15] 3-letter ownership type (PTC/PLC/GOI/SGC/NPL/FTC/FLC/OPC/...)
  [15:21] 6-digit registration sequence number

LLPIN format is 7 characters (3 letters + 4 digits) per MCA's published
shape — structurally checked here, but (like TAN) no public checksum
algorithm exists to validate beyond that shape.
"""
from __future__ import annotations

import re

_CIN_RE = re.compile(r"^[UL][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
_LLPIN_RE = re.compile(r"^[A-Z]{3}[0-9]{4}$")

# Statuses a real MCA record can carry that mean the entity is no longer
# a going concern — distinct from a genuinely active registration. Not
# an exhaustive legal taxonomy, but the common real MCA status values.
_DISSOLVED_STATUSES = {"Strike Off", "Dissolved", "Amalgamated", "Converted", "Liquidated", "Under Liquidation"}


def validate_mca_identifier_format(*, cin: str | None = None, llpin: str | None = None, legal_name: str | None = None) -> dict:
    """At least one of cin/llpin/legal_name must be given; whichever
    identifier(s) are given must themselves be structurally valid.
    Returns {"format_status", "search_mode"}."""
    cin = (cin or "").strip().upper() or None
    llpin = (llpin or "").strip().upper() or None
    legal_name = (legal_name or "").strip() or None

    if cin:
        return {"format_status": "FORMAT_VALID" if _CIN_RE.match(cin) else "FORMAT_INVALID", "search_mode": "CIN"}
    if llpin:
        return {"format_status": "FORMAT_VALID" if _LLPIN_RE.match(llpin) else "FORMAT_INVALID", "search_mode": "LLPIN"}
    if legal_name:
        return {"format_status": "FORMAT_VALID", "search_mode": "LEGAL_NAME"}
    return {"format_status": "FORMAT_INVALID", "search_mode": None}


async def search_company(connector, *, cin: str | None = None, llpin: str | None = None, legal_name: str | None = None) -> dict:
    """The one real provider call site for MCA search. Returns
    {"provider_status_code", "matches": [...], "verified_at"} —
    provider_status_code is translated from the connector's raw
    FOUND/NOT_FOUND/AMBIGUOUS/ERROR/UNAVAILABLE into spec section 9's
    canonical vocabulary (REGISTERED_AND_VERIFIED/NOT_FOUND/
    INACTIVE_OR_DISSOLVED/AMBIGUOUS_MATCH/UNAVAILABLE/ERROR) here, the
    one place that translation happens."""
    import asyncio
    provider_result = await asyncio.to_thread(connector.search_mca_company, cin=cin, llpin=llpin, legal_name=legal_name)
    raw_status = provider_result.get("status") or "ERROR"
    matches = provider_result.get("matches") or []
    verified_at = matches[0].get("verified_at") if matches else None

    if raw_status in ("ERROR", "UNAVAILABLE", "NOT_FOUND"):
        status = raw_status
    elif raw_status == "AMBIGUOUS":
        status = "AMBIGUOUS_MATCH"
    elif raw_status == "FOUND" and len(matches) == 1:
        status = "INACTIVE_OR_DISSOLVED" if matches[0].get("status") in _DISSOLVED_STATUSES else "REGISTERED_AND_VERIFIED"
    elif raw_status == "FOUND" and len(matches) > 1:
        # A connector that returns FOUND with multiple matches for an
        # exact-identifier (CIN/LLPIN) lookup would be a provider bug —
        # treated as ambiguous rather than silently picking one, same
        # never-auto-pick discipline as a legal-name search.
        status = "AMBIGUOUS_MATCH"
    else:
        status = "ERROR"

    return {"provider_status_code": status, "matches": matches, "verified_at": verified_at}
