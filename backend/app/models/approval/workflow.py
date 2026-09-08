"""
Payment Invoice Approval — Workflow INSTANCE models (Phase 4).

Phase 2 (rules.py) modeled the rule DEFINITION — what SHOULD happen for
an invoice matching certain conditions. This module models what actually
DID/IS happening for one real invoice: one ApprovalWorkflow row per
invoice that entered approval, created from a Phase 3
rule_engine_service.evaluate() winner, plus the concrete step-by-step
record of who needs to act and what they did.

Reproducibility is the same non-negotiable this whole feature has kept
since Phase 2: ApprovalWorkflow records rule_id + rule_version + a
snapshot of the approval_required/approval_mode/approval_count/
peer_review_* fields AT CREATION TIME, and every step snapshots its own
approver_type/approver_*_id/sla_* fields from the ApprovalRuleStep it
was generated from. None of these are ever re-read live off the rule —
a rule edited or even deleted after a workflow started must never
silently change what that in-flight or historical workflow means. This
is exactly why ApprovalRuleAuditLog stores rule_name/rule_version_id
instead of assuming the current row will always exist, applied one
layer up.

APPROVAL and PEER_REVIEW share ONE discriminated table at the rule-
DEFINITION level (ApprovalRuleStep, Phase 2) but split into two
genuinely separate tables here at the workflow-INSTANCE level, exactly
as that model's own docstring promised and spec §14 requires: peer
review is an acknowledgement/flagging pass (REVIEWED/FLAGGED), not an
approve/reject gate, and forcing it through ApprovalWorkflowStep's
APPROVED/REJECTED vocabulary would misrepresent what actually happened.

Explicitly OUT of scope for this phase (disclosed, not faked):
  - Resolving approver_type (ROLE/DEPARTMENT/POOL) to a concrete
    assigned_user_id — Phase 5's job. assigned_user_id sits NULL on
    every step until then; nothing here pretends resolution already
    happened.
  - Actually creating ApprovalWorkflow rows from an evaluated invoice,
    advancing current_*_step_number, or writing audit log rows — that's
    the workflow ENGINE (a later phase), which will consume
    rule_engine_service.evaluate()'s EvaluationTrace as
    context_snapshot/evaluation_trace below. This phase is schema only,
    same split as rules.py (Phase 2, models) vs rule_engine_service.py
    (Phase 3, logic).
  - SLA reminder/escalation scheduling — due_at/escalated_at columns
    exist as the natural home for that data (same "column exists,
    disclosed not yet acted on" pattern as the Document taxonomy's bbox
    fields before INV4 wired evidence linking); actually wired up in
    Phase 7's sla_scheduler_service.py, which also needed one small
    additive column this file didn't originally have — assigned_at
    (see ApprovalWorkflowStep/PeerReviewStep's own field comment).
"""
import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase
from app.models.approval.rules import ApprovalMode, ApproverType


class ApprovalWorkflowStatus(str, enum.Enum):
    PENDING = "PENDING"          # created, not yet actively worked (e.g. first step not yet assigned)
    IN_PROGRESS = "IN_PROGRESS"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"      # withdrawn (e.g. the underlying invoice was voided) — not a step decision
    EXPIRED = "EXPIRED"
    # PAY22 — the user's own explicit ask: a workflow record for EVERY
    # invoice that reaches the decision engine while PAYMENT_APPROVAL is
    # active, not just the ones an ApprovalRule actually claims. Before
    # this, create_workflow() returned None for "no rule matched" / "the
    # matched rule needs neither gate" and nothing was ever persisted —
    # correct for the approval/posting logic (nothing needed to block
    # posting) but left no durable, listable record that this invoice was
    # ever evaluated at all. AUTO_APPROVED is a terminal, zero-step
    # status stamped immediately at creation (no PENDING/IN_PROGRESS
    # window — there is no human step to wait on). It's deliberately
    # excluded from every "does this need to block ERP posting" check
    # (see invoice_decision_service.decide()'s own status-set check),
    # so this is additive audit visibility only — it changes nothing
    # about which invoices actually need a human.
    AUTO_APPROVED = "AUTO_APPROVED"


class ApprovalStepStatus(str, enum.Enum):
    PENDING = "PENDING"      # not yet this step's turn (SEQUENTIAL mode, a later step_number)
    ASSIGNED = "ASSIGNED"    # this step is active and awaiting the approver's action
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"      # e.g. PARALLEL M-of-N already satisfied by other steps
    ESCALATED = "ESCALATED"
    EXPIRED = "EXPIRED"


class PeerReviewStatus(str, enum.Enum):
    """Deliberately distinct verbs from ApprovalStepStatus — see module
    docstring on why peer review is a genuinely different concept."""
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    REVIEWED = "REVIEWED"
    FLAGGED = "FLAGGED"
    SKIPPED = "SKIPPED"
    ESCALATED = "ESCALATED"
    EXPIRED = "EXPIRED"


