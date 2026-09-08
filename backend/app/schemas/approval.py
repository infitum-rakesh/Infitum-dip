from datetime import datetime

from pydantic import BaseModel


# ── Rules ──────────────────────────────────────────────────────────

class ApprovalConditionIn(BaseModel):
    field: str
    operator: str
    value: object


class ApprovalConditionOut(BaseModel):
    id: int
    field: str
    operator: str
    value: object
    model_config = {"from_attributes": True}


class ApprovalStepIn(BaseModel):
    step_type: str
    step_number: int
    approver_type: str
    approver_role_id: int | None = None
    approver_user_id: int | None = None
    approver_department: str | None = None
    approver_pool: list[int] | None = None
    label: str | None = None
    sla_hours: int | None = None
    reminder_after_hours: int | None = None
    escalation_after_hours: int | None = None
    escalation_approver_type: str | None = None
    escalation_role_id: int | None = None
    escalation_user_id: int | None = None


class ApprovalStepOut(BaseModel):
    id: int
    step_type: str
    step_number: int
    approver_type: str
    approver_role_id: int | None
    approver_user_id: int | None
    approver_department: str | None
    approver_pool: list[int] | None
    label: str | None
    sla_hours: int | None
    reminder_after_hours: int | None
    escalation_after_hours: int | None
    escalation_approver_type: str | None
    escalation_role_id: int | None
    escalation_user_id: int | None
    model_config = {"from_attributes": True}


class ApprovalRuleIn(BaseModel):
    name: str
    description: str | None = None
    priority: int = 100
    status: str = "ACTIVE"
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None
    invoice_type: str | None = None
    action: str = "REQUIRE_APPROVAL"
    approval_required: bool = True
    approval_count: int = 1
    approval_mode: str = "SEQUENTIAL"
    peer_review_required: bool = False
    peer_review_count: int = 0
    peer_review_mode: str = "SEQUENTIAL"
    default_sla_hours: int | None = None
    conditions: list[ApprovalConditionIn] = []
    steps: list[ApprovalStepIn] = []


class ApprovalRuleOut(BaseModel):
    id: int
    organization_id: int | None
    name: str
    description: str | None
    version: int
    is_current: bool
    status: str
    priority: int
    effective_from: datetime | None
    effective_to: datetime | None
    min_amount: float | None
    max_amount: float | None
    currency: str | None
    invoice_type: str | None
    action: str
    approval_required: bool
    approval_count: int
    approval_mode: str
    peer_review_required: bool
    peer_review_count: int
    peer_review_mode: str
    default_sla_hours: int | None
    created_by: int | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ApprovalRuleDetailOut(ApprovalRuleOut):
    conditions: list[ApprovalConditionOut] = []
    steps: list[ApprovalStepOut] = []


class ApprovalRuleStatusUpdate(BaseModel):
    status: str  # "ACTIVE" | "INACTIVE"


class ApprovalRuleValidationErrorOut(BaseModel):
    errors: list[str]


class ApprovalSimulateRequest(BaseModel):
    amount: float | None = None
    currency: str | None = None
    invoice_type: str | None = None
    conditions: dict[str, str] = {}


class ApprovalEvaluationCheckOut(BaseModel):
    field: str
    operator: str
    expected: object
    actual: object
    passed: bool
    note: str


class ApprovalEvaluationOut(BaseModel):
    rule_id: int
    rule_name: str
    version: int
    organization_id: int | None
    priority: int
    specificity: int
    matched: bool
    checks: list[ApprovalEvaluationCheckOut]
    reason: str


class ApprovalSimulateResponse(BaseModel):
    candidates_considered: int
    evaluations: list[ApprovalEvaluationOut]
    matched_rules: list[ApprovalEvaluationOut]
    winner: ApprovalEvaluationOut | None
    conflict: bool
    conflicting_rule_ids: list[int]


# ── Workflows ──────────────────────────────────────────────────────

class ApprovalWorkflowCreateRequest(BaseModel):
    invoice_id: int
    amount: float | None = None
    currency: str | None = None
    invoice_type: str | None = None
    conditions: dict[str, str] = {}


