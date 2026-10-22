"""
india_verification.py — India Business, Tax Identity & Corporate Document
Verification module ("Compliance & Verification").

Phase 1 (IV1-IV6) scopes PAN only, against a local mock verification
gateway (see scripts/mock_verification_gateway.py and
connectors/verification/ for the provider abstraction) — every later check
type (GSTIN/TAN/MCA/Udyam/Aadhaar/MOA) is added without reshaping any
table here: VerificationCheckType simply grows, and the rest of this
module is already check-type-agnostic.

Design notes:
 - A VerificationCase is the overall subject being verified (e.g. one
   vendor onboarding) — it may carry several checks (PAN + GSTIN + MOA).
 - A VerificationRequest is one real attempt at one check (one provider
   call, one correlation id) — retries create NEW request rows, never
   mutate a prior one, so every attempt has its own immutable audit trail.
 - A VerificationResult is the CURRENT normalized outcome for one
   (case, check_type) pair — upserted in place as requests complete, same
   convention as TallyVoucherPreview (one row per (connection, invoice),
   never duplicated per rebuild). `latest_request_id` always points at the
   request that produced the currently-stored outcome.
 - Per spec section 14, format validation / provider verification /
   identity matching / overall decision are kept as separate fields,
   never conflated. `overall_status` is the coarse, dashboard-friendly
   roll-up (OverallVerificationStatus); `provider_status_code` is the
   check-type-specific detailed code (free string, validated in the
   service layer against a per-check-type allowed set — PAN's outcomes
   are not GSTIN's or MCA's, so a single shared DB enum would either be
   too loose or force meaningless values onto the wrong check type).
 - No Aadhaar number, OTP, or biometric data is ever persisted here —
   only a masked reference (see identity_verification_service.mask_*())
   and a provider correlation id. See verification_orchestrator.py's own
   docstring for the full pipeline these tables back.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class VerificationCheckType(str, enum.Enum):
    PAN = "PAN"
    GSTIN = "GSTIN"
    TAN = "TAN"
    AADHAAR = "AADHAAR"
    MCA = "MCA"
    UDYAM = "UDYAM"
    MOA = "MOA"


class OverallVerificationStatus(str, enum.Enum):
    """The coarse, dashboard/rules-friendly roll-up — spec section 14.
    Never equates missing data, a provider outage, format validation
    alone, or AI extraction confidence with a successful verification."""
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFIED = "VERIFIED"
    VERIFIED_WITH_WARNINGS = "VERIFIED_WITH_WARNINGS"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    MISMATCH = "MISMATCH"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    PENDING_PROVIDER = "PENDING_PROVIDER"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


class VerificationRequestStatus(str, enum.Enum):
    """The call-lifecycle status of one attempt — distinct from the
    *outcome* (OverallVerificationStatus/provider_status_code) a
    successfully-completed attempt produced."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class VerificationCase(TenantBase):
    """One subject being verified — e.g. a single vendor-onboarding case
    that may require several independent checks (PAN + GSTIN + MOA)."""
    __tablename__ = "verification_cases"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)

    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. "VENDOR" / "CUSTOMER" / "COMPANY" — caller-defined, not a closed set
    subject_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # no FK — the subject may live in any module (Customer, Connection, Organization, ...)
    subject_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Matched against VerificationRule.business_process (Phase Group 4) to
    # resolve which checks this case requires — free text until the rules
    # engine exists, e.g. "VENDOR_ONBOARDING" / "MSME_WORKFLOW".
    business_process: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    overall_status: Mapped[OverallVerificationStatus] = mapped_column(
        Enum(OverallVerificationStatus), default=OverallVerificationStatus.NOT_STARTED, index=True,
    )

    requested_by: Mapped[int | None] = mapped_column(nullable=True)
    consent_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<VerificationCase {self.subject_name} ({self.overall_status})>"