class ApprovalWorkflowAuditEventType(str, enum.Enum):
    CREATED = "CREATED"
    STEP_ASSIGNED = "STEP_ASSIGNED"
    STEP_APPROVED = "STEP_APPROVED"
    STEP_REJECTED = "STEP_REJECTED"
    STEP_SKIPPED = "STEP_SKIPPED"
    PEER_REVIEW_ASSIGNED = "PEER_REVIEW_ASSIGNED"
    PEER_REVIEW_REVIEWED = "PEER_REVIEW_REVIEWED"
    PEER_REVIEW_FLAGGED = "PEER_REVIEW_FLAGGED"
    ESCALATED = "ESCALATED"
    REMINDER_SENT = "REMINDER_SENT"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    # Phase 5 (approver_resolution_service.py): a step's approver_type/ids
    # resolved to ZERO active eligible users — a misconfigured rule
    # naming a role nobody currently holds, an empty pool, a deactivated
    # specific user, ... Recorded honestly instead of silently leaving the
    # step PENDING forever with no trace of why. Safe to add post-hoc:
    # this column has no DB-level CHECK constraint (plain VARCHAR(20) on
    # SQLite, see every other enum column in this feature), and this
    # value's length fits the column width already baked into the table.
    RESOLUTION_FAILED = "RESOLUTION_FAILED"
    # PAY16 — the idempotency guard create_workflow() now runs before ever
    # touching the rule engine: a second create_workflow() call for an
    # invoice that already has a PENDING/IN_PROGRESS/APPROVED workflow is
    # suppressed rather than creating a duplicate row. Logged on the
    # EXISTING workflow (not a new one — none was created), so a
    # duplicate-triggering caller (scheduler retry, double-processed
    # source record, a stray manual API call) leaves a durable trace.
    # 20 chars — fits the column width RESOLUTION_FAILED/PEER_REVIEW_
    # REVIEWED already established, no width concern.
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
    # PAY22 — stamped once, at creation, on the synthetic workflow row
    # create_workflow() now returns instead of None for an invoice no
    # rule claimed (or a matched rule needing neither gate). See
    # ApprovalWorkflowStatus.AUTO_APPROVED for what this status means.
    AUTO_APPROVED = "AUTO_APPROVED"