class ApprovalWorkflowStepOut(BaseModel):
    id: int
    workflow_id: int
    step_number: int
    approver_type: str
    approver_role_id: int | None
    approver_user_id: int | None
    approver_department: str | None
    approver_pool: list[int] | None
    assigned_user_id: int | None
    status: str
    decision_comment: str | None
    acted_by: int | None
    acted_at: datetime | None
    sla_hours: int | None
    reminder_after_hours: int | None
    escalation_after_hours: int | None
    due_at: datetime | None
    assigned_at: datetime | None
    escalated_at: datetime | None
    # Real bug found live (user-reported): the Workflow Detail screen
    # rendered "User #3" for every approver/assignee/actor — the frontend
    # had nothing but the raw id to show. User/Role are PlatformBase (see
    # approver_resolution_service.py's own module docstring on why), so
    # this module can't just eager-load a relationship across that
    # boundary — api/approvals.py's get_workflow() batch-fetches these
    # three ids' full_names from the platform db and stamps them here,
    # same "populated by the API layer, never fabricated" convention
    # PAY12 used for invoice_number/vendor_name on ApprovalWorkflowOut.
    # None only when the id itself is None, or (rare) the user row was
    # since deleted — the frontend falls back to "User #{id}" only then.
    approver_user_name: str | None = None
    assigned_user_name: str | None = None
    acted_by_name: str | None = None
    model_config = {"from_attributes": True}


class PeerReviewStepOut(BaseModel):
    id: int
    workflow_id: int
    step_number: int
    approver_type: str
    approver_role_id: int | None
    approver_user_id: int | None
    approver_department: str | None
    approver_pool: list[int] | None
    assigned_user_id: int | None
    status: str
    review_comment: str | None
    reviewed_by: int | None
    reviewed_at: datetime | None
    sla_hours: int | None
    reminder_after_hours: int | None
    escalation_after_hours: int | None
    due_at: datetime | None
    assigned_at: datetime | None
    escalated_at: datetime | None
    # Same real bug/fix as ApprovalWorkflowStepOut above.
    approver_user_name: str | None = None
    assigned_user_name: str | None = None
    reviewed_by_name: str | None = None
    model_config = {"from_attributes": True}


class ApprovalWorkflowAuditLogOut(BaseModel):
    id: int
    event_type: str
    message: str | None
    details: dict | None
    actor_id: int | None
    created_at: datetime
    model_config = {"from_attributes": True}


class ApprovalWorkflowOut(BaseModel):
    id: int
    organization_id: int | None
    invoice_id: int
    # PAY22 — all three nullable: an AUTO_APPROVED workflow for an
    # invoice no ApprovalRule matched at all has no rule to report (see
    # the ApprovalWorkflow model's own comment on rule_id).
    rule_id: int | None
    rule_name: str | None
    rule_version: int | None
    status: str
    approval_required: bool
    approval_mode: str
    approval_count_required: int
    approval_count_completed: int
    current_approval_step_number: int | None
    peer_review_required: bool
    peer_review_mode: str
    peer_review_count_required: int
    peer_review_count_completed: int
    current_peer_review_step_number: int | None
    decision_reason: str | None
    created_by: int | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    # PAY12 — same "an approver/viewer needs to see WHAT this workflow is
    # about, not just an invoice_id" fix PAY10 applied to MyApprovalItemOut.
    # Populated by the API layer (list_workflows()/get_workflow() batch- or
    # single-fetch ApInvoice), never fabricated; None if the invoice was
    # since deleted.
    invoice_number: str | None = None
    vendor_name: str | None = None
    total: float | None = None
    currency: str | None = None
    model_config = {"from_attributes": True}


class ApprovalWorkflowDetailOut(ApprovalWorkflowOut):
    context_snapshot: dict | None
    evaluation_trace: dict | None
    steps: list[ApprovalWorkflowStepOut] = []
    peer_review_steps: list[PeerReviewStepOut] = []
    audit_log: list[ApprovalWorkflowAuditLogOut] = []


class ApprovalActionRequest(BaseModel):
    comment: str | None = None


class MyApprovalItemOut(BaseModel):
    step_id: int
    step_kind: str  # "APPROVAL" | "PEER_REVIEW"
    workflow_id: int
    invoice_id: int
    rule_name: str
    step_number: int
    assigned_at: datetime | None
    due_at: datetime | None
    # PAY10 — an approver needs to see WHAT they're approving without an
    # extra round trip per row; the API layer batch-fetches these from
    # ApInvoice (see api/approvals.py::my_approvals()), never fabricated.
    invoice_number: str | None = None
    vendor_name: str | None = None
    total: float | None = None
    currency: str | None = None