class VerificationRequest(TenantBase):
    """One real attempt at one check — one provider call (or one
    NOT_CONFIGURED/UNAVAILABLE short-circuit before a call was even
    possible), one correlation id, immutable once completed. A retry is a
    brand-new row, never a mutation of a prior attempt."""
    __tablename__ = "verification_requests"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)

    case_id: Mapped[int] = mapped_column(ForeignKey("verification_cases.id"), nullable=False, index=True)
    check_type: Mapped[VerificationCheckType] = mapped_column(Enum(VerificationCheckType), nullable=False, index=True)
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id"), nullable=True, index=True)

    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # (case_id, check_type, input_hash) — lets a caller safely re-submit
    # the exact same check without duplicating a provider call; see
    # verification_orchestrator.py's idempotency handling.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    input_identifier_masked: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(255), nullable=True)

    requested_by: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[VerificationRequestStatus] = mapped_column(
        Enum(VerificationRequestStatus), default=VerificationRequestStatus.PENDING, index=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)  # always scrubbed — see connectors/base.py's scrub_secrets()

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<VerificationRequest case={self.case_id} {self.check_type} attempt={self.attempt_number}>"


class VerificationResult(TenantBase):
    """The CURRENT normalized outcome for one (case, check_type) pair —
    upserted in place as requests complete. `provider_status_code` is a
    free string validated per check_type in the service layer (PAN's
    outcome set is not GSTIN's or MCA's)."""
    __tablename__ = "verification_results"
    __table_args__ = (
        UniqueConstraint("case_id", "check_type", name="uq_verification_result_case_check_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)

    case_id: Mapped[int] = mapped_column(ForeignKey("verification_cases.id"), nullable=False, index=True)
    check_type: Mapped[VerificationCheckType] = mapped_column(Enum(VerificationCheckType), nullable=False, index=True)
    latest_request_id: Mapped[int | None] = mapped_column(ForeignKey("verification_requests.id"), nullable=True)

    format_status: Mapped[str] = mapped_column(String(30), default="FORMAT_UNKNOWN")  # FORMAT_VALID | FORMAT_INVALID | FORMAT_UNKNOWN
    provider_status_code: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g. "VERIFIED" / "NAME_MISMATCH" / "AMBIGUOUS_MATCH" — check-type-specific
    match_status: Mapped[str | None] = mapped_column(String(30), nullable=True)  # MATCHED | MISMATCH | NOT_COMPARED
    overall_status: Mapped[OverallVerificationStatus] = mapped_column(
        Enum(OverallVerificationStatus), default=OverallVerificationStatus.NOT_STARTED, index=True,
    )

    normalized_result: Mapped[dict] = mapped_column(JSON, default=dict)  # check-type-specific normalized attributes (legal_name, registration fields, ...) — never the raw provider payload

    provider_connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id"), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)  # freshness horizon — see verification_orchestrator.py caching rules (Phase Group 4)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<VerificationResult case={self.case_id} {self.check_type} {self.overall_status}>"


class VerificationEvidence(TenantBase):
    """Evidence references for one result — a provider response
    reference, a document page/region, or a cross-record comparison
    detail. Never the raw provider payload verbatim unless the provider's
    own terms permit retention — see `snapshot_ref` below."""
    __tablename__ = "verification_evidence"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)

    result_id: Mapped[int] = mapped_column(ForeignKey("verification_results.id"), nullable=False, index=True)
    evidence_type: Mapped[str] = mapped_column(String(40), nullable=False)  # PROVIDER_RESPONSE_REF | DOCUMENT_REGION | COMPARISON
    document_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # no FK — MOA evidence anchors to a real Document, but identifier checks have none
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    snapshot_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VerificationDocument(TenantBase):
    """Links a real Document to a VerificationCase for document-type
    checks (MOA, Phase Group 3) — never a fake/duplicated Document row,
    same anchor discipline as Review Queue's Tally-exception items."""
    __tablename__ = "verification_documents"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)

    case_id: Mapped[int] = mapped_column(ForeignKey("verification_cases.id"), nullable=False, index=True)
    document_id: Mapped[int] = mapped_column(nullable=False, index=True)  # no FK — see VerificationEvidence.document_id
    check_type: Mapped[VerificationCheckType] = mapped_column(Enum(VerificationCheckType), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VerificationUsage(TenantBase):
    """Append-only, one row per completed request — the raw log
    Observability/Reports (Phase Group 5) aggregate from, same
    "don't pre-aggregate, derive at read time" convention as QueryExecutionLog."""
    __tablename__ = "verification_usage"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)

    request_id: Mapped[int] = mapped_column(ForeignKey("verification_requests.id"), nullable=False, index=True)
    check_type: Mapped[VerificationCheckType] = mapped_column(Enum(VerificationCheckType), nullable=False, index=True)
    provider_connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id"), nullable=True)
    outcome: Mapped[str] = mapped_column(String(50), nullable=False)  # mirrors the request's final status/overall_status at completion time

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