class ApprovalWorkflow(TenantBase):
    __tablename__ = "approval_workflows"
    # Accumulates FK children (ApprovalWorkflowStep, PeerReviewStep,
    # ApprovalWorkflowAuditLog) — same SQLite id-reuse protection as
    # ApInvoice/Pipeline, see either's __table_args__ comment for the
    # exact failure mode this prevents. No-op on Postgres.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    # Real FK — ApInvoice is TenantBase, same DB, no cross-boundary issue.
    # Deliberately NOT unique: a REJECTED invoice can be corrected and
    # resubmitted, creating a second ApprovalWorkflow row for the same
    # invoice — "at most one ACTIVE (PENDING/IN_PROGRESS) workflow per
    # invoice at a time" is a service-layer invariant (a later phase),
    # not a DB constraint, since history must keep every past attempt.
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ap_invoices.id"), nullable=False, index=True)

    # Which rule (+ exact published version) actually produced this
    # workflow, recorded once and never re-derived — see module
    # docstring. rule_name is a denormalized display copy, same
    # convention as ApprovalRuleAuditLog.rule_name.
    #
    # PAY22 — all three made nullable: an AUTO_APPROVED row (status
    # above) exists for the "no rule matched at all" case too, and there
    # genuinely is no rule to reference there. NULL rule_id means
    # exactly that — not a data-integrity gap, a real "nothing claimed
    # this invoice" fact worth keeping distinct from "a rule claimed it
    # but needed no gate" (rule_id populated, still AUTO_APPROVED).
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("approval_rules.id"), nullable=True, index=True)
    rule_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    rule_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[ApprovalWorkflowStatus] = mapped_column(Enum(ApprovalWorkflowStatus), default=ApprovalWorkflowStatus.PENDING, index=True)

    # Snapshotted from the matched ApprovalRule at creation time.
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    approval_mode: Mapped[ApprovalMode] = mapped_column(Enum(ApprovalMode), default=ApprovalMode.SEQUENTIAL)
    approval_count_required: Mapped[int] = mapped_column(Integer, default=0)
    approval_count_completed: Mapped[int] = mapped_column(Integer, default=0)
    current_approval_step_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    peer_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    peer_review_mode: Mapped[ApprovalMode] = mapped_column(Enum(ApprovalMode), default=ApprovalMode.SEQUENTIAL)
    peer_review_count_required: Mapped[int] = mapped_column(Integer, default=0)
    peer_review_count_completed: Mapped[int] = mapped_column(Integer, default=0)
    current_peer_review_step_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The exact ApprovalMatchContext + EvaluationTrace
    # (rule_engine_service.py, Phase 3) that selected this rule — a
    # durable explainability record independent of whatever the
    # invoice's live fields say later (spec §87's rule-evaluation trace,
    # made persistent instead of only computable on demand).
    context_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evaluation_trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # No ForeignKey("users.id") wrapper — User is PlatformBase, this
    # table is TenantBase; see rules.py's ApprovalRule.created_by for
    # the full cross-boundary explanation, unchanged here.
    created_by: Mapped[int | None] = mapped_column(nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ApprovalWorkflow invoice={self.invoice_id} rule={self.rule_name} v{self.rule_version} status={self.status}>"


class ApprovalWorkflowStep(TenantBase):
    """One row per approver-assignment INSTANCE for one ApprovalWorkflow
    — see module docstring for why this stays genuinely separate from
    PeerReviewStep below."""
    __tablename__ = "approval_workflow_steps"
    __table_args__ = (UniqueConstraint("workflow_id", "step_number", name="uq_approval_workflow_step"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("approval_workflows.id"), nullable=False, index=True)
    rule_step_id: Mapped[int] = mapped_column(ForeignKey("approval_rule_steps.id"), nullable=False, index=True)

    step_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Assignment definition, snapshotted from the ApprovalRuleStep this
    # instance was generated from — Phase 5's approver resolution
    # resolves THESE values, not the live rule step (which could have
    # been edited/republished since this workflow started).
    approver_type: Mapped[ApproverType] = mapped_column(Enum(ApproverType), nullable=False)
    approver_role_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    approver_user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    approver_department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approver_pool: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # The concrete user this step is actually assigned to — NULL until
    # Phase 5's approver-resolution logic runs. ROLE/DEPARTMENT/POOL all
    # need a real lookup against Users that doesn't exist yet in this
    # build; disclosed gap, not a fabricated assignment.
    assigned_user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    # The real SLA-clock-start timestamp — stamped by
    # approver_resolution_service.resolve_and_assign_step() the moment
    # this step actually becomes ASSIGNED (Phase 7). Deliberately NOT
    # updated_at, which changes on any field edit, not just assignment.
    # Re-stamped on a successful escalation (Phase 7's sla_scheduler_
    # service.py), so the new assignee gets a fresh SLA window rather
    # than inheriting the original assignee's already-elapsed one.
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[ApprovalStepStatus] = mapped_column(Enum(ApprovalStepStatus), default=ApprovalStepStatus.PENDING, index=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # No ForeignKey — User is PlatformBase.
    acted_by: Mapped[int | None] = mapped_column(nullable=True, index=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # SLA snapshot — same reproducibility reason as approver_type above.
    # A later phase's reminder/escalation scheduler reads these, not the
    # live ApprovalRuleStep.
    sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reminder_after_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    escalation_after_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    escalation_approver_type: Mapped[ApproverType | None] = mapped_column(Enum(ApproverType), nullable=True)
    escalation_role_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    escalation_user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ApprovalWorkflowStep workflow={self.workflow_id} #{self.step_number} {self.status}>"


class PeerReviewStep(TenantBase):
    """Peer review's own workflow-instance table — deliberately NOT
    reused as ApprovalWorkflowStep rows even though the shape rhymes,
    because peer review is a genuinely different concept: an
    acknowledgement/flagging pass (REVIEWED/FLAGGED), not an
    approve/reject gate. See ApprovalStepType's docstring
    (models/approval/rules.py) and PeerReviewStatus above."""
    __tablename__ = "peer_review_steps"
    __table_args__ = (UniqueConstraint("workflow_id", "step_number", name="uq_peer_review_step"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("approval_workflows.id"), nullable=False, index=True)
    rule_step_id: Mapped[int] = mapped_column(ForeignKey("approval_rule_steps.id"), nullable=False, index=True)

    step_number: Mapped[int] = mapped_column(Integer, nullable=False)

    approver_type: Mapped[ApproverType] = mapped_column(Enum(ApproverType), nullable=False)
    approver_role_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    approver_user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    approver_department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approver_pool: Mapped[list | None] = mapped_column(JSON, nullable=True)

    assigned_user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # NULL until Phase 5, same as ApprovalWorkflowStep
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # SLA-clock start — see ApprovalWorkflowStep's identical field for the full explanation

    status: Mapped[PeerReviewStatus] = mapped_column(Enum(PeerReviewStatus), default=PeerReviewStatus.PENDING, index=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(nullable=True, index=True)  # no FK — User is PlatformBase
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reminder_after_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    escalation_after_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    escalation_approver_type: Mapped[ApproverType | None] = mapped_column(Enum(ApproverType), nullable=True)
    escalation_role_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    escalation_user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<PeerReviewStep workflow={self.workflow_id} #{self.step_number} {self.status}>"


class ApprovalWorkflowAuditLog(TenantBase):
    """Mirrors ApprovalRuleAuditLog's shape one layer up — one durable,
    append-only row per workflow-lifecycle event, independent of
    whatever ApprovalWorkflow.status/step statuses say NOW."""
    __tablename__ = "approval_workflow_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    workflow_id: Mapped[int] = mapped_column(ForeignKey("approval_workflows.id"), nullable=False, index=True)
    # Denormalized — lets a future Invoice Detail screen pull approval
    # history for one invoice without an extra join through
    # approval_workflows first.
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ap_invoices.id"), nullable=False, index=True)

    event_type: Mapped[ApprovalWorkflowAuditEventType] = mapped_column(Enum(ApprovalWorkflowAuditEventType), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    actor_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # no FK — User is PlatformBase
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ApprovalWorkflowAuditLog workflow={self.workflow_id} {self.event_type}>"
