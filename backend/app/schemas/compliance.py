"""Compliance & Verification schemas. VerificationCaseOut/
VerificationResultOut are shared across every check type (check_type and
provider_status_code are free strings, not per-type enums — see
models/india_verification.py and services/compliance/verification_status.
py). RunVerificationRequest/RunCheckOnCaseRequest are likewise one shape
for all six identifier/registration checks (PAN/GSTIN/TAN/MCA/Udyam/
Aadhaar) — each field only one or a few check types actually use, same
"generic request, type-specific fields optional" shape as the ERP/
Accounting connector verb clusters in connectors/base.py."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class VerificationEvidenceOut(BaseModel):
    id: int
    result_id: int
    evidence_type: str
    document_id: int | None
    page_number: int | None
    bbox: dict[str, Any] | None
    snapshot_ref: str | None
    details: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class VerificationResultOut(BaseModel):
    id: int
    case_id: int
    check_type: str
    format_status: str
    provider_status_code: str | None
    match_status: str | None
    overall_status: str
    normalized_result: dict[str, Any]
    provider_connection_id: int | None
    provider_reference: str | None
    verified_at: datetime | None
    expires_at: datetime | None
    retry_count: int
    created_at: datetime
    updated_at: datetime
    evidence: list[VerificationEvidenceOut] = []

    model_config = {"from_attributes": True}


class VerificationComplianceOut(BaseModel):
    """Attached to VerificationCaseOut (Phase Group 4, IV20) — a
    genuinely distinct axis from overall_status (see verification_
    rules_service.ComplianceResult's own docstring): `compliant=None`
    means "no rule applies", never conflated with True."""
    rule_id: int | None
    rule_name: str | None
    required_check_types: list[str]
    satisfied_check_types: list[str]
    missing_check_types: list[str]
    compliant: bool | None
    reason: str


class VerificationCaseOut(BaseModel):
    id: int
    subject_type: str
    subject_id: int | None
    subject_name: str
    business_process: str | None
    overall_status: str
    requested_by: int | None
    created_at: datetime
    updated_at: datetime
    results: list[VerificationResultOut] = []
    compliance: VerificationComplianceOut | None = None

    model_config = {"from_attributes": True}


class VerificationCaseListOut(BaseModel):
    items: list[VerificationCaseOut]
    total: int
    page: int
    page_size: int


class RunVerificationRequest(BaseModel):
    check_type: str  # PAN | GSTIN | TAN | MCA | UDYAM | AADHAAR
    subject_type: str
    subject_name: str
    subject_id: int | None = None
    business_process: str | None = None
    identifier: str | None = None  # PAN/GSTIN/TAN/UDYAM/AADHAAR value — unused for MCA
    cin: str | None = None  # MCA only
    llpin: str | None = None  # MCA only
    legal_name: str | None = None  # MCA only — a name search, may return several candidates
    submitted_name: str | None = None
    connection_id: int | None = None
    consent_reference: str | None = None  # AADHAAR only — required before any provider call is attempted
    purpose: str | None = None  # AADHAAR only
    force_refresh: bool = False  # bypass an unexpired cached result (Phase Group 4, IV20) and call the provider anyway


class RunCheckOnCaseRequest(BaseModel):
    """Body for POST /verifications/{case_id}/checks/{check_type} — runs
    (or retries) ONE check type on an EXISTING case. Also the one way to
    retry: the identifier value is never persisted in the clear (see
    VerificationRequest.input_identifier_masked), so a retry genuinely
    needs it supplied again rather than replaying a stored value — same
    discipline as never caching a password to "retry" a login."""
    identifier: str | None = None
    cin: str | None = None
    llpin: str | None = None
    legal_name: str | None = None
    submitted_name: str | None = None
    connection_id: int | None = None
    consent_reference: str | None = None
    purpose: str | None = None
    force_refresh: bool = False


class VerificationRuleOut(BaseModel):
    id: int
    organization_id: int | None
    name: str
    description: str | None
    version: int
    is_current: bool
    status: str
    priority: int
    business_process: str
    subject_type: str | None
    required_check_types: list[str]
    freshness_days: int | None
    effective_from: datetime | None
    effective_to: datetime | None
    created_by: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VerificationRuleIn(BaseModel):
    name: str
    description: str | None = None
    business_process: str
    subject_type: str | None = None
    required_check_types: list[str]
    freshness_days: int | None = None
    priority: int = 100
    effective_from: datetime | None = None
    effective_to: datetime | None = None


class VerificationRuleStatusRequest(BaseModel):
    status: str  # ACTIVE | INACTIVE


class VerificationSimulateRequest(BaseModel):
    business_process: str
    subject_type: str | None = None


class VerificationConditionCheckOut(BaseModel):
    field: str
    operator: str
    expected: object
    actual: object
    passed: bool
    note: str = ""


class VerificationRuleEvaluationOut(BaseModel):
    rule_id: int
    rule_name: str
    version: int
    organization_id: int | None
    priority: int
    specificity: int
    matched: bool
    checks: list[VerificationConditionCheckOut]
    reason: str


class VerificationSimulateResponse(BaseModel):
    candidates_considered: int
    evaluations: list[VerificationRuleEvaluationOut]
    matched_rules: list[VerificationRuleEvaluationOut]
    winner: VerificationRuleEvaluationOut | None
    conflict: bool
    conflicting_rule_ids: list[int]


class VerificationJobSubjectIn(BaseModel):
    subject_type: str
    subject_name: str
    subject_id: int | None = None
    identifiers: dict[str, str] = {}  # {check_type: value}


class CreateVerificationJobRequest(BaseModel):
    business_process: str | None = None
    check_types: list[str]
    connection_id: int | None = None
    subjects: list[VerificationJobSubjectIn]


class VerificationJobItemOut(BaseModel):
    id: int
    run_id: int
    subject_type: str
    subject_name: str
    subject_id: int | None
    case_id: int | None
    check_type: str | None
    status: str
    worker_id: str | None
    result_summary: dict[str, Any]
    error_message: str | None
    claimed_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class VerificationJobRunOut(BaseModel):
    id: int
    trigger_type: str
    status: str
    business_process: str | None
    check_types: list[str]
    connection_id: int | None
    schedule_id: int | None
    total_items: int
    success_items: int
    failed_items: int
    needs_attention_items: int
    error_message: str | None
    created_by: int | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    items: list[VerificationJobItemOut] = []

    model_config = {"from_attributes": True}


class VerificationJobListOut(BaseModel):
    items: list[VerificationJobRunOut]
    total: int
    page: int
    page_size: int


class RunMoaVerificationRequest(BaseModel):
    """Body for POST /compliance/moa/verify — the document must already
    have been processed through Document Intelligence with document_type
    "MOA" (see POST /api/documents/{id}/run-extraction — this route only
    ASSESSES what that pipeline already extracted, never re-runs OCR/AI
    extraction itself). Either case_id (append this check to an existing
    case) or subject_type+subject_name (create a new case) must be given."""
    document_id: int
    case_id: int | None = None
    subject_type: str | None = None
    subject_name: str | None = None
    business_process: str | None = None
