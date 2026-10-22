"""
verification_status.py — single source of truth for turning a result's
independent inputs (format validity, the provider's own detailed outcome
code, and identity/entity-match comparison) into the one coarse,
dashboard/rules-facing OverallVerificationStatus — per spec section 14's
explicit requirement that missing data, a provider outage, format
validation alone, or AI/extraction confidence must never be equated with
a successful verification.

Every verification service (identity_verification_service.py today;
tax_verification_service.py / company_verification_service.py /
udyam_verification_service.py / moa_verification_service.py in later
phases) calls derive_result_overall_status() rather than deciding this ad
hoc — this is the ONE place that interpretation lives, so every check
type's "VERIFIED" means the same thing on the Dashboard regardless of how
differently each one's own provider_status_code vocabulary is shaped.
"""
from app.models.india_verification import OverallVerificationStatus

# Provider status codes that mean "no attempt/answer was possible" —
# distinct from the provider actively returning a negative finding.
_PENDING_CODES = {"PENDING"}
_UNAVAILABLE_CODES = {"UNAVAILABLE"}
_ERROR_CODES = {"ERROR"}

# A provider actively confirming the identifier/registration doesn't
# verify, is inactive, or wasn't found — a definitive, non-ambiguous
# negative finding that still needs a human decision (could be a typo,
# could be fraud) — never silently treated as either pass or outright
# failure.
_NEGATIVE_FINDING_CODES = {
    "NOT_VERIFIED", "INACTIVE_OR_INVALID", "NOT_FOUND", "INACTIVE_OR_DISSOLVED",
    "AUTHENTICITY_NOT_ESTABLISHED", "REQUIRES_MANUAL_REVIEW",
    # Aadhaar-specific — consent/purpose wasn't captured before the check
    # was attempted (spec section 5's own outcome for exactly this case).
    # Grouped with the other "needs a human to act before this can
    # proceed" codes rather than NOT_CONFIGURED, since a provider IS
    # configured here; it's the request itself that's incomplete.
    "CONSENT_REQUIRED",
    # MCA-specific — several real candidates, none auto-picked (spec
    # section 9's explicit requirement). Grouped here rather than in
    # _MISMATCH_CODES below: nothing is confirmed WRONG, a human just
    # needs to pick which candidate is right — the same "needs a person,
    # not a definitive failure" semantics as NOT_FOUND/CONSENT_REQUIRED,
    # not the "we compared and it didn't match" semantics MISMATCH means.
    "AMBIGUOUS_MATCH",
}
# A provider (or our own comparison logic) found a record but it doesn't
# match what was submitted.
_MISMATCH_CODES = {"NAME_MISMATCH", "MISMATCH", "MISMATCH_FOUND"}
_POSITIVE_CODES = {
    "VERIFIED", "REGISTERED_AND_VERIFIED", "DOCUMENT_TYPE_CONFIRMED",
    "CONTENT_EXTRACTED", "COMPLETENESS_CHECK_PASSED", "REGISTRY_DETAILS_MATCHED",
}


def derive_result_overall_status(
    *, format_status: str, provider_status_code: str | None, match_status: str | None = None,
) -> OverallVerificationStatus:
    if format_status == "FORMAT_INVALID":
        return OverallVerificationStatus.FAILED
    if provider_status_code is None:
        return OverallVerificationStatus.NOT_STARTED
    if provider_status_code == "NOT_CONFIGURED":
        return OverallVerificationStatus.NOT_CONFIGURED
    if provider_status_code in _PENDING_CODES:
        return OverallVerificationStatus.PENDING_PROVIDER
    if provider_status_code in _UNAVAILABLE_CODES:
        return OverallVerificationStatus.UNAVAILABLE
    if provider_status_code in _ERROR_CODES:
        return OverallVerificationStatus.FAILED
    if match_status == "MISMATCH" or provider_status_code in _MISMATCH_CODES:
        return OverallVerificationStatus.MISMATCH
    if provider_status_code in _NEGATIVE_FINDING_CODES:
        return OverallVerificationStatus.REQUIRES_REVIEW
    if provider_status_code in _POSITIVE_CODES:
        return OverallVerificationStatus.VERIFIED
    # An unrecognized code (e.g. a future check type's provider returning
    # something not yet added to the sets above) fails safe to
    # REQUIRES_REVIEW rather than silently treating it as verified.
    return OverallVerificationStatus.REQUIRES_REVIEW


# Worst-status-wins priority — the default, rule-free case roll-up used
# until a VerificationRule exists for this org/business_process (Phase
# Group 4 adds real rule-driven derivation). Same "no rule configured ==
# safe, non-silent default" precedent as PAY22's AUTO_APPROVED-when-
# unconfigured behavior for Approval Rules — never a silent pass.
_CASE_PRIORITY = [
    OverallVerificationStatus.FAILED,
    OverallVerificationStatus.MISMATCH,
    OverallVerificationStatus.REQUIRES_REVIEW,
    OverallVerificationStatus.UNAVAILABLE,
    OverallVerificationStatus.PENDING_PROVIDER,
    OverallVerificationStatus.NOT_CONFIGURED,
    OverallVerificationStatus.IN_PROGRESS,
    OverallVerificationStatus.NOT_STARTED,
    OverallVerificationStatus.PARTIALLY_VERIFIED,
    OverallVerificationStatus.VERIFIED_WITH_WARNINGS,
    OverallVerificationStatus.VERIFIED,
]


def derive_case_overall_status(
    result_statuses: list[OverallVerificationStatus], *,
    required_check_types: set[str] | None = None, present_check_types: set[str] | None = None,
) -> OverallVerificationStatus:
    """`required_check_types`/`present_check_types` (Phase Group 4, IV20)
    are the rule-aware completeness check this module's own prior comment
    previewed: resolved from verification_rules_service against this
    case's business_process — see evaluate_case_compliance(). Without
    them (no applicable VerificationRule, or an older call site that
    predates this), behavior is unchanged from Phase 1-3."""
    if not result_statuses:
        return OverallVerificationStatus.NOT_STARTED
    distinct = set(result_statuses)
    # A mix of "done" and "not started yet" statuses means the case is
    # genuinely in progress, not simply "not started" — checked before
    # the worst-wins ladder so a case with one VERIFIED result and one
    # NOT_STARTED result reads as IN_PROGRESS, not NOT_STARTED.
    if OverallVerificationStatus.NOT_STARTED in distinct and len(distinct) > 1:
        return OverallVerificationStatus.IN_PROGRESS
    base = OverallVerificationStatus.REQUIRES_REVIEW
    for status in _CASE_PRIORITY:
        if status in distinct:
            base = status
            break

    # A case can't honestly read as fully VERIFIED/VERIFIED_WITH_WARNINGS
    # while an applicable rule still requires a check_type that hasn't
    # even been run yet — that's a checklist that's merely in progress,
    # not complete. PARTIALLY_VERIFIED exists in the enum for exactly
    # this: distinct from IN_PROGRESS (which means "nothing usable yet"),
    # distinct from VERIFIED (which means "nothing outstanding").
    if required_check_types and base in (OverallVerificationStatus.VERIFIED, OverallVerificationStatus.VERIFIED_WITH_WARNINGS):
        missing = required_check_types - (present_check_types or set())
        if missing:
            return OverallVerificationStatus.PARTIALLY_VERIFIED
    return base
