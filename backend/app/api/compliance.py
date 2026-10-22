"""Compliance & Verification router — /api/compliance/... (Phase 1 built
PAN; Phase Group 2 added GSTIN/TAN/MCA/Udyam/Aadhaar on the same generic
dispatch — see _run_check()/_run_check_on_case() below). Every route
here is gated on the INDIA_VERIFICATION module; GET routes need VIEW,
everything that creates a case/runs/retries a check needs EXECUTE — same
two-tier convention api/tally.py already uses.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.audit_log import AuditStatus, record_event
from app.models.connection import ConnectionCategory
from app.models.document import Document
from app.models.india_verification import (
    VerificationCase,
    VerificationCheckType,
    VerificationDocument,
    VerificationEvidence,
    VerificationResult,
)
from app.models.india_verification_jobs import VerificationJobItem, VerificationJobRun
from app.models.user import User, UserRole
from app.schemas.compliance import (
    CreateVerificationJobRequest,
    RunCheckOnCaseRequest,
    RunMoaVerificationRequest,
    RunVerificationRequest,
    VerificationCaseListOut,
    VerificationCaseOut,
    VerificationComplianceOut,
    VerificationConditionCheckOut,
    VerificationEvidenceOut,
    VerificationJobItemOut,
    VerificationJobListOut,
    VerificationJobRunOut,
    VerificationResultOut,
    VerificationRuleEvaluationOut,
    VerificationRuleIn,
    VerificationRuleOut,
    VerificationRuleStatusRequest,
    VerificationSimulateRequest,
    VerificationSimulateResponse,
)
from app.schemas.connections import ConnectionListOut
from app.services import connection_service
from app.services.compliance import moa_verification_service
from app.services.compliance import verification_job_service
from app.services.compliance import verification_orchestrator as orch
from app.services.compliance import verification_rules_service

router = APIRouter(prefix="/compliance", tags=["compliance"])

_view = require_action("VIEW", module="INDIA_VERIFICATION")
_execute = require_action("EXECUTE", module="INDIA_VERIFICATION")


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _case_out(db: AsyncSession, case: VerificationCase) -> VerificationCaseOut:
    results = (await db.execute(
        select(VerificationResult).where(VerificationResult.case_id == case.id).order_by(VerificationResult.check_type)
    )).scalars().all()
    result_outs = []
    for r in results:
        result_out = VerificationResultOut.model_validate(r)
        evidence = (await db.execute(
            select(VerificationEvidence).where(VerificationEvidence.result_id == r.id).order_by(VerificationEvidence.page_number)
        )).scalars().all()
        result_out.evidence = [VerificationEvidenceOut.model_validate(e) for e in evidence]
        result_outs.append(result_out)
    out = VerificationCaseOut.model_validate(case)
    out.results = result_outs

    compliance = await verification_rules_service.evaluate_case_compliance(db, case)
    out.compliance = VerificationComplianceOut(
        rule_id=compliance.rule.id if compliance.rule else None,
        rule_name=compliance.rule.name if compliance.rule else None,
        required_check_types=compliance.required_check_types, satisfied_check_types=compliance.satisfied_check_types,
        missing_check_types=compliance.missing_check_types, compliant=compliance.compliant, reason=compliance.reason,
    )
    return out


def _parse_check_type(raw: str) -> VerificationCheckType:
    try:
        return VerificationCheckType(raw.strip().upper())
    except ValueError:
        valid = ", ".join(t.value for t in VerificationCheckType)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown check type '{raw}' — must be one of: {valid}.")


async def _dispatch_check(
    db: AsyncSession, case: VerificationCase, check_type: VerificationCheckType, body, *, actor_user_id: int,
) -> VerificationResult:
    """The one place a check_type string turns into the right orchestrator
    call — shared by POST /verifications (new case) and POST .../checks/
    {check_type} (existing case). MOA isn't dispatched here: it's a
    Document Intelligence pipeline trigger, not an on-demand identifier
    check — see Phase Group 3."""
    force_refresh = getattr(body, "force_refresh", False)
    common = {"submitted_name": body.submitted_name, "connection_id": body.connection_id, "actor_user_id": actor_user_id, "force_refresh": force_refresh}
    if check_type == VerificationCheckType.PAN:
        if not body.identifier:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "PAN requires 'identifier'.")
        return await orch.run_pan_check(db, case, pan=body.identifier, **common)
    if check_type == VerificationCheckType.GSTIN:
        if not body.identifier:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "GSTIN requires 'identifier'.")
        return await orch.run_gstin_check(db, case, gstin=body.identifier, **common)
    if check_type == VerificationCheckType.TAN:
        if not body.identifier:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "TAN requires 'identifier'.")
        return await orch.run_tan_check(db, case, tan=body.identifier, **common)
    if check_type == VerificationCheckType.UDYAM:
        if not body.identifier:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Udyam requires 'identifier'.")
        return await orch.run_udyam_check(db, case, udyam_number=body.identifier, **common)
    if check_type == VerificationCheckType.MCA:
        if not (body.cin or body.llpin or body.legal_name):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "MCA search requires one of 'cin', 'llpin', or 'legal_name'.")
        return await orch.run_mca_check(
            db, case, cin=body.cin, llpin=body.llpin, legal_name=body.legal_name,
            connection_id=body.connection_id, actor_user_id=actor_user_id, force_refresh=force_refresh,
        )
    if check_type == VerificationCheckType.AADHAAR:
        if not body.identifier:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Aadhaar requires 'identifier'.")
        return await orch.run_aadhaar_check(
            db, case, aadhaar=body.identifier, consent_reference=body.consent_reference, purpose=body.purpose,
            connection_id=body.connection_id, actor_user_id=actor_user_id, force_refresh=force_refresh,
        )
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, f"Check type '{check_type.value}' is not run through this endpoint.")


@router.post("/verifications", response_model=VerificationCaseOut, status_code=status.HTTP_201_CREATED)
async def run_verification(
    body: RunVerificationRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db),
):
    org_id = _effective_org_id(user)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A verification must be run for a specific organization.")
    check_type = _parse_check_type(body.check_type)

    case = await orch.get_or_create_case(
        db, organization_id=org_id, subject_type=body.subject_type, subject_name=body.subject_name,
        subject_id=body.subject_id, business_process=body.business_process, requested_by=user.id,
    )
    result = await _dispatch_check(db, case, check_type, body, actor_user_id=user.id)
    await record_event(
        db, organization_id=org_id, actor_id=user.id, action=f"{check_type.value} Verification Run",
        module="India Verification", resource="VerificationCase", resource_id=str(case.id),
        status=AuditStatus.SUCCESS if result.overall_status.value == "VERIFIED" else AuditStatus.WARNING,
        after_value={"overall_status": result.overall_status.value, "provider_status_code": result.provider_status_code},
    )
    return await _case_out(db, case)


def _apply_case_filters(
    stmt, *, org_id: int | None, business_process: str | None, subject_search: str | None,
    check_type: str | None, overall_status: str | None, date_from: datetime | None, date_to: datetime | None,
):
    """Shared by list_verifications() and export_verifications() (IV25) —
    one filter set, so "what you see in History is exactly what you
    export" rather than two independently-drifting query paths."""
    if org_id is not None:
        stmt = stmt.where(VerificationCase.organization_id == org_id)
    if business_process:
        stmt = stmt.where(VerificationCase.business_process == business_process)
    if subject_search:
        stmt = stmt.where(VerificationCase.subject_name.ilike(f"%{subject_search}%"))
    if date_from:
        stmt = stmt.where(VerificationCase.created_at >= date_from)
    if date_to:
        stmt = stmt.where(VerificationCase.created_at <= date_to)
    if check_type or overall_status:
        result_filter = select(VerificationResult.id).where(VerificationResult.case_id == VerificationCase.id)
        if check_type:
            result_filter = result_filter.where(VerificationResult.check_type == _parse_check_type(check_type))
        if overall_status:
            result_filter = result_filter.where(VerificationResult.overall_status == overall_status.strip().upper())
        stmt = stmt.where(result_filter.exists())
    return stmt


