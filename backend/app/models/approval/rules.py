"""
Payment Invoice Approval — Approval Rule Engine models (Phase 2).

Versioning follows the exact same "never mutate a published version in
place, publish a new row instead" convention already used by
ApInvoiceValidationRule (models/invoice_intelligence/rules.py, INV6) and
ColumnMappingVersion (CM1-14 project history): `is_current` marks the one
row currently in effect per (organization, name); every ApprovalWorkflow
(Phase 4) will record which rule id + version it was actually evaluated
against, so a later rule edit never silently rewrites the meaning of a
past workflow's decision (spec §8/§50-52).

Same "org row wins, else platform-default fallback" convention as
ApInvoiceValidationRule/OrganizationSettings: organization_id IS NULL
means a platform-wide default rule visible to every org unless that org
has its own override row for the same `name`.

Condition modeling is deliberately hybrid, not pure EAV and not pure
fixed-columns:
  - amount range, currency and invoice_type are the conditions every
    single rule in this feature actually needs (see the spec's own
    seed-rule examples), so they're first-class indexed columns on
    ApprovalRule itself for fast candidate-rule lookup.
  - every other dimension (supplier, business unit, department, cost
    center, company/legal entity, payment method, ERP/source system) is
    open-ended and rule-specific, so it lives in ApprovalRuleCondition —
    one row per (field, operator, value) — instead of bloating this
    table with a dozen mostly-NULL columns.

Approval steps and peer-review steps are modeled as ONE discriminated
table (`step_type`) at the RULE-DEFINITION level, since a rule's "THEN"
clause is really one ordered list of approver assignments with two
distinct purposes — not because approval and peer review are the same
concept (they are not; see the module docstring on Phase 4's
ApprovalWorkflowStep/PeerReviewStep, which ARE two separate tables at the
workflow-instance level, exactly as spec §14 requires). Keeping the rule
DEFINITION in one table avoids a second near-identical table here while
still keeping the two concepts numbered and queried independently
(step_number is scoped within step_type, not shared).
"""
import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ApprovalEntityType(str, enum.Enum):
    """TALLY-H — what kind of thing a rule/workflow gates. Added so the
    same rule-engine + workflow-instance machinery this whole feature
    built for invoice payment approval can be reused, unforked, for
    Tally voucher write-back authorization (spec's own directive: reuse
    the generic engine, never duplicate it, but keep the two kinds of
    approval logically separate). Every ApprovalRule/ApprovalWorkflow row
    predating this column is backfilled to INVOICE (see migrate_tally_h.py)
    — exactly the behavior every existing rule/workflow already had, so
    this is purely additive, never a behavior change for Payment Approval.

    resolve_candidate_rules() filters on this so a TALLY_VOUCHER context
    can never accidentally match an INVOICE-scoped rule (or vice versa) —
    without that filter, an org's existing unconditional invoice rule
    ("matches unconditionally, no conditions defined") would silently
    also claim every Tally voucher submitted for approval, which would be
    a real, dangerous correctness bug given this feature's own "no
    AI-generated data may post to Tally without explicit human
    authorization" requirement."""
    INVOICE = "INVOICE"
    TALLY_VOUCHER = "TALLY_VOUCHER"


class ApprovalRuleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class ApprovalMode(str, enum.Enum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL = "PARALLEL"  # accepted by the schema now; the evaluation engine (Phase 3) may implement SEQUENTIAL only first and disclose PARALLEL as not-yet-executed rather than fake it


class ApprovalRuleAction(str, enum.Enum):
    """The rule's THEN-clause verdict — kept as an explicit column (not
    only inferred from approval_required) so the rule-evaluation trace
    (spec §87) can display a plain-language verdict without recomputing it."""
    AUTO_APPROVE = "AUTO_APPROVE"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ApprovalConditionField(str, enum.Enum):
    """Extension conditions beyond ApprovalRule's own first-class amount/
    currency/invoice_type columns. TENANT is intentionally omitted here —
    tenant scoping is organization_id itself, not a condition row."""
    SUPPLIER = "SUPPLIER"
    BUSINESS_UNIT = "BUSINESS_UNIT"
    DEPARTMENT = "DEPARTMENT"
    COST_CENTER = "COST_CENTER"
    COMPANY = "COMPANY"
    PAYMENT_METHOD = "PAYMENT_METHOD"
    SOURCE_SYSTEM = "SOURCE_SYSTEM"


class ApprovalConditionOperator(str, enum.Enum):
    EQ = "EQ"
    NEQ = "NEQ"
    IN = "IN"          # value is a JSON list
    NOT_IN = "NOT_IN"  # value is a JSON list


class ApprovalStepType(str, enum.Enum):
    APPROVAL = "APPROVAL"
    PEER_REVIEW = "PEER_REVIEW"


class ApproverType(str, enum.Enum):
    """MANAGER and a configured round-robin POOL policy are both named in
    the spec (§40); this codebase has no manager hierarchy on User today
    (no manager_id column exists anywhere) so MANAGER is deliberately not
    offered here — disclosed limitation, not faked. POOL is a plain list
    of user ids with a resolution policy applied at assignment time
    (Phase 5), not a new first-class group entity."""
    ROLE = "ROLE"              # any active User with custom_role_id == approver_role_id
    USER = "USER"              # exactly approver_user_id
    DEPARTMENT = "DEPARTMENT"  # any active User with department == approver_department
    POOL = "POOL"              # any active User whose id is in approver_pool, per a configured resolution policy


class ApprovalRule(TenantBase):
    __tablename__ = "approval_rules"
    # sqlite_autoincrement: this table accumulates FK children
    # (ApprovalRuleCondition, ApprovalRuleStep, ApprovalRuleAuditLog, and
    # now ApprovalWorkflow — models/approval/workflow.py, Phase 4) —
    # protects against the SQLite id-reuse collision already found live
    # twice in this project (organizations, pipelines). Same precedent
    # as ApInvoice/Pipeline. No-op on Postgres. Added retroactively here
    # while building Phase 4, which is the first table that actually
    # grows this table a real FK child from OUTSIDE this file.
    __table_args__ = (
        UniqueConstraint("organization_id", "name", "version", "entity_type", name="uq_approval_rule_org_name_version_entity"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    # TALLY-H — scopes this rule to one kind of approval. Two entity types
    # may reuse the same rule `name` independently (each has its own
    # version lineage) — that's why entity_type joined the uniqueness key
    # above rather than name alone.
    entity_type: Mapped[ApprovalEntityType] = mapped_column(Enum(ApprovalEntityType), default=ApprovalEntityType.INVOICE, index=True)

    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[ApprovalRuleStatus] = mapped_column(Enum(ApprovalRuleStatus), default=ApprovalRuleStatus.ACTIVE, index=True)

    # Explicit tie-break when more than one active rule's conditions match
    # the same invoice (spec §5) — LOWER number wins (priority 1 beats
    # priority 10). The rule engine (Phase 3) also applies the documented
    # specificity ladder (tenant-specific > supplier-specific > ...
    # generic) before ever falling back to this raw number; two equally
    # specific matching rules with equal priority is a genuine conflict,
    # never resolved by picking one arbitrarily (spec §5's RULE_CONFLICT).
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)

    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    # First-class conditions — every seed/demo rule in the spec keys off
    # these three; None means "matches any" for that dimension (e.g.
    # currency=None matches every currency — see Phase 1 analysis §71,
    # Option A: rules apply per-currency, no FX conversion in this build).
    min_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_amount: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = open-ended ("500,000+")
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    invoice_type: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)

    # THEN clause
    action: Mapped[ApprovalRuleAction] = mapped_column(Enum(ApprovalRuleAction), default=ApprovalRuleAction.REQUIRE_APPROVAL)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    approval_count: Mapped[int] = mapped_column(Integer, default=0)  # 0 when action=AUTO_APPROVE
    approval_mode: Mapped[ApprovalMode] = mapped_column(Enum(ApprovalMode), default=ApprovalMode.SEQUENTIAL)

    peer_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    peer_review_count: Mapped[int] = mapped_column(Integer, default=0)
    peer_review_mode: Mapped[ApprovalMode] = mapped_column(Enum(ApprovalMode), default=ApprovalMode.SEQUENTIAL)

    # Falls back to on the individual ApprovalRuleStep's own sla_hours
    # when that step doesn't override it (Phase 4/7).
    default_sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # No ForeignKey("users.id") wrapper — User is PlatformBase, this table
    # is TenantBase. Real FKs never cross that boundary anywhere in this
    # codebase (see models/document.py's uploaded_by_id, and the WW3 "75-FK
    # sweep" that stripped exactly this class of wrapper everywhere else) —
    # a physically separate per-tenant database (BYO-Database) can't
    # enforce a cross-database FK constraint, and even same-database,
    # PlatformBase.metadata/TenantBase.metadata are two separate
    # create_all() calls that can't resolve a cross-metadata FK at all.
    created_by: Mapped[int | None] = mapped_column(nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ApprovalRule {self.name} v{self.version} ({self.status})>"


class ApprovalRuleCondition(TenantBase):
    """Extension conditions beyond ApprovalRule's own amount/currency/
    invoice_type columns — one row per (field, operator, value); all
    conditions on a rule are AND'd together (spec's rule-builder examples
    never show OR)."""
    __tablename__ = "approval_rule_conditions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("approval_rules.id"), nullable=False, index=True)

    field: Mapped[ApprovalConditionField] = mapped_column(Enum(ApprovalConditionField), nullable=False)
    operator: Mapped[ApprovalConditionOperator] = mapped_column(Enum(ApprovalConditionOperator), default=ApprovalConditionOperator.EQ)
    value: Mapped[dict | list | str] = mapped_column(JSON, nullable=False)  # scalar string, or a JSON list for IN/NOT_IN

    def __repr__(self) -> str:
        return f"<ApprovalRuleCondition rule={self.rule_id} {self.field}:{self.operator}>"


class ApprovalRuleStep(TenantBase):
    """One row per approver assignment in a rule's THEN clause — see
    module docstring for why APPROVAL and PEER_REVIEW share this table at
    the rule-definition level while staying two separate tables at the
    workflow-instance level (Phase 4)."""
    __tablename__ = "approval_rule_steps"
    # sqlite_autoincrement: now an FK target too (ApprovalWorkflowStep/
    # PeerReviewStep.rule_step_id, Phase 4) — same reasoning as
    # ApprovalRule's __table_args__ comment above.
    __table_args__ = (
        UniqueConstraint("rule_id", "step_type", "step_number", name="uq_approval_rule_step"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("approval_rules.id"), nullable=False, index=True)

    step_type: Mapped[ApprovalStepType] = mapped_column(Enum(ApprovalStepType), default=ApprovalStepType.APPROVAL, index=True)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based, scoped within (rule_id, step_type)

    # No ForeignKey wrapper on approver_role_id/approver_user_id — same
    # cross-boundary reason as ApprovalRule.created_by above (Role and
    # User are both PlatformBase).
    approver_type: Mapped[ApproverType] = mapped_column(Enum(ApproverType), nullable=False)
    approver_role_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    approver_user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    approver_department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approver_pool: Mapped[list | None] = mapped_column(JSON, nullable=True)  # list[int] of user ids

    # Display snapshot only (e.g. "Finance Manager", "CFO") — the actual
    # resolution always goes through approver_type/_role_id/_user_id at
    # assignment time (Phase 5), never through this label.
    label: Mapped[str | None] = mapped_column(String(150), nullable=True)

    sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)  # overrides ApprovalRule.default_sla_hours when set
    reminder_after_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    escalation_after_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)

    escalation_approver_type: Mapped[ApproverType | None] = mapped_column(Enum(ApproverType), nullable=True)
    escalation_role_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    escalation_user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    def __repr__(self) -> str:
        return f"<ApprovalRuleStep rule={self.rule_id} {self.step_type}#{self.step_number}>"


class ApprovalRuleAuditEventType(str, enum.Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    VERSION_PUBLISHED = "VERSION_PUBLISHED"
    ACTIVATED = "ACTIVATED"
    DEACTIVATED = "DEACTIVATED"
    DELETED = "DELETED"
    SIMULATION_RUN = "SIMULATION_RUN"


class ApprovalRuleAuditLog(TenantBase):
    """Mirrors RoleAuditLog's shape (models/role.py). Keyed by the stable
    `rule_name` (the (organization_id, name) family), not just one
    version's row id, so history survives across version bumps — a rule
    version being superseded must never look like its history vanished."""
    __tablename__ = "approval_rule_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    rule_name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    rule_version_id: Mapped[int | None] = mapped_column(ForeignKey("approval_rules.id"), nullable=True, index=True)

    event_type: Mapped[ApprovalRuleAuditEventType] = mapped_column(Enum(ApprovalRuleAuditEventType), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # No ForeignKey("users.id") wrapper — same cross-boundary reason as
    # ApprovalRule.created_by above (User is PlatformBase, this table is
    # TenantBase).
    actor_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ApprovalRuleAuditLog {self.rule_name} {self.event_type}>"
