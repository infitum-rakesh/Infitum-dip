"""
verification_orchestrator.py — Phase 1 (IV3) built PAN; Phase Group 2
(IV7-IV11) added GSTIN/TAN/MCA/Udyam/Aadhaar on the same shape.

Creates/updates a VerificationCase + VerificationRequest + (upserted)
VerificationResult for one check. Phase Group 4 (IV19-20) extends this to
the full Consent -> Format -> Provider -> MCA/Udyam Lookup -> Document
Intelligence -> Cross-Record Matching -> Rule Evaluation -> Review Queue
-> Final Report pipeline described in the spec; this phase only needs
Format -> Provider -> persisted Result, which is already a complete,
honest, non-fabricated outcome for a single identifier/registration check
— no rules engine exists yet to gate on, and no cross-record/document
comparison applies to one check in isolation.

Every outcome — including "no provider connection configured" — is
written as a real, persisted VerificationResult with a defensible
OverallVerificationStatus; nothing here silently no-ops without a row to
show for it, since the caller explicitly asked for a verification and
must get a real case/result back to query (contrast with, say,
tally_invoice_sync_service's quieter no-mapping-configured skip, which is
fine there because nothing explicitly asked for a sync).

`_run_identifier_check()` is the one generic engine every single-identifier
check (PAN/GSTIN/TAN/Udyam/Aadhaar) runs through — each public
run_*_check() is a thin, type-specific wrapper supplying that check's own
format validator / masking function / provider-call function. MCA is
NOT a single-identifier check (a legal-name search can return several
real candidates, never auto-picked) — see run_mca_check() below, which
has its own shape rather than being forced into this one.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, ConnectorUnsupportedError
from app.models.connection import Connection
from app.models.india_verification import (
    OverallVerificationStatus,
    VerificationCase,
    VerificationCheckType,
    VerificationRequest,
    VerificationRequestStatus,
    VerificationResult,
    VerificationUsage,
)
from app.services import connection_service
from app.services.compliance import company_verification_service, identity_verification_service, tax_verification_service, udyam_verification_service, verification_rules_service
from app.services.compliance.verification_status import derive_case_overall_status, derive_result_overall_status

_FRESH_STATUSES = {OverallVerificationStatus.VERIFIED, OverallVerificationStatus.VERIFIED_WITH_WARNINGS}


async def _rule_context(db: AsyncSession, case: VerificationCase) -> tuple[set, dict]:
    """Resolves the applicable VerificationRule once per check-run (Phase
    Group 4, IV20) and returns (required_check_types, freshness_days_by_
    check_type) — both empty when no rule applies, which is the normal,
    valid Phase 1-3 state. Centralized here rather than duplicated in
    each of _run_identifier_check()/run_mca_check() below."""
    rule = await verification_rules_service.resolve_applicable_rule(
        db, case.organization_id, business_process=case.business_process, subject_type=case.subject_type,
    )
    if rule is None:
        return set(), {}
    required = set(rule.required_check_types or [])
    freshness = {t: rule.freshness_days for t in required} if rule.freshness_days else {}
    return required, freshness


async def _cached_fresh_result(db: AsyncSession, case: VerificationCase, check_type: VerificationCheckType) -> VerificationResult | None:
    """Freshness/expiry-driven caching (Phase Group 4, IV20) — a result
    that's still within its expires_at horizon is returned as-is instead
    of spending another provider call. expires_at is only ever set when
    an applicable rule gave this check_type a freshness_days policy (see
    _rule_context()/the expires_at computation below) — absent a rule,
    nothing here ever short-circuits, same as Phase 1-3. The expires_at
    comparison is done in the WHERE clause (not against an already-
    loaded Python attribute) so it compares as stored ISO8601 UTC
    strings — SQLite drops tzinfo on round-trip, so a naive-vs-aware
    Python-side comparison would raise; see schedule_service.py's
    _as_utc() docstring for the same gotcha elsewhere in this codebase."""
    result = (await db.execute(
        select(VerificationResult).where(
            VerificationResult.case_id == case.id, VerificationResult.check_type == check_type,
            VerificationResult.overall_status.in_(_FRESH_STATUSES),
            VerificationResult.expires_at.is_not(None), VerificationResult.expires_at > datetime.now(timezone.utc),
        )
    )).scalar_one_or_none()
    return result


async def get_or_create_case(
    db: AsyncSession, *, organization_id: int, subject_type: str, subject_name: str,
    subject_id: int | None = None, business_process: str | None = None, requested_by: int | None = None,
) -> VerificationCase:
    case = VerificationCase(
        organization_id=organization_id, subject_type=subject_type, subject_name=subject_name,
        subject_id=subject_id, business_process=business_process, requested_by=requested_by,
    )
    db.add(case)
    await db.flush()
    return case


async def get_case(db: AsyncSession, *, organization_id: int, case_id: int) -> VerificationCase | None:
    case = await db.get(VerificationCase, case_id)
    if case is None or case.organization_id != organization_id:
        return None
    return case


async def _resolve_connection(db: AsyncSession, *, organization_id: int, connection_id: int | None) -> Connection | None:
    if connection_id is None:
        return None
    conn = await db.get(Connection, connection_id)
    if conn is None or conn.organization_id != organization_id or not conn.is_active:
        return None
    return conn


async def _run_identifier_check(
    db: AsyncSession, case: VerificationCase, *, check_type: VerificationCheckType, identifier: str,
    submitted_name: str | None, connection_id: int | None, actor_user_id: int | None,
    format_validator: Callable[[str], dict], mask_fn: Callable[[str], str],
    verify_fn: Callable[..., Awaitable[dict]],
    consent_reference: str | None = None, purpose: str | None = None, require_consent: bool = False,
    force_refresh: bool = False,
) -> VerificationResult:
    """The generic single-identifier engine — see this module's own
    docstring. `format_validator(identifier) -> dict` must return at
    least {"format_status": ...}; any other keys are merged into the
    result's normalized_result so format-derived detail (e.g. GSTIN's
    state_name, PAN's holder_type) survives even when no provider is
    configured. `verify_fn(connector, identifier, *, submitted_name=,
    consent_reference=, purpose=)` must return {"provider_status_code",
    "match_status", "normalized_result", "provider_reference",
    "verified_at"}, same contract across every check type — see
    identity_verification_service.verify_pan() for the reference shape.

    `require_consent=True` (Aadhaar only) short-circuits to a stored
    CONSENT_REQUIRED result — without even attempting to resolve a
    connection — when consent_reference/purpose weren't supplied; spec
    section 5's own explicit outcome for exactly this case.

    `force_refresh=False` (Phase Group 4, IV20): if this (case,
    check_type) already has an unexpired VERIFIED/VERIFIED_WITH_WARNINGS
    result, it's returned as-is — no new VerificationRequest, no provider
    call. Pass force_refresh=True to bypass this and re-verify anyway."""
    if not force_refresh:
        cached = await _cached_fresh_result(db, case, check_type)
        if cached is not None:
            return cached

    format_info = format_validator(identifier)
    masked = mask_fn(identifier)
    correlation_id = uuid.uuid4().hex
    idempotency_key = f"{case.id}:{check_type.value}:{masked}:{uuid.uuid4().hex[:8]}"

    prior_attempts = (await db.execute(
        select(VerificationRequest).where(VerificationRequest.case_id == case.id, VerificationRequest.check_type == check_type)
    )).scalars().all()

    request = VerificationRequest(
        organization_id=case.organization_id, case_id=case.id, check_type=check_type,
        connection_id=connection_id, attempt_number=len(prior_attempts) + 1, correlation_id=correlation_id,
        idempotency_key=idempotency_key, input_identifier_masked=masked, requested_by=actor_user_id,
        consent_reference=consent_reference, purpose=purpose,
        status=VerificationRequestStatus.RUNNING,
    )
    db.add(request)
    await db.flush()

    provider_status_code: str | None = None
    match_status: str | None = None
    normalized_result: dict = {k: v for k, v in format_info.items() if k != "format_status"}
    provider_reference: str | None = None
    verified_at_raw: str | None = None

    if format_info["format_status"] == "FORMAT_INVALID":
        request.status = VerificationRequestStatus.FAILED
        request.error_code = "FORMAT_INVALID"
    elif require_consent and not (consent_reference and purpose):
        provider_status_code = "CONSENT_REQUIRED"
        request.status = VerificationRequestStatus.SUCCEEDED
    else:
        connection = await _resolve_connection(db, organization_id=case.organization_id, connection_id=connection_id)
        if connection is None:
            provider_status_code = "NOT_CONFIGURED"
            request.status = VerificationRequestStatus.SUCCEEDED
        else:
            try:
                connector = connection_service.build_connector(connection)
                outcome = await verify_fn(connector, identifier, submitted_name=submitted_name, consent_reference=consent_reference, purpose=purpose)
                provider_status_code = outcome["provider_status_code"]
                match_status = outcome["match_status"]
                normalized_result = {**normalized_result, **outcome["normalized_result"]}
                provider_reference = outcome["provider_reference"]
                verified_at_raw = outcome["verified_at"]
                request.status = VerificationRequestStatus.SUCCEEDED
            except ConnectorUnsupportedError as exc:
                # The configured provider is real, but doesn't implement
                # this specific check — an architectural/licensing gap
                # (see this module's Aadhaar docstring), not an
                # operational failure. Reported the same as "no provider
                # configured" rather than as ERROR.
                provider_status_code = "NOT_CONFIGURED"
                request.status = VerificationRequestStatus.SUCCEEDED
                request.error_message = str(exc)[:500]
            except ConnectorError as exc:
                provider_status_code = "ERROR"
                request.status = VerificationRequestStatus.FAILED
                request.error_code = "PROVIDER_ERROR"
                request.error_message = str(exc)[:500]

    request.completed_at = datetime.now(timezone.utc)

    overall_status = derive_result_overall_status(
        format_status=format_info["format_status"], provider_status_code=provider_status_code, match_status=match_status,
    )

    result = (await db.execute(
        select(VerificationResult).where(VerificationResult.case_id == case.id, VerificationResult.check_type == check_type)
    )).scalar_one_or_none()
    if result is None:
        result = VerificationResult(organization_id=case.organization_id, case_id=case.id, check_type=check_type)
        db.add(result)
    else:
        result.retry_count += 1
    result.latest_request_id = request.id
    result.format_status = format_info["format_status"]
    result.provider_status_code = provider_status_code
    result.match_status = match_status
    result.overall_status = overall_status
    result.normalized_result = normalized_result
    result.provider_connection_id = connection_id
    result.provider_reference = provider_reference
    result.verified_at = datetime.fromisoformat(verified_at_raw) if verified_at_raw else None

    required_check_types, freshness_by_type = await _rule_context(db, case)
    freshness_days = freshness_by_type.get(check_type.value)
    result.expires_at = (result.verified_at + timedelta(days=freshness_days)) if (overall_status in _FRESH_STATUSES and result.verified_at and freshness_days) else None

    db.add(VerificationUsage(
        organization_id=case.organization_id, request_id=request.id, check_type=check_type,
        provider_connection_id=connection_id, outcome=overall_status.value,
    ))

    other_results = (await db.execute(
        select(VerificationResult).where(VerificationResult.case_id == case.id, VerificationResult.check_type != check_type)
    )).scalars().all()
    present_check_types = {r.check_type.value for r in other_results} | {check_type.value}
    case.overall_status = derive_case_overall_status(
        [r.overall_status for r in other_results] + [overall_status],
        required_check_types=required_check_types, present_check_types=present_check_types,
    )

    await db.commit()
    await db.refresh(result)
    # commit() expires every object touched in this transaction,
    # including `case` (its overall_status was just mutated above) — the
    # caller (api/compliance.py's _case_out()) serializes `case` right
    # after this returns, and an expired attribute access outside an
    # active await triggers a MissingGreenlet error, not a lazy reload.
    await db.refresh(case)
    return result


async def run_pan_check(
    db: AsyncSession, case: VerificationCase, *, pan: str, submitted_name: str | None = None,
    connection_id: int | None = None, actor_user_id: int | None = None, force_refresh: bool = False,
) -> VerificationResult:
    return await _run_identifier_check(
        db, case, check_type=VerificationCheckType.PAN, identifier=pan, submitted_name=submitted_name,
        connection_id=connection_id, actor_user_id=actor_user_id, force_refresh=force_refresh,
        format_validator=identity_verification_service.validate_pan_format,
        mask_fn=identity_verification_service.mask_pan, verify_fn=identity_verification_service.verify_pan,
    )


async def run_gstin_check(
    db: AsyncSession, case: VerificationCase, *, gstin: str, submitted_name: str | None = None,
    connection_id: int | None = None, actor_user_id: int | None = None, force_refresh: bool = False,
) -> VerificationResult:
    return await _run_identifier_check(
        db, case, check_type=VerificationCheckType.GSTIN, identifier=gstin, submitted_name=submitted_name,
        connection_id=connection_id, actor_user_id=actor_user_id, force_refresh=force_refresh,
        format_validator=tax_verification_service.validate_gstin_format,
        mask_fn=tax_verification_service.mask_gstin, verify_fn=tax_verification_service.verify_gstin,
    )


async def run_tan_check(
    db: AsyncSession, case: VerificationCase, *, tan: str, submitted_name: str | None = None,
    connection_id: int | None = None, actor_user_id: int | None = None, force_refresh: bool = False,
) -> VerificationResult:
    return await _run_identifier_check(
        db, case, check_type=VerificationCheckType.TAN, identifier=tan, submitted_name=submitted_name,
        connection_id=connection_id, actor_user_id=actor_user_id, force_refresh=force_refresh,
        format_validator=tax_verification_service.validate_tan_format,
        mask_fn=tax_verification_service.mask_tan, verify_fn=tax_verification_service.verify_tan,
    )


async def run_udyam_check(
    db: AsyncSession, case: VerificationCase, *, udyam_number: str, submitted_name: str | None = None,
    connection_id: int | None = None, actor_user_id: int | None = None, force_refresh: bool = False,
) -> VerificationResult:
    return await _run_identifier_check(
        db, case, check_type=VerificationCheckType.UDYAM, identifier=udyam_number, submitted_name=submitted_name,
        connection_id=connection_id, actor_user_id=actor_user_id, force_refresh=force_refresh,
        format_validator=udyam_verification_service.validate_udyam_format,
        mask_fn=udyam_verification_service.mask_udyam, verify_fn=udyam_verification_service.verify_udyam,
    )


async def run_mca_check(
    db: AsyncSession, case: VerificationCase, *, cin: str | None = None, llpin: str | None = None,
    legal_name: str | None = None, connection_id: int | None = None, actor_user_id: int | None = None,
    force_refresh: bool = False,
) -> VerificationResult:
    """MCA search is not a single-identifier check — a legal-name search
    can return zero, one, or several real candidates, and this function
    never auto-picks an uncertain match (spec section 9's own explicit
    requirement). See company_verification_service.search_company() for
    the AMBIGUOUS_MATCH/NOT_FOUND/REGISTERED_AND_VERIFIED distinction.
    force_refresh (Phase Group 4, IV20): same freshness-cache bypass as
    _run_identifier_check(); a cached MCA result is only ever returned
    when it was a single, confirmed match (REGISTERED_AND_VERIFIED) —
    never for an AMBIGUOUS_MATCH, which always needs a fresh human pick."""
    if not force_refresh:
        cached = await _cached_fresh_result(db, case, VerificationCheckType.MCA)
        if cached is not None:
            return cached

    identifier = cin or llpin or legal_name or ""
    masked = identifier  # CIN/LLPIN/company name are not sensitive like PAN/Aadhaar — shown in full
    correlation_id = uuid.uuid4().hex
    idempotency_key = f"{case.id}:{VerificationCheckType.MCA.value}:{masked}:{uuid.uuid4().hex[:8]}"

    prior_attempts = (await db.execute(
        select(VerificationRequest).where(VerificationRequest.case_id == case.id, VerificationRequest.check_type == VerificationCheckType.MCA)
    )).scalars().all()

    request = VerificationRequest(
        organization_id=case.organization_id, case_id=case.id, check_type=VerificationCheckType.MCA,
        connection_id=connection_id, attempt_number=len(prior_attempts) + 1, correlation_id=correlation_id,
        idempotency_key=idempotency_key, input_identifier_masked=masked, requested_by=actor_user_id,
        status=VerificationRequestStatus.RUNNING,
    )
    db.add(request)
    await db.flush()

    format_info = company_verification_service.validate_mca_identifier_format(cin=cin, llpin=llpin, legal_name=legal_name)

    provider_status_code: str | None = None
    normalized_result: dict = {k: v for k, v in format_info.items() if k != "format_status"}
    provider_reference: str | None = None
    verified_at_raw: str | None = None

    if format_info["format_status"] == "FORMAT_INVALID":
        request.status = VerificationRequestStatus.FAILED
        request.error_code = "FORMAT_INVALID"
    else:
        connection = await _resolve_connection(db, organization_id=case.organization_id, connection_id=connection_id)
        if connection is None:
            provider_status_code = "NOT_CONFIGURED"
            request.status = VerificationRequestStatus.SUCCEEDED
        else:
            try:
                connector = connection_service.build_connector(connection)
                outcome = await company_verification_service.search_company(connector, cin=cin, llpin=llpin, legal_name=legal_name)
                provider_status_code = outcome["provider_status_code"]
                normalized_result = {**normalized_result, "matches": outcome["matches"]}
                verified_at_raw = outcome["verified_at"]
                request.status = VerificationRequestStatus.SUCCEEDED
            except ConnectorUnsupportedError as exc:
                provider_status_code = "NOT_CONFIGURED"
                request.status = VerificationRequestStatus.SUCCEEDED
                request.error_message = str(exc)[:500]
            except ConnectorError as exc:
                provider_status_code = "ERROR"
                request.status = VerificationRequestStatus.FAILED
                request.error_code = "PROVIDER_ERROR"
                request.error_message = str(exc)[:500]

    request.completed_at = datetime.now(timezone.utc)

    overall_status = derive_result_overall_status(format_status=format_info["format_status"], provider_status_code=provider_status_code)

    result = (await db.execute(
        select(VerificationResult).where(VerificationResult.case_id == case.id, VerificationResult.check_type == VerificationCheckType.MCA)
    )).scalar_one_or_none()
    if result is None:
        result = VerificationResult(organization_id=case.organization_id, case_id=case.id, check_type=VerificationCheckType.MCA)
        db.add(result)
    else:
        result.retry_count += 1
    result.latest_request_id = request.id
    result.format_status = format_info["format_status"]
    result.provider_status_code = provider_status_code
    result.match_status = None
    result.overall_status = overall_status
    result.normalized_result = normalized_result
    result.provider_connection_id = connection_id
    result.provider_reference = provider_reference
    result.verified_at = datetime.fromisoformat(verified_at_raw) if verified_at_raw else None

    required_check_types, freshness_by_type = await _rule_context(db, case)
    freshness_days = freshness_by_type.get(VerificationCheckType.MCA.value)
    result.expires_at = (result.verified_at + timedelta(days=freshness_days)) if (overall_status in _FRESH_STATUSES and result.verified_at and freshness_days) else None

    db.add(VerificationUsage(
        organization_id=case.organization_id, request_id=request.id, check_type=VerificationCheckType.MCA,
        provider_connection_id=connection_id, outcome=overall_status.value,
    ))

    other_results = (await db.execute(
        select(VerificationResult).where(VerificationResult.case_id == case.id, VerificationResult.check_type != VerificationCheckType.MCA)
    )).scalars().all()
    present_check_types = {r.check_type.value for r in other_results} | {VerificationCheckType.MCA.value}
    case.overall_status = derive_case_overall_status(
        [r.overall_status for r in other_results] + [overall_status],
        required_check_types=required_check_types, present_check_types=present_check_types,
    )

    await db.commit()
    await db.refresh(result)
    await db.refresh(case)
    return result


async def run_aadhaar_check(
    db: AsyncSession, case: VerificationCase, *, aadhaar: str, consent_reference: str | None = None,
    purpose: str | None = None, connection_id: int | None = None, actor_user_id: int | None = None,
    force_refresh: bool = False,
) -> VerificationResult:
    """Aadhaar is the one check in this module where require_consent=True
    is set — see _run_identifier_check()'s own docstring and the approved
    plan's Regulatory risks section for why this will always resolve to
    CONSENT_REQUIRED or NOT_CONFIGURED (never VERIFIED) until a UIDAI-
    licensed provider is wired. force_refresh is threaded through for API
    consistency, but the freshness cache never actually short-circuits
    Aadhaar in practice, since neither outcome is in _FRESH_STATUSES."""
    return await _run_identifier_check(
        db, case, check_type=VerificationCheckType.AADHAAR, identifier=aadhaar, submitted_name=None,
        connection_id=connection_id, actor_user_id=actor_user_id, force_refresh=force_refresh,
        format_validator=identity_verification_service.validate_aadhaar_format,
        mask_fn=identity_verification_service.mask_aadhaar, verify_fn=identity_verification_service.verify_aadhaar,
        consent_reference=consent_reference, purpose=purpose, require_consent=True,
    )