def _parse_optional_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid date: {raw}")


@router.get("/verifications", response_model=VerificationCaseListOut)
async def list_verifications(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    business_process: str | None = Query(None), subject_search: str | None = Query(None),
    check_type: str | None = Query(None), overall_status: str | None = Query(None),
    date_from: str | None = Query(None), date_to: str | None = Query(None),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    org_id = _effective_org_id(user)
    filters = dict(
        org_id=org_id, business_process=business_process, subject_search=subject_search, check_type=check_type,
        overall_status=overall_status, date_from=_parse_optional_date(date_from), date_to=_parse_optional_date(date_to),
    )
    stmt = _apply_case_filters(select(VerificationCase), **filters)
    count_stmt = _apply_case_filters(select(func.count()).select_from(VerificationCase), **filters)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(
        stmt.order_by(VerificationCase.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    items = [await _case_out(db, case) for case in rows]
    return VerificationCaseListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/verifications/export")
async def export_verifications(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    business_process: str | None = Query(None), subject_search: str | None = Query(None),
    check_type: str | None = Query(None), overall_status: str | None = Query(None),
    date_from: str | None = Query(None), date_to: str | None = Query(None),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    """Same VIEW gate and the exact same filter set as list_verifications()
    — exporting is never a looser authorization boundary than viewing.
    One row per VerificationResult (not per case): overall_status/
    provider_status_code are check_type-specific, so flattening to the
    check level is the only honest granularity. Nothing here can ever
    carry a raw identifier — VerificationResult never stores one (see
    models/india_verification.py's own docstring); only the already-
    masked VerificationRequest.input_identifier_masked exists, and this
    export doesn't even read that table."""
    org_id = _effective_org_id(user)
    filters = dict(
        org_id=org_id, business_process=business_process, subject_search=subject_search, check_type=check_type,
        overall_status=overall_status, date_from=_parse_optional_date(date_from), date_to=_parse_optional_date(date_to),
    )
    stmt = _apply_case_filters(select(VerificationCase), **filters).order_by(VerificationCase.created_at.desc())
    cases = (await db.execute(stmt)).scalars().all()

    headers = ["Case ID", "Subject Name", "Subject Type", "Business Process", "Case Status", "Check Type", "Format", "Provider Outcome", "Overall Status", "Verified At", "Created At"]
    rows: list[list] = []
    for case in cases:
        results = (await db.execute(select(VerificationResult).where(VerificationResult.case_id == case.id).order_by(VerificationResult.check_type))).scalars().all()
        for r in results:
            rows.append([
                case.id, case.subject_name, case.subject_type, case.business_process or "", case.overall_status.value,
                r.check_type.value, r.format_status, r.provider_status_code or "", r.overall_status.value,
                r.verified_at.isoformat() if r.verified_at else "", case.created_at.isoformat(),
            ])

    if format == "xlsx":
        from io import BytesIO
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Verifications"
        ws.append(headers)
        for row in rows:
            ws.append(row)
        buf = BytesIO()
        wb.save(buf)
        content, media_type, filename = buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "verifications.xlsx"
    else:
        import csv
        from io import StringIO
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(rows)
        content, media_type, filename = buf.getvalue().encode("utf-8"), "text/csv", "verifications.csv"

    await record_event(
        db, organization_id=org_id, actor_id=user.id, action="Verification Export",
        module="India Verification", resource="Export", resource_id=None,
        status=AuditStatus.SUCCESS, after_value={"format": format, "row_count": len(rows)},
    )
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/verifications/{case_id}", response_model=VerificationCaseOut)
async def get_verification(case_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    case = await db.get(VerificationCase, case_id)
    if case is None or (org_id is not None and case.organization_id != org_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Verification case not found")
    return await _case_out(db, case)


@router.post("/verifications/{case_id}/checks/{check_type}", response_model=VerificationCaseOut)
async def run_check_on_case(
    case_id: int, check_type: str, body: RunCheckOnCaseRequest,
    user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db),
):
    """Runs (or retries — see RunCheckOnCaseRequest's docstring) ONE
    check type on an existing case. This is the one real retry path: the
    identifier value is never persisted in the clear, so a retry
    genuinely needs it supplied again."""
    org_id = _effective_org_id(user)
    case = await db.get(VerificationCase, case_id)
    if case is None or (org_id is not None and case.organization_id != org_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Verification case not found")

    check_type_enum = _parse_check_type(check_type)
    result = await _dispatch_check(db, case, check_type_enum, body, actor_user_id=user.id)
    await record_event(
        db, organization_id=case.organization_id, actor_id=user.id, action=f"{check_type_enum.value} Verification Retried",
        module="India Verification", resource="VerificationCase", resource_id=str(case.id),
        status=AuditStatus.SUCCESS if result.overall_status.value == "VERIFIED" else AuditStatus.WARNING,
        after_value={"overall_status": result.overall_status.value, "provider_status_code": result.provider_status_code,
                     "attempt": result.retry_count + 1},
    )
    return await _case_out(db, case)


@router.post("/moa/verify", response_model=VerificationCaseOut, status_code=status.HTTP_201_CREATED)
async def verify_moa(
    body: RunMoaVerificationRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db),
):
    """The MOA entry point — see moa_verification_service.py's own
    docstring for why this never runs OCR/AI extraction itself: the
    document must already be processed with document_type="MOA" via the
    existing POST /api/documents/{id}/run-extraction first. Links the
    document to a case (creating a new one if case_id isn't given, same
    as every other check's POST /verifications) and persists the
    analysis exactly like any other VerificationResult."""
    org_id = _effective_org_id(user)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A verification must be run for a specific organization.")

    doc = await db.get(Document, body.document_id)
    if doc is None or doc.organization_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    if not doc.extraction_result or not doc.extraction_result.get("fields"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This document has no AI-extracted fields yet — run AI Extraction with document type \"MOA\" on it first "
            "(POST /api/documents/{id}/run-extraction), then verify.",
        )

    if body.case_id is not None:
        case = await db.get(VerificationCase, body.case_id)
        if case is None or case.organization_id != org_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Verification case not found")
    else:
        if not body.subject_type or not body.subject_name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Either case_id, or subject_type + subject_name to create a new case, is required.")
        case = await orch.get_or_create_case(
            db, organization_id=org_id, subject_type=body.subject_type, subject_name=body.subject_name,
            business_process=body.business_process, requested_by=user.id,
        )

    link = (await db.execute(
        select(VerificationDocument).where(VerificationDocument.document_id == doc.id, VerificationDocument.check_type == VerificationCheckType.MOA)
    )).scalars().first()
    if link is None:
        db.add(VerificationDocument(organization_id=org_id, case_id=case.id, document_id=doc.id, check_type=VerificationCheckType.MOA))
    elif link.case_id != case.id:
        link.case_id = case.id
    await db.flush()

    result = await moa_verification_service.verify_moa(db, doc, case)
    await record_event(
        db, organization_id=org_id, actor_id=user.id, action="MOA Verification Run",
        module="India Verification", resource="VerificationCase", resource_id=str(case.id),
        status=AuditStatus.SUCCESS if result.overall_status.value in ("VERIFIED", "VERIFIED_WITH_WARNINGS") else AuditStatus.WARNING,
        after_value={"overall_status": result.overall_status.value, "provider_status_code": result.provider_status_code, "document_id": doc.id},
    )
    return await _case_out(db, case)


@router.get("/providers", response_model=ConnectionListOut)
async def list_providers(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    conns, total, owner_names = await connection_service.list_connections(
        db, organization_id=org_id, category=ConnectionCategory.VERIFICATION_PROVIDER.value, page_size=200,
    )
    items = [connection_service.to_out(c, owner_names.get(c.owner_id)) for c in conns]
    return ConnectionListOut(items=items, total=total, page=1, page_size=200)


@router.post("/providers/{connection_id}/test")
async def test_provider(connection_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    conn = await connection_service.get_connection(db, connection_id, org_id)
    if conn is None or conn.category != ConnectionCategory.VERIFICATION_PROVIDER:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Verification provider connection not found")
    return await connection_service.test_connection(db, conn)


# ── Verification Rules Engine (Phase Group 4, IV19) ───────────────────

def _rule_evaluation_out(e) -> "VerificationRuleEvaluationOut":
    return VerificationRuleEvaluationOut(
        rule_id=e.rule_id, rule_name=e.rule_name, version=e.version, organization_id=e.organization_id,
        priority=e.priority, specificity=e.specificity, matched=e.matched, reason=e.reason,
        checks=[VerificationConditionCheckOut(field=c.field, operator=c.operator, expected=c.expected, actual=c.actual, passed=c.passed, note=c.note) for c in e.checks],
    )


@router.get("/rules", response_model=list[VerificationRuleOut])
async def list_rules(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    rules = await verification_rules_service.list_rules_for_admin(db, _effective_org_id(user))
    return [VerificationRuleOut.model_validate(r) for r in rules]


@router.get("/rules/{rule_id}", response_model=VerificationRuleOut)
async def get_rule(rule_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    rule = await verification_rules_service.get_rule(db, _effective_org_id(user), rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Verification rule not found")
    return VerificationRuleOut.model_validate(rule)


@router.post("/rules", response_model=VerificationRuleOut, status_code=status.HTTP_201_CREATED)
async def publish_rule(body: VerificationRuleIn, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    if user.role not in (UserRole.SUPER_ADMIN, UserRole.ORG_ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an org admin or platform admin can publish verification rules.")
    try:
        rule = await verification_rules_service.publish_rule(
            db, org_id, name=body.name, description=body.description, business_process=body.business_process,
            subject_type=body.subject_type, required_check_types=body.required_check_types, freshness_days=body.freshness_days,
            priority=body.priority, effective_from=body.effective_from, effective_to=body.effective_to, actor_id=user.id,
        )
    except verification_rules_service.VerificationRuleServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(exc.errors))
    await record_event(
        db, organization_id=org_id, actor_id=user.id, action="Verification Rule Published",
        module="India Verification", resource="VerificationRule", resource_id=str(rule.id),
        status=AuditStatus.SUCCESS, after_value={"name": rule.name, "version": rule.version, "required_check_types": rule.required_check_types},
    )
    return VerificationRuleOut.model_validate(rule)


@router.post("/rules/{rule_id}/status", response_model=VerificationRuleOut)
async def set_rule_status(rule_id: int, body: VerificationRuleStatusRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    from app.models.india_verification_rules import VerificationRuleStatus as RuleStatus
    try:
        new_status = RuleStatus(body.status.strip().upper())
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "status must be ACTIVE or INACTIVE.")
    try:
        rule = await verification_rules_service.set_rule_status(db, _effective_org_id(user), rule_id, new_status)
    except verification_rules_service.VerificationRuleServiceError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "; ".join(exc.errors))
    await record_event(
        db, organization_id=rule.organization_id, actor_id=user.id, action=f"Verification Rule {new_status.value.title()}d",
        module="India Verification", resource="VerificationRule", resource_id=str(rule.id), status=AuditStatus.SUCCESS,
    )
    return VerificationRuleOut.model_validate(rule)


@router.post("/rules/simulate", response_model=VerificationSimulateResponse)
async def simulate_rule(body: VerificationSimulateRequest, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    trace = await verification_rules_service.simulate_rule(
        db, _effective_org_id(user), verification_rules_service.VerificationRuleMatchContext(business_process=body.business_process, subject_type=body.subject_type),
    )
    return VerificationSimulateResponse(
        candidates_considered=trace.candidates_considered,
        evaluations=[_rule_evaluation_out(e) for e in trace.evaluations],
        matched_rules=[_rule_evaluation_out(e) for e in trace.matched_rules],
        winner=_rule_evaluation_out(trace.winner) if trace.winner else None,
        conflict=trace.conflict, conflicting_rule_ids=trace.conflicting_rule_ids,
    )


# ── Batch verification jobs (Phase Group 4, IV21) ─────────────────────

async def _job_out(db: AsyncSession, run: VerificationJobRun) -> VerificationJobRunOut:
    items = (await db.execute(select(VerificationJobItem).where(VerificationJobItem.run_id == run.id).order_by(VerificationJobItem.id))).scalars().all()
    out = VerificationJobRunOut.model_validate(run)
    out.items = [VerificationJobItemOut.model_validate(i) for i in items]
    return out


@router.post("/jobs", response_model=VerificationJobRunOut, status_code=status.HTTP_201_CREATED)
async def create_job(body: CreateVerificationJobRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A batch must be run for a specific organization.")
    try:
        run = await verification_job_service.create_manual_job(
            db, org_id, business_process=body.business_process, check_types=body.check_types,
            connection_id=body.connection_id, subjects=[s.model_dump() for s in body.subjects], created_by=user.id,
        )
    except verification_job_service.VerificationJobServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(exc.errors))
    await record_event(
        db, organization_id=org_id, actor_id=user.id, action="Verification Batch Started",
        module="India Verification", resource="VerificationJobRun", resource_id=str(run.id),
        status=AuditStatus.SUCCESS, after_value={"total_items": run.total_items, "check_types": run.check_types},
    )
    return await _job_out(db, run)


@router.get("/jobs", response_model=VerificationJobListOut)
async def list_jobs(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    org_id = _effective_org_id(user)
    stmt = select(VerificationJobRun)
    count_stmt = select(func.count()).select_from(VerificationJobRun)
    if org_id is not None:
        stmt = stmt.where(VerificationJobRun.organization_id == org_id)
        count_stmt = count_stmt.where(VerificationJobRun.organization_id == org_id)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt.order_by(VerificationJobRun.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    items = [await _job_out(db, r) for r in rows]
    return VerificationJobListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/jobs/{run_id}", response_model=VerificationJobRunOut)
async def get_job(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    run = await db.get(VerificationJobRun, run_id)
    if run is None or (org_id is not None and run.organization_id != org_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Verification batch not found")
    return await _job_out(db, run)


@router.post("/jobs/{run_id}/retry-failed", response_model=VerificationJobRunOut)
async def retry_failed_job_items(run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    run = await db.get(VerificationJobRun, run_id)
    if run is None or (org_id is not None and run.organization_id != org_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Verification batch not found")
    requeued = await verification_job_service.retry_failed_items(db, run)
    await db.refresh(run)
    await record_event(
        db, organization_id=run.organization_id, actor_id=user.id, action="Verification Batch Retried",
        module="India Verification", resource="VerificationJobRun", resource_id=str(run.id),
        status=AuditStatus.SUCCESS, after_value={"requeued_items": requeued},
    )
    return await _job_out(db, run)
