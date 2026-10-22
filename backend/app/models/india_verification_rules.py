"""
india_verification_rules.py — Verification Rules Engine (Phase Group 4,
IV19). "required-check-sets per business process + freshness/expiry
policy" per the approved plan — NOT approval-workflow gating (that's
ApprovalRule's job, a different domain). Mirrors ApprovalRule's shape
(app/models/approval/rules.py) for the parts that generalize —
org-vs-platform scope, version+is_current, priority, effective window —
but deliberately skips ApprovalRule's separate conditions/steps child
tables: this module's only two real match conditions (business_process,
subject_type) are common enough to earn first-class columns, the same
hybrid ApprovalRule itself uses for min_amount/max_amount/currency/
invoice_type before falling back to a child table for everything else.
Since this module has no comparable long tail of conditions yet, a
child table would be unused generality — add one later if a real need
for it (e.g. matching on connection/provider, or a value range) shows up.

Versioning convention is identical to ApprovalRule: never mutate a
published version in place; publish_rule() writes a new row and flips
is_current on the prior one, so history is always queryable.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class VerificationRuleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class VerificationRule(TenantBase):
    """One version of a named rule. `organization_id IS NULL` = a
    platform-wide default, visible to every org unless a same-named org-
    specific row overrides it (merge logic lives in
    verification_rules_service.resolve_candidate_rules(), not here —
    same split as ApprovalRule)."""
    __tablename__ = "verification_rules"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", "version", name="uq_verification_rule_org_name_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(default=True, index=True)
    status: Mapped[VerificationRuleStatus] = mapped_column(Enum(VerificationRuleStatus), default=VerificationRuleStatus.ACTIVE, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)  # lower wins a tie-break

    # Match conditions — AND'd together; subject_type=NULL means "any subject type".
    business_process: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # The THEN-clause: which VerificationCheckType values a compliant case
    # under this business_process must carry a VERIFIED/VERIFIED_WITH_
    # WARNINGS result for — see verification_rules_service.
    # evaluate_case_compliance(). Free list of check_type strings, not an
    # FK/enum column — validated in the service layer the same way
    # VerificationResult.provider_status_code is a free string validated
    # per check_type rather than a DB enum.
    required_check_types: Mapped[list] = mapped_column(JSON, default=list)

    # How many days a VERIFIED/VERIFIED_WITH_WARNINGS result stays fresh
    # before it needs re-verification — None = never expires. Only
    # applied to check_types this rule actually requires; see
    # verification_orchestrator.py's expires_at computation (IV20).
    freshness_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)  # no FK — User is PlatformBase, this table is TenantBase, same cross-boundary note as ApprovalRule.created_by
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<VerificationRule {self.name} v{self.version} ({self.business_process})>"
