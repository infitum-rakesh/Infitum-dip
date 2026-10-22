"""
moa_verification_service.py — Memorandum of Association verification
(Phase Group 3, IV15-IV16).

Reuses the EXISTING Document Intelligence pipeline wholesale: a document
processed with document_type="MOA" already has real OCR text
(doc.ocr_result["full_text"]) and a real, evidence-grounded extraction
(doc.extraction_result["fields"], seeded from document_taxonomies.
MOA_FIELDS — see that module's own docstring for the clause structure).
This service never re-does OCR/extraction — it only ASSESSES what was
already genuinely extracted, exactly the "derive at read time" pattern
every other service in this codebase follows.

Per spec section 11.3, OCR+AI extraction alone can confirm a clause's
TEXT is present — it can NEVER independently prove the document is
genuine, currently legally effective, or actually filed with MCA. This
module's status vocabulary keeps those two kinds of claim strictly
separate:
  DOCUMENT_TYPE_CONFIRMED / CONTENT_EXTRACTED / COMPLETENESS_CHECK_PASSED
    — purely about what THIS document's own text contains.
  REGISTRY_DETAILS_MATCHED / MISMATCH_FOUND
    — a genuine cross-check against a real MCA search result already on
    the same VerificationCase (see company_verification_service.py) —
    never fabricated, and skipped entirely (no comparison attempted) if
    no MCA result exists on the case yet.
  AUTHENTICITY_NOT_ESTABLISHED / REQUIRES_MANUAL_REVIEW
    — this module is explicit that it is NOT asserting the document is a
    forgery or genuinely invalid; only that nothing here establishes
    authenticity, which is the honest limit of what text extraction can
    ever prove (official filing/digital-signature verification is a
    distinct, unimplemented capability — see the approved plan).

No connector/external provider is involved here (this is an internal
Document Intelligence analysis, not a third-party API call) — so unlike
every other check in this module, VerificationRequest.connection_id is
always None and there is no NOT_CONFIGURED/UNAVAILABLE path for the
analysis itself (only for the optional MCA cross-check's own data, which
simply isn't attempted if absent).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.india_verification import (
    VerificationCase,
    VerificationCheckType,
    VerificationDocument,
    VerificationEvidence,
    VerificationRequest,
    VerificationRequestStatus,
    VerificationResult,
    VerificationUsage,
)
from app.services.compliance.verification_status import derive_case_overall_status, derive_result_overall_status

# The clauses every real MOA must have, per spec section 11.1 — missing
# any one of these means the document is genuinely incomplete, not just
# an extraction gap (document_taxonomies.MOA_FIELDS asks the model for
# all of these; a missing value means the model found no grounded text
# for it in this document, not that it forgot to look).
REQUIRED_MOA_FIELDS = [
    "company_name", "registered_office_address", "objects_clause_text",
    "liability_clause_text", "capital_clause_text",
]


def _normalize_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _field_map(doc: Document) -> dict:
    return {f["name"]: f.get("value") for f in (doc.extraction_result or {}).get("fields", []) if f.get("value")}


def _field_source_page(doc: Document, name: str) -> int | None:
    for f in (doc.extraction_result or {}).get("fields", []):
        if f.get("name") == name:
            return f.get("source_page")
    return None


async def find_linked_case_id(db: AsyncSession, document_id: int) -> int | None:
    """Looks up whether a VerificationCase has already claimed this
    document for an MOA check — created via the explicit Corporate
    Document Verification flow (POST /api/compliance/moa/verify), never
    auto-created here. Used by document_service.py's auto-reverify hook
    (a document getting reprocessed/corrected after a case already
    exists for it) and returns None otherwise, a clean no-op."""
    row = (await db.execute(
        select(VerificationDocument).where(VerificationDocument.document_id == document_id, VerificationDocument.check_type == VerificationCheckType.MOA)
    )).scalars().first()
    return row.case_id if row else None


async def verify_moa_if_linked(db: AsyncSession, doc: Document) -> VerificationResult | None:
    """document_service.py's auto-hook entry point — a no-op unless a
    VerificationCase already claimed this document (see
    find_linked_case_id's docstring)."""
    case_id = await find_linked_case_id(db, doc.id)
    if case_id is None:
        return None
    case = await db.get(VerificationCase, case_id)
    if case is None or case.organization_id != doc.organization_id:
        return None
    return await verify_moa(db, doc, case)


async def verify_moa(db: AsyncSession, doc: Document, case: VerificationCase) -> VerificationResult:
    """The one real analysis function — assesses what document_service.py's
    existing OCR/AI-extraction pipeline already produced for this
    document (never re-extracts), optionally cross-checks against an
    MCA result already on the same case, and persists a
    VerificationResult + evidence rows. Never raises — an analysis bug
    here must never look like a confirmed finding; see the outer
    try/except in both call sites (document_service.py's hook and
    api/compliance.py's route)."""
    fields = _field_map(doc)
    full_text = (doc.ocr_result or {}).get("full_text") or ""
    ocr_quality_flags = (doc.ocr_result or {}).get("quality_flags") or []

    correlation_id = uuid.uuid4().hex
    idempotency_key = f"{case.id}:{VerificationCheckType.MOA.value}:{doc.id}:{uuid.uuid4().hex[:8]}"

    prior_attempts = (await db.execute(
        select(VerificationRequest).where(VerificationRequest.case_id == case.id, VerificationRequest.check_type == VerificationCheckType.MOA)
    )).scalars().all()

    request = VerificationRequest(
        organization_id=case.organization_id, case_id=case.id, check_type=VerificationCheckType.MOA,
        connection_id=None, attempt_number=len(prior_attempts) + 1, correlation_id=correlation_id,
        idempotency_key=idempotency_key, input_identifier_masked=f"document:{doc.id}",
        status=VerificationRequestStatus.RUNNING,
    )
    db.add(request)
    await db.flush()

    # 1. Document type confirmation — structural (the real OCR text says
    # "memorandum"), never just trusting that the caller pinned
    # document_type="MOA" without evidence.
    document_type_confirmed = (
        "memorandum" in full_text.lower() or "memorandum" in _normalize_name(fields.get("document_title"))
    )

    # 2. Completeness — every required clause has a genuinely extracted value.
    missing_fields = [f for f in REQUIRED_MOA_FIELDS if not fields.get(f)]
    completeness_passed = not missing_fields

    # 3. Cross-record check against this case's own MCA result, if any —
    # never fabricated, never attempted without a real MCA match to
    # compare against.
    mca_result = (await db.execute(
        select(VerificationResult).where(VerificationResult.case_id == case.id, VerificationResult.check_type == VerificationCheckType.MCA)
    )).scalar_one_or_none()

    comparisons: list[dict] = []
    cross_check_status: str | None = None
    if mca_result and (mca_result.normalized_result or {}).get("matches"):
        matches = mca_result.normalized_result["matches"]
        candidate = matches[0] if len(matches) == 1 else None
        if candidate is not None:
            name_match = fields.get("company_name") is not None and _normalize_name(fields.get("company_name")) == _normalize_name(candidate.get("legal_name"))
            comparisons.append({"field": "company_name", "moa_value": fields.get("company_name"), "mca_value": candidate.get("legal_name"), "matched": name_match})
            if fields.get("cin"):
                cin_match = fields["cin"].strip().upper() == (candidate.get("cin") or "").strip().upper()
                comparisons.append({"field": "cin", "moa_value": fields.get("cin"), "mca_value": candidate.get("cin"), "matched": cin_match})
            cross_check_status = "REGISTRY_DETAILS_MATCHED" if all(c["matched"] for c in comparisons) else "MISMATCH_FOUND"

    # 4. Derive the one provider_status_code — order matters: authenticity
    # concerns and confirmed mismatches outrank a merely-incomplete
    # document, which in turn outranks a clean pass.
    if not document_type_confirmed:
        provider_status_code = "AUTHENTICITY_NOT_ESTABLISHED"
    elif cross_check_status == "MISMATCH_FOUND":
        provider_status_code = "MISMATCH_FOUND"
    elif not completeness_passed or ocr_quality_flags:
        provider_status_code = "REQUIRES_MANUAL_REVIEW"
    elif cross_check_status == "REGISTRY_DETAILS_MATCHED":
        provider_status_code = "REGISTRY_DETAILS_MATCHED"
    else:
        provider_status_code = "COMPLETENESS_CHECK_PASSED"

    request.status = VerificationRequestStatus.SUCCEEDED
    request.completed_at = datetime.now(timezone.utc)

    normalized_result = {
        "document_id": doc.id,
        "document_type_confirmed": document_type_confirmed,
        "missing_fields": missing_fields,
        "ocr_quality_flags": ocr_quality_flags,
        "extracted_fields": fields,
        "comparisons": comparisons,
    }

    overall_status = derive_result_overall_status(format_status="FORMAT_VALID", provider_status_code=provider_status_code)

    result = (await db.execute(
        select(VerificationResult).where(VerificationResult.case_id == case.id, VerificationResult.check_type == VerificationCheckType.MOA)
    )).scalar_one_or_none()
    if result is None:
        result = VerificationResult(organization_id=case.organization_id, case_id=case.id, check_type=VerificationCheckType.MOA)
        db.add(result)
    else:
        result.retry_count += 1
    result.latest_request_id = request.id
    result.format_status = "FORMAT_VALID"
    result.provider_status_code = provider_status_code
    result.match_status = "MISMATCH" if cross_check_status == "MISMATCH_FOUND" else ("MATCHED" if cross_check_status == "REGISTRY_DETAILS_MATCHED" else None)
    result.overall_status = overall_status
    result.normalized_result = normalized_result
    result.provider_connection_id = None
    result.provider_reference = None
    result.verified_at = datetime.now(timezone.utc)
    await db.flush()

    # Evidence — one row per required clause (page it was found on, so
    # the UI can jump to it), plus one per MCA comparison (spec's own
    # "preserve evidence references ... and supporting values for each
    # comparison" requirement).
    for field_name in REQUIRED_MOA_FIELDS + ["cin"]:
        if field_name in fields:
            db.add(VerificationEvidence(
                organization_id=case.organization_id, result_id=result.id, evidence_type="DOCUMENT_REGION",
                document_id=doc.id, page_number=_field_source_page(doc, field_name),
                details={"field": field_name, "value": fields[field_name]},
            ))
    for comparison in comparisons:
        db.add(VerificationEvidence(
            organization_id=case.organization_id, result_id=result.id, evidence_type="COMPARISON",
            document_id=doc.id, details=comparison,
        ))

    db.add(VerificationUsage(
        organization_id=case.organization_id, request_id=request.id, check_type=VerificationCheckType.MOA,
        provider_connection_id=None, outcome=overall_status.value,
    ))

    other_results = (await db.execute(
        select(VerificationResult).where(VerificationResult.case_id == case.id, VerificationResult.check_type != VerificationCheckType.MOA)
    )).scalars().all()
    from app.services.compliance import verification_rules_service
    rule = await verification_rules_service.resolve_applicable_rule(db, case.organization_id, business_process=case.business_process, subject_type=case.subject_type)
    required_check_types = set(rule.required_check_types or []) if rule else set()
    present_check_types = {r.check_type.value for r in other_results} | {VerificationCheckType.MOA.value}
    case.overall_status = derive_case_overall_status(
        [r.overall_status for r in other_results] + [overall_status],
        required_check_types=required_check_types, present_check_types=present_check_types,
    )

    await db.commit()
    await db.refresh(result)
    await db.refresh(case)

    # Escalate to Review Queue — a genuinely separate step from the
    # verification transaction above (same isolation discipline as every
    # other cross-system escalation in this codebase): a bug in the
    # Review Queue write must never un-persist a verification result
    # that already succeeded.
    if provider_status_code in ("MISMATCH_FOUND", "AUTHENTICITY_NOT_ESTABLISHED", "REQUIRES_MANUAL_REVIEW"):
        try:
            from app.services import review_queue_service
            issue = {
                "MISMATCH_FOUND": f"MOA details do not match the MCA record on file for case #{case.id}.",
                "AUTHENTICITY_NOT_ESTABLISHED": "This document's text does not confirm it is a Memorandum of Association.",
                "REQUIRES_MANUAL_REVIEW": f"MOA is missing required clauses ({', '.join(missing_fields)}) or has low-quality OCR." if missing_fields else "MOA extraction has OCR quality concerns.",
            }[provider_status_code]
            await review_queue_service.create_from_verification_mismatch(db, doc, issue=issue)
        except Exception:  # noqa: BLE001 - a Review Queue escalation failure must never undo the verification result above
            pass

    return result
