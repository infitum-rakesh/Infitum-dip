"""
Payment Invoice Approval Workflow router — /api/approvals/... (Phase 8).

The first externally-reachable surface for this feature (Phases 2-7 were
all models/services). Rule CRUD/versioning sits on approval_rule_service.py;
workflow creation and every approve/reject/review/flag action sits on
approval_workflow_engine_service.py (the real state-machine Phases 4-7
each deferred). Nothing here re-implements resolution, notification, or
SLA logic — this router is thin.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import AsyncSessionLocal
from app.database.tenant_routing import get_tenant_db
from app.models.approval.rules import (
    ApprovalConditionField,
    ApprovalConditionOperator,
    ApprovalMode,
    ApprovalRuleAction,
    ApprovalRuleStatus,
    ApprovalStepType,
    ApproverType,
)
from app.models.approval.workflow import (
    ApprovalStepStatus,
    ApprovalWorkflow,
    ApprovalWorkflowAuditLog,
    ApprovalWorkflowStep,
    PeerReviewStatus,
    PeerReviewStep,
)
from app.models.user import User, UserRole
from app.schemas.approval import (
    ApprovalActionRequest,
    ApprovalConditionIn,
    ApprovalEvaluationCheckOut,
    ApprovalEvaluationOut,
    ApprovalRuleDetailOut,
    ApprovalRuleIn,
    ApprovalRuleOut,
    ApprovalRuleStatusUpdate,
    ApprovalSimulateRequest,
    ApprovalSimulateResponse,
    ApprovalStepIn,
    ApprovalWorkflowAuditLogOut,
    ApprovalWorkflowCreateRequest,
    ApprovalWorkflowDetailOut,
    ApprovalWorkflowOut,
    ApprovalWorkflowStepOut,
    MyApprovalItemOut,
    PeerReviewStepOut,
)
from app.services.approval import approval_rule_service as rule_svc
from app.services.approval import approval_workflow_engine_service as engine_svc
from app.services.approval import rule_engine_service as engine

router = APIRouter(prefix="/approvals", tags=["approvals"])

APPROVAL_MODULE = "PAYMENT_APPROVAL"


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")


def _bad_request(detail) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail)


# ── enum parsing helpers — a bad string in the request body should be a
# clean 400, never a 500 from an uncaught ValueError inside the enum ──

def _enum(cls, value, field_name: str):
    try:
        return cls(value)
    except ValueError:
        valid = ", ".join(m.value for m in cls)
        raise _bad_request(f"Invalid {field_name} '{value}' — expected one of: {valid}.")


def _to_rule_input(body: ApprovalRuleIn) -> rule_svc.RuleInput:
    conditions = [
        rule_svc.ConditionInput(
            field=_enum(ApprovalConditionField, c.field, "condition field"),
            operator=_enum(ApprovalConditionOperator, c.operator, "condition operator"),
            value=c.value,
        )
        for c in body.conditions
    ]
    steps = [
        rule_svc.StepInput(
            step_type=_enum(ApprovalStepType, s.step_type, "step_type"),
            step_number=s.step_number,
            approver_type=_enum(ApproverType, s.approver_type, "approver_type"),
            approver_role_id=s.approver_role_id, approver_user_id=s.approver_user_id,
            approver_department=s.approver_department, approver_pool=s.approver_pool, label=s.label,
            sla_hours=s.sla_hours, reminder_after_hours=s.reminder_after_hours, escalation_after_hours=s.escalation_after_hours,
            escalation_approver_type=_enum(ApproverType, s.escalation_approver_type, "escalation_approver_type") if s.escalation_approver_type else None,
            escalation_role_id=s.escalation_role_id, escalation_user_id=s.escalation_user_id,
        )
        for s in body.steps
    ]
    return rule_svc.RuleInput(
        name=body.name, description=body.description, priority=body.priority,
        status=_enum(ApprovalRuleStatus, body.status, "status"),
        effective_from=body.effective_from, effective_to=body.effective_to,
        min_amount=body.min_amount, max_amount=body.max_amount, currency=body.currency, invoice_type=body.invoice_type,
        action=_enum(ApprovalRuleAction, body.action, "action"), approval_required=body.approval_required,
        approval_count=body.approval_count, approval_mode=_enum(ApprovalMode, body.approval_mode, "approval_mode"),
        peer_review_required=body.peer_review_required, peer_review_count=body.peer_review_count,
        peer_review_mode=_enum(ApprovalMode, body.peer_review_mode, "peer_review_mode"),
        default_sla_hours=body.default_sla_hours, conditions=conditions, steps=steps,
    )


def _evaluation_out(e: engine.RuleEvaluation) -> ApprovalEvaluationOut:
    return ApprovalEvaluationOut(
        rule_id=e.rule_id, rule_name=e.rule_name, version=e.version, organization_id=e.organization_id,
        priority=e.priority, specificity=e.specificity, matched=e.matched, reason=e.reason,
        checks=[ApprovalEvaluationCheckOut(field=c.field, operator=c.operator, expected=c.expected, actual=c.actual, passed=c.passed, note=c.note) for c in e.checks],
    )


# ── Rules ────────────────────────────────────────────────────────────

@router.get("/rules", response_model=list[ApprovalRuleOut])
async def list_rules(
    user: User = Depends(require_action("VIEW", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    return await rule_svc.list_rules_for_admin(db, _effective_org_id(user))


@router.get("/rules/{rule_id}", response_model=ApprovalRuleDetailOut)
async def get_rule(
    rule_id: int, user: User = Depends(require_action("VIEW", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    rule = await rule_svc.get_rule(db, _effective_org_id(user), rule_id)
    if rule is None:
        raise _not_found()
    conditions, steps = await rule_svc.get_rule_children(db, rule.id)
    out = ApprovalRuleDetailOut.model_validate(rule)
    out.conditions = conditions
    out.steps = steps
    return out


@router.post("/rules", response_model=ApprovalRuleOut, status_code=status.HTTP_201_CREATED)
async def publish_rule(
    body: ApprovalRuleIn, user: User = Depends(require_action("CREATE", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    payload = _to_rule_input(body)
    try:
        return await rule_svc.publish_rule(db, _effective_org_id(user), payload, user.id)
    except rule_svc.ApprovalRuleServiceError as exc:
        raise _bad_request({"errors": exc.errors})


@router.post("/rules/{rule_id}/status", response_model=ApprovalRuleOut)
async def set_rule_status(
    rule_id: int, body: ApprovalRuleStatusUpdate,
    user: User = Depends(require_action("EDIT", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    new_status = _enum(ApprovalRuleStatus, body.status, "status")
    try:
        return await rule_svc.set_rule_status(db, _effective_org_id(user), rule_id, new_status, user.id)
    except rule_svc.ApprovalRuleServiceError as exc:
        raise _bad_request({"errors": exc.errors})


@router.post("/rules/simulate", response_model=ApprovalSimulateResponse)
async def simulate_rule(
    body: ApprovalSimulateRequest, user: User = Depends(require_action("EXECUTE", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    context = engine.ApprovalMatchContext(amount=body.amount, currency=body.currency, invoice_type=body.invoice_type, conditions=body.conditions)
    trace = await rule_svc.simulate_rule(db, _effective_org_id(user), context, user.id)
    return ApprovalSimulateResponse(
        candidates_considered=trace.candidates_considered,
        evaluations=[_evaluation_out(e) for e in trace.evaluations],
        matched_rules=[_evaluation_out(e) for e in trace.matched_rules],
        winner=_evaluation_out(trace.winner) if trace.winner else None,
        conflict=trace.conflict, conflicting_rule_ids=trace.conflicting_rule_ids,
    )


# ── Workflows ──────────────────────────────────────────────────────

async def _load_workflow(db: AsyncSession, organization_id: int | None, workflow_id: int) -> ApprovalWorkflow:
    workflow = await db.get(ApprovalWorkflow, workflow_id)
    if workflow is None:
        raise _not_found()
    if organization_id is not None and workflow.organization_id is not None and workflow.organization_id != organization_id:
        raise _not_found()
    return workflow


async def _user_name_map(ids: set[int]) -> dict[int, str]:
    """Real bug found live (user-reported): the Workflow Detail screen
    showed 'User #3' instead of a real name — get_workflow() below only
    had raw ids to hand the frontend. Same idiom as
    review_queue_service._user_name_map()/document_service.py: User is a
    PlatformBase model (see approver_resolution_service.py's own module
    docstring), so this opens its own platform-db session rather than
    reusing the tenant-routed `db` every route here receives."""
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


def _with_invoice_fields(out: ApprovalWorkflowOut, invoice, organization_id: int | None) -> ApprovalWorkflowOut:
    """PAY12 — stamps the invoice_number/vendor_name/total/currency PAY12
    added to ApprovalWorkflowOut. Kept as a tiny shared helper rather than
    duplicated inline in list_workflows()/get_workflow() (batch-fetch vs.
    single-fetch respectively).

    PAY13 — tenant-isolation hardening: also re-checks invoice.organization_id
    against the caller's own org before stamping anything. workflow.invoice_id
    is normally trustworthy (create_workflow() now validates it — see that
    module's own PAY13 note), but this is the actual data-exposure point, so
    it gets its own independent check rather than relying solely on the
    write-side fix — a workflow created before that fix, or created via any
    future path that forgets the check, must still not leak another org's
    invoice fields here."""
    if invoice is not None and (invoice.organization_id is None or organization_id is None or invoice.organization_id == organization_id):
        out.invoice_number = invoice.invoice_number
        out.vendor_name = invoice.vendor_name
        out.total = invoice.total
        out.currency = invoice.currency
    return out


@router.get("/workflows", response_model=list[ApprovalWorkflowOut])
async def list_workflows(
    status_filter: str | None = Query(None, alias="status"),
    user: User = Depends(require_action("VIEW", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    from app.models.invoice_intelligence.core import ApInvoice

    stmt = select(ApprovalWorkflow)
    org_id = _effective_org_id(user)
    if org_id is not None:
        stmt = stmt.where(ApprovalWorkflow.organization_id == org_id)
    if status_filter:
        stmt = stmt.where(ApprovalWorkflow.status == status_filter)
    stmt = stmt.order_by(ApprovalWorkflow.created_at.desc())
    workflows = (await db.execute(stmt)).scalars().all()

    invoice_ids = {wf.invoice_id for wf in workflows}
    invoices = {inv.id: inv for inv in (await db.execute(select(ApInvoice).where(ApInvoice.id.in_(invoice_ids)))).scalars().all()} if invoice_ids else {}
    return [_with_invoice_fields(ApprovalWorkflowOut.model_validate(wf), invoices.get(wf.invoice_id), org_id) for wf in workflows]


@router.get("/workflows/{workflow_id}", response_model=ApprovalWorkflowDetailOut)
async def get_workflow(
    workflow_id: int, user: User = Depends(require_action("VIEW", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    from app.models.invoice_intelligence.core import ApInvoice

    org_id = _effective_org_id(user)
    workflow = await _load_workflow(db, org_id, workflow_id)
    steps = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == workflow_id).order_by(ApprovalWorkflowStep.step_number))).scalars().all()
    peer_steps = (await db.execute(select(PeerReviewStep).where(PeerReviewStep.workflow_id == workflow_id).order_by(PeerReviewStep.step_number))).scalars().all()
    audit = (await db.execute(select(ApprovalWorkflowAuditLog).where(ApprovalWorkflowAuditLog.workflow_id == workflow_id).order_by(ApprovalWorkflowAuditLog.created_at))).scalars().all()
    invoice = await db.get(ApInvoice, workflow.invoice_id)

    # Real bug found live (user-reported): the Approver/Assigned To/Acted
    # columns on the Workflow Detail screen showed "User #3" — stamp real
    # full_names the same way _with_invoice_fields() stamps invoice
    # fields below, one batch platform-db lookup for every user id this
    # workflow's steps reference, rather than N+1 lookups.
    user_ids = {uid for s in steps for uid in (s.approver_user_id, s.assigned_user_id, s.acted_by) if uid is not None}
    user_ids |= {uid for s in peer_steps for uid in (s.approver_user_id, s.assigned_user_id, s.reviewed_by) if uid is not None}
    names = await _user_name_map(user_ids)

    step_outs = []
    for s in steps:
        so = ApprovalWorkflowStepOut.model_validate(s)
        so.approver_user_name = names.get(s.approver_user_id)
        so.assigned_user_name = names.get(s.assigned_user_id)
        so.acted_by_name = names.get(s.acted_by)
        step_outs.append(so)
    peer_outs = []
    for s in peer_steps:
        po = PeerReviewStepOut.model_validate(s)
        po.approver_user_name = names.get(s.approver_user_id)
        po.assigned_user_name = names.get(s.assigned_user_id)
        po.reviewed_by_name = names.get(s.reviewed_by)
        peer_outs.append(po)

    out = ApprovalWorkflowDetailOut.model_validate(workflow)
    out.steps = step_outs
    out.peer_review_steps = peer_outs
    out.audit_log = audit
    return _with_invoice_fields(out, invoice, org_id)


@router.post("/workflows")
async def create_workflow(
    body: ApprovalWorkflowCreateRequest, user: User = Depends(require_action("CREATE", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    org_id = _effective_org_id(user)
    if org_id is None:
        raise _bad_request("A super-admin without an organization context cannot create an approval workflow — this endpoint is org-scoped.")
    context = engine.ApprovalMatchContext(amount=body.amount, currency=body.currency, invoice_type=body.invoice_type, conditions=body.conditions)
    try:
        workflow = await engine_svc.create_workflow(
            db, organization_id=org_id, invoice_id=body.invoice_id, context=context, created_by=user.id,
        )
    except engine_svc.ApprovalWorkflowEngineError as exc:
        # PAY13 — create_workflow() now also raises this for "invoice not
        # found in this organization" (no conflicting_rule_ids set); a real
        # RULE_CONFLICT always carries at least 2 tied rule ids, so that
        # list's presence is what actually distinguishes the two cases.
        if exc.conflicting_rule_ids:
            raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc), "conflicting_rule_ids": exc.conflicting_rule_ids})
        raise _not_found()
    if workflow is None:
        # PAY22 — create_workflow() itself no longer returns None (a
        # no-rule-matched invoice now gets a persisted AUTO_APPROVED row
        # instead, returned like any other workflow below); kept purely
        # as a defensive fallback for a future engine change, not a path
        # this route can currently reach.
        return {"created": False, "workflow": None, "reason": "No REQUIRE_APPROVAL rule matched this invoice — nothing to approve."}
    return {"created": True, "workflow": ApprovalWorkflowOut.model_validate(workflow), "reason": None}


@router.get("/my-approvals", response_model=list[MyApprovalItemOut])
async def my_approvals(
    user: User = Depends(require_action("VIEW", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    """PAY10 — batch-fetches the invoice header fields (number/vendor/
    total/currency) an approver actually needs to decide, in one extra
    query rather than N+1 per row. ApInvoice import is local to this
    function, not a module-level import — api/approvals.py otherwise has
    zero dependency on the invoice_intelligence package, and this keeps
    it that way for every route except this one."""
    from app.models.invoice_intelligence.core import ApInvoice

    items: list[MyApprovalItemOut] = []
    steps = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.assigned_user_id == user.id, ApprovalWorkflowStep.status == ApprovalStepStatus.ASSIGNED))).scalars().all()
    peer_steps = (await db.execute(select(PeerReviewStep).where(PeerReviewStep.assigned_user_id == user.id, PeerReviewStep.status == PeerReviewStatus.ASSIGNED))).scalars().all()

    workflow_ids = {s.workflow_id for s in steps} | {s.workflow_id for s in peer_steps}
    workflows = {wf.id: wf for wf in (await db.execute(select(ApprovalWorkflow).where(ApprovalWorkflow.id.in_(workflow_ids)))).scalars().all()} if workflow_ids else {}
    invoice_ids = {wf.invoice_id for wf in workflows.values()}
    invoices = {inv.id: inv for inv in (await db.execute(select(ApInvoice).where(ApInvoice.id.in_(invoice_ids)))).scalars().all()} if invoice_ids else {}

    def _invoice_fields(wf) -> dict:
        # PAY13 — same tenant-isolation check as _with_invoice_fields()
        # below: never stamp an invoice's fields without confirming it
        # actually belongs to the workflow's own organization.
        inv = invoices.get(wf.invoice_id)
        if inv is None or (inv.organization_id is not None and inv.organization_id != wf.organization_id):
            return {}
        return {"invoice_number": inv.invoice_number, "vendor_name": inv.vendor_name, "total": inv.total, "currency": inv.currency}

    for s in steps:
        wf = workflows.get(s.workflow_id)
        if wf is None:
            continue
        items.append(MyApprovalItemOut(step_id=s.id, step_kind="APPROVAL", workflow_id=wf.id, invoice_id=wf.invoice_id, rule_name=wf.rule_name, step_number=s.step_number, assigned_at=s.assigned_at, due_at=s.due_at, **_invoice_fields(wf)))
    for s in peer_steps:
        wf = workflows.get(s.workflow_id)
        if wf is None:
            continue
        items.append(MyApprovalItemOut(step_id=s.id, step_kind="PEER_REVIEW", workflow_id=wf.id, invoice_id=wf.invoice_id, rule_name=wf.rule_name, step_number=s.step_number, assigned_at=s.assigned_at, due_at=s.due_at, **_invoice_fields(wf)))
    return items


@router.post("/workflows/{workflow_id}/steps/{step_id}/approve", response_model=ApprovalWorkflowStepOut)
async def approve_step(
    workflow_id: int, step_id: int, body: ApprovalActionRequest,
    user: User = Depends(require_action("APPROVE", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    try:
        return await engine_svc.act_on_approval_step(db, organization_id=_effective_org_id(user), step_id=step_id, actor_user_id=user.id, approve=True, comment=body.comment, workflow_id=workflow_id)
    except engine_svc.ApprovalWorkflowEngineError as exc:
        raise _bad_request(str(exc))


@router.post("/workflows/{workflow_id}/steps/{step_id}/reject", response_model=ApprovalWorkflowStepOut)
async def reject_step(
    workflow_id: int, step_id: int, body: ApprovalActionRequest,
    user: User = Depends(require_action("APPROVE", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    try:
        return await engine_svc.act_on_approval_step(db, organization_id=_effective_org_id(user), step_id=step_id, actor_user_id=user.id, approve=False, comment=body.comment, workflow_id=workflow_id)
    except engine_svc.ApprovalWorkflowEngineError as exc:
        raise _bad_request(str(exc))


@router.post("/workflows/{workflow_id}/peer-review-steps/{step_id}/review", response_model=PeerReviewStepOut)
async def review_peer_step(
    workflow_id: int, step_id: int, body: ApprovalActionRequest,
    user: User = Depends(require_action("APPROVE", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    try:
        return await engine_svc.act_on_peer_review_step(db, organization_id=_effective_org_id(user), step_id=step_id, actor_user_id=user.id, flagged=False, comment=body.comment, workflow_id=workflow_id)
    except engine_svc.ApprovalWorkflowEngineError as exc:
        raise _bad_request(str(exc))


@router.post("/workflows/{workflow_id}/peer-review-steps/{step_id}/flag", response_model=PeerReviewStepOut)
async def flag_peer_step(
    workflow_id: int, step_id: int, body: ApprovalActionRequest,
    user: User = Depends(require_action("APPROVE", module=APPROVAL_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    try:
        return await engine_svc.act_on_peer_review_step(db, organization_id=_effective_org_id(user), step_id=step_id, actor_user_id=user.id, flagged=True, comment=body.comment, workflow_id=workflow_id)
    except engine_svc.ApprovalWorkflowEngineError as exc:
        raise _bad_request(str(exc))
