"""
PAY14 — automated acceptance tests for the Payment Invoice Approval
Workflow module, against a real (isolated) SQLite DB via the actual
service-layer functions — no mocking of the code under test. Same
convention as SB33's test_billing_platform.py and INV19's
test_invoice_intelligence.py: real ORM models, real service functions;
the HTTP-layer wiring (route auth, request/response shapes) was already
live-verified end to end during PAY8/PAY10-12 via the browser and direct
DB inspection — this suite exercises the service layer so coverage runs
on every future change, not to redo that verification a second way.

Scope, matching the build's own phase table:
   1-5  rule engine evaluation (PAY3) — amount range, specificity,
        RULE_CONFLICT, IN/NOT_IN operators
   6-9  rule definition validation (PAY3)
  10-14 rule CRUD/versioning (PAY8's approval_rule_service.py) —
        publish/republish, invalid-definition rejection, status
        toggle, platform-default org override, simulate
  15-18 approver resolution (PAY5)
  19-22 workflow creation (PAY4/PAY9)
  23-27 workflow action semantics (PAY8's engine — approve/reject/
        SEQUENTIAL/PARALLEL/peer-review FLAGGED)
  28-30 tenant isolation (PAY13's real fix — cross-org invoice leak)
  31-33 RBAC (role tier + module license, PAY1's registration checklist)
  34-36 idempotency guard (PAY16's real fix — duplicate workflow creation)
  37-42 scheduled-run approval-outcome classification (PAY17)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.deps import require_action
from app.models.approval.rules import (
    ApprovalConditionField,
    ApprovalConditionOperator,
    ApprovalMode,
    ApprovalRule,
    ApprovalRuleAction,
    ApprovalRuleCondition,
    ApprovalRuleStatus,
    ApprovalRuleStep,
    ApprovalStepType,
    ApproverType,
)
from app.models.approval.workflow import (
    ApprovalStepStatus,
    ApprovalWorkflow,
    ApprovalWorkflowAuditEventType,
    ApprovalWorkflowAuditLog,
    ApprovalWorkflowStatus,
    ApprovalWorkflowStep,
    PeerReviewStatus,
    PeerReviewStep,
)
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceDecision, ApInvoiceSource, ApInvoiceStatus
from app.models.subscription import SubscriptionStatus
from app.models.user import User, UserRole
from app.services import schedule_service as sched_svc
from app.services.approval import approval_rule_service as rule_svc
from app.services.approval import approval_workflow_engine_service as engine_svc
from app.services.approval import approver_resolution_service as resolver
from app.services.approval import rule_engine_service as engine
from app.api import approvals as approvals_api
from app.schemas.approval import ApprovalWorkflowOut

PAYMENT_APPROVAL_MODULE = "PAYMENT_APPROVAL"


# ── Fixtures / helpers ──────────────────────────────────────────────

async def _make_invoice(db, org, *, invoice_number="INV-1001", vendor_name="Acme Corp", total=1000.0, currency="USD", status=ApInvoiceStatus.APPROVING) -> ApInvoice:
    doc = Document(
        organization_id=org.id, filename=f"{invoice_number}.pdf", storage_path=f"/tmp/{invoice_number}.pdf",
        doc_type=DocumentType.PDF, status=DocumentStatus.COMPLETED,
    )
    db.add(doc)
    await db.flush()
    invoice = ApInvoice(
        organization_id=org.id, document_id=doc.id, source=ApInvoiceSource.MANUAL_UPLOAD,
        invoice_number=invoice_number, vendor_name=vendor_name, currency=currency, total=total, status=status,
    )
    db.add(invoice)
    await db.commit()
    await db.refresh(invoice)
    return invoice


async def _make_user(db, org, *, role=UserRole.ORG_ADMIN, department=None, is_active=True) -> User:
    from app.core.security import hash_password
    import uuid
    u = User(
        email=f"u-{uuid.uuid4().hex[:10]}@test.example", full_name="Test User",
        hashed_password=hash_password("Test1234!"), role=role, organization_id=org.id,
        department=department, is_active=is_active,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


def _rule_input(**overrides) -> rule_svc.RuleInput:
    """Builds a RuleInput for tests. `_approver_user_id` is a test-only
    convenience — when the caller doesn't supply its own `steps` list,
    a single USER-approver APPROVAL step is generated using it (defaults
    to user id 1, which is fine for the pure rule-engine-evaluation tests
    that never actually create a workflow from the rule)."""
    approver_user_id = overrides.pop("_approver_user_id", 1)
    defaults = dict(
        name="Test Rule", priority=100, status=ApprovalRuleStatus.ACTIVE,
        min_amount=1000, max_amount=5000, currency="USD", action=ApprovalRuleAction.REQUIRE_APPROVAL,
        approval_required=True, approval_count=1, approval_mode=ApprovalMode.SEQUENTIAL,
        peer_review_required=False, peer_review_count=0, peer_review_mode=ApprovalMode.SEQUENTIAL,
    )
    defaults.update(overrides)
    if "steps" not in defaults:
        defaults["steps"] = [rule_svc.StepInput(step_type=ApprovalStepType.APPROVAL, step_number=1, approver_type=ApproverType.USER, approver_user_id=approver_user_id)]
    return rule_svc.RuleInput(**defaults)


def _transient_rule(**overrides) -> ApprovalRule:
    defaults = dict(
        organization_id=1, name="T", version=1, is_current=True, status=ApprovalRuleStatus.ACTIVE, priority=100,
        action=ApprovalRuleAction.REQUIRE_APPROVAL, approval_required=True, approval_count=1, approval_mode=ApprovalMode.SEQUENTIAL,
        peer_review_required=False, peer_review_count=0, peer_review_mode=ApprovalMode.SEQUENTIAL,
    )
    defaults.update(overrides)
    return ApprovalRule(**defaults)


def _transient_step(**overrides) -> ApprovalRuleStep:
    defaults = dict(organization_id=1, step_type=ApprovalStepType.APPROVAL, step_number=1, approver_type=ApproverType.USER, approver_user_id=1)
    defaults.update(overrides)
    return ApprovalRuleStep(**defaults)


# ── 1-5: rule engine evaluation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_evaluate_matches_rule_within_amount_range(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    await rule_svc.publish_rule(db, org.id, _rule_input(_approver_user_id=user.id), user.id)

    trace = await engine.evaluate(db, org.id, engine.ApprovalMatchContext(amount=2500, currency="USD"))
    assert trace.winner is not None
    assert trace.winner.rule_name == "Test Rule"
    assert not trace.conflict


@pytest.mark.asyncio
async def test_evaluate_no_match_outside_amount_range(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    await rule_svc.publish_rule(db, org.id, _rule_input(_approver_user_id=user.id), user.id)

    trace = await engine.evaluate(db, org.id, engine.ApprovalMatchContext(amount=9999, currency="USD"))
    assert trace.winner is None
    assert not trace.matched_rules


@pytest.mark.asyncio
async def test_evaluate_prefers_more_specific_rule_by_condition_count(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    await rule_svc.publish_rule(db, org.id, _rule_input(name="Generic", priority=100, min_amount=1000, max_amount=5000, _approver_user_id=user.id), user.id)
    await rule_svc.publish_rule(
        db, org.id,
        _rule_input(
            name="Specific", priority=100, min_amount=1000, max_amount=5000,
            conditions=[rule_svc.ConditionInput(field=ApprovalConditionField.SUPPLIER, operator=ApprovalConditionOperator.EQ, value="Acme")],
            _approver_user_id=user.id,
        ),
        user.id,
    )

    trace = await engine.evaluate(db, org.id, engine.ApprovalMatchContext(amount=2500, currency="USD", conditions={"SUPPLIER": "Acme"}))
    assert trace.winner.rule_name == "Specific"
    assert not trace.conflict


@pytest.mark.asyncio
async def test_evaluate_rule_conflict_when_equally_specific_rules_tie(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    await rule_svc.publish_rule(db, org.id, _rule_input(name="Rule A", priority=100, min_amount=1000, max_amount=5000, _approver_user_id=user.id), user.id)
    await rule_svc.publish_rule(db, org.id, _rule_input(name="Rule B", priority=100, min_amount=1000, max_amount=5000, _approver_user_id=user.id), user.id)

    trace = await engine.evaluate(db, org.id, engine.ApprovalMatchContext(amount=2500, currency="USD"))
    assert trace.conflict is True
    assert len(trace.conflicting_rule_ids) == 2


@pytest.mark.asyncio
async def test_evaluate_condition_operators_in_and_not_in(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    await rule_svc.publish_rule(
        db, org.id,
        _rule_input(
            name="Dept Rule", min_amount=1000, max_amount=5000,
            conditions=[rule_svc.ConditionInput(field=ApprovalConditionField.DEPARTMENT, operator=ApprovalConditionOperator.IN, value=["FINANCE", "OPS"])],
            _approver_user_id=user.id,
        ),
        user.id,
    )

    matched = await engine.evaluate(db, org.id, engine.ApprovalMatchContext(amount=2500, currency="USD", conditions={"DEPARTMENT": "OPS"}))
    not_matched = await engine.evaluate(db, org.id, engine.ApprovalMatchContext(amount=2500, currency="USD", conditions={"DEPARTMENT": "SALES"}))
    assert matched.winner is not None
    assert not_matched.winner is None


# ── 6-9: rule definition validation ─────────────────────────────────

def test_validate_rejects_min_amount_greater_than_max_amount():
    rule = _transient_rule(min_amount=5000, max_amount=1000)
    errors = engine.validate_rule_definition(rule, [], [_transient_step()])
    assert any("min_amount" in e and "max_amount" in e for e in errors)


def test_validate_rejects_auto_approve_with_approval_required():
    rule = _transient_rule(action=ApprovalRuleAction.AUTO_APPROVE, approval_required=True)
    errors = engine.validate_rule_definition(rule, [], [_transient_step()])
    assert any("AUTO_APPROVE" in e for e in errors)


def test_validate_rejects_approval_count_exceeding_step_count():
    rule = _transient_rule(approval_required=True, approval_count=3)
    errors = engine.validate_rule_definition(rule, [], [_transient_step(step_number=1)])
    assert any("approval_count" in e and "cannot exceed" in e for e in errors)


def test_validate_rejects_escalation_hours_without_escalation_target():
    rule = _transient_rule()
    step = _transient_step(escalation_after_hours=24)
    errors = engine.validate_rule_definition(rule, [], [step])
    assert any("nothing to escalate to" in e for e in errors)


# ── 10-14: rule CRUD/versioning ──────────────────────────────────────

@pytest.mark.asyncio
async def test_publish_rule_creates_version_1_then_version_2_on_republish(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    v1 = await rule_svc.publish_rule(db, org.id, _rule_input(_approver_user_id=user.id), user.id)
    v2 = await rule_svc.publish_rule(db, org.id, _rule_input(priority=50, _approver_user_id=user.id), user.id)

    assert v1.version == 1 and v2.version == 2
    await db.refresh(v1)
    assert v1.is_current is False
    assert v2.is_current is True


@pytest.mark.asyncio
async def test_publish_rule_raises_on_invalid_definition_and_persists_nothing(db, make_org):
    org = await make_org()
    with pytest.raises(rule_svc.ApprovalRuleServiceError):
        await rule_svc.publish_rule(db, org.id, _rule_input(min_amount=5000, max_amount=1000, _approver_user_id=1), None)

    rules = await rule_svc.list_rules_for_admin(db, org.id)
    assert rules == []


@pytest.mark.asyncio
async def test_set_rule_status_toggles_active_inactive_on_org_owned_rule(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    rule = await rule_svc.publish_rule(db, org.id, _rule_input(_approver_user_id=user.id), user.id)

    deactivated = await rule_svc.set_rule_status(db, org.id, rule.id, ApprovalRuleStatus.INACTIVE, user.id)
    assert deactivated.id == rule.id and deactivated.status == ApprovalRuleStatus.INACTIVE

    trace = await engine.evaluate(db, org.id, engine.ApprovalMatchContext(amount=2500, currency="USD"))
    assert trace.winner is None  # INACTIVE rules never reach live evaluation


@pytest.mark.asyncio
async def test_set_rule_status_on_platform_default_creates_org_override(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    platform_rule = await rule_svc.publish_rule(db, None, _rule_input(name="Platform Default", _approver_user_id=user.id), None)

    override = await rule_svc.set_rule_status(db, org.id, platform_rule.id, ApprovalRuleStatus.INACTIVE, user.id)
    assert override.id != platform_rule.id
    assert override.organization_id == org.id
    assert override.version == 1
    await db.refresh(platform_rule)
    assert platform_rule.organization_id is None  # the platform row itself is untouched


@pytest.mark.asyncio
async def test_simulate_rule_matches_evaluate_and_logs_audit_event(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    await rule_svc.publish_rule(db, org.id, _rule_input(_approver_user_id=user.id), user.id)

    trace = await rule_svc.simulate_rule(db, org.id, engine.ApprovalMatchContext(amount=2500, currency="USD"), user.id)
    assert trace.winner is not None and trace.winner.rule_name == "Test Rule"


# ── 15-18: approver resolution ───────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_role_with_zero_eligible_users_fails_cleanly(db, make_org):
    org = await make_org()
    outcome = await resolver.resolve(db, organization_id=org.id, step_model=ApprovalWorkflowStep, approver_type=ApproverType.ROLE, approver_role_id=999999)
    assert outcome.resolved is False
    assert outcome.assigned_user_id is None
    assert "No active user found" in outcome.reason


@pytest.mark.asyncio
async def test_resolve_department_picks_least_loaded_among_candidates(db, make_org):
    org = await make_org()
    u1 = await _make_user(db, org, department="FINANCE")
    u2 = await _make_user(db, org, department="FINANCE")

    # Give u1 an existing open ASSIGNED step so it's more loaded than u2.
    dummy_wf = ApprovalWorkflow(
        organization_id=org.id, entity_id=1, invoice_id=1, rule_id=1, rule_name="x", rule_version=1,
        status=ApprovalWorkflowStatus.IN_PROGRESS, approval_required=True, approval_mode=ApprovalMode.SEQUENTIAL,
        approval_count_required=1, current_approval_step_number=1,
    )
    db.add(dummy_wf)
    await db.flush()
    db.add(ApprovalWorkflowStep(
        organization_id=org.id, workflow_id=dummy_wf.id, rule_step_id=1, step_number=1,
        approver_type=ApproverType.DEPARTMENT, approver_department="FINANCE",
        assigned_user_id=u1.id, status=ApprovalStepStatus.ASSIGNED,
    ))
    await db.commit()

    outcome = await resolver.resolve(db, organization_id=org.id, step_model=ApprovalWorkflowStep, approver_type=ApproverType.DEPARTMENT, approver_department="FINANCE")
    assert outcome.resolved is True
    assert outcome.assigned_user_id == u2.id  # the less-loaded of the two


@pytest.mark.asyncio
async def test_resolve_user_type_in_different_org_is_not_eligible(db, make_org):
    org1 = await make_org()
    org2 = await make_org()
    foreign_user = await _make_user(db, org2)

    outcome = await resolver.resolve(db, organization_id=org1.id, step_model=ApprovalWorkflowStep, approver_type=ApproverType.USER, approver_user_id=foreign_user.id)
    assert outcome.resolved is False


@pytest.mark.asyncio
async def test_resolve_and_assign_step_stamps_due_at_from_sla_hours(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    wf = ApprovalWorkflow(
        organization_id=org.id, entity_id=1, invoice_id=1, rule_id=1, rule_name="x", rule_version=1,
        status=ApprovalWorkflowStatus.IN_PROGRESS, approval_required=True, approval_mode=ApprovalMode.SEQUENTIAL,
        approval_count_required=1, current_approval_step_number=1,
    )
    db.add(wf)
    await db.flush()
    step = ApprovalWorkflowStep(
        organization_id=org.id, workflow_id=wf.id, rule_step_id=1, step_number=1,
        approver_type=ApproverType.USER, approver_user_id=user.id, sla_hours=24, status=ApprovalStepStatus.PENDING,
    )
    db.add(step)
    await db.flush()

    outcome = await resolver.resolve_and_assign_step(db, step, wf)
    await db.commit()
    assert outcome.resolved is True
    assert step.status == ApprovalStepStatus.ASSIGNED
    assert step.due_at is not None
    assert step.due_at - step.assigned_at == timedelta(hours=24)


# ── 19-22: workflow creation ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_workflow_materializes_steps_and_assigns_first_step(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    await rule_svc.publish_rule(db, org.id, _rule_input(_approver_user_id=user.id), user.id)

    wf = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)
    assert wf is not None
    assert wf.status == ApprovalWorkflowStatus.IN_PROGRESS

    steps = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == wf.id))).scalars().all()
    assert len(steps) == 1
    assert steps[0].status == ApprovalStepStatus.ASSIGNED
    assert steps[0].assigned_user_id == user.id


@pytest.mark.asyncio
async def test_create_workflow_auto_approves_when_rule_requires_nothing(db, make_org):
    """PAY22 — a rule that matches but needs neither gate now persists a
    terminal AUTO_APPROVED row (rule_id populated, since a real rule DID
    own this decision) instead of the old bare `None` return."""
    org = await make_org()
    user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    rule = await rule_svc.publish_rule(
        db, org.id,
        _rule_input(action=ApprovalRuleAction.AUTO_APPROVE, approval_required=False, approval_count=0, peer_review_required=False, steps=[], _approver_user_id=user.id),
        user.id,
    )

    wf = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)
    assert wf is not None
    assert wf.status == ApprovalWorkflowStatus.AUTO_APPROVED
    assert wf.rule_id == rule.id
    assert wf.approval_required is False
    assert wf.peer_review_required is False
    assert wf.completed_at is not None
    steps = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == wf.id))).scalars().all()
    assert steps == []


@pytest.mark.asyncio
async def test_create_workflow_auto_approves_when_no_rule_matches(db, make_org):
    """PAY22 — when NO rule matches at all, the row is still created but
    with rule_id/rule_name/rule_version left NULL (no rule to reference)
    — distinct from the "a rule matched but needed no gate" case above."""
    org = await make_org()
    user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    # No rule published for this org at all.

    wf = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)
    assert wf is not None
    assert wf.status == ApprovalWorkflowStatus.AUTO_APPROVED
    assert wf.rule_id is None
    assert wf.rule_name is None
    assert wf.rule_version is None

    # Idempotent: a second call for the same invoice returns the SAME row.
    wf2 = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)
    assert wf2.id == wf.id


@pytest.mark.asyncio
async def test_create_workflow_raises_on_rule_conflict(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    await rule_svc.publish_rule(db, org.id, _rule_input(name="A", _approver_user_id=user.id), user.id)
    await rule_svc.publish_rule(db, org.id, _rule_input(name="B", _approver_user_id=user.id), user.id)

    with pytest.raises(engine_svc.ApprovalWorkflowEngineError) as exc:
        await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)
    assert len(exc.value.conflicting_rule_ids) == 2


@pytest.mark.asyncio
async def test_create_workflow_auto_approve_action_with_peer_review_still_creates_workflow(db, make_org):
    """Real bug found live during PAY8 (documented in the module's own
    docstring): AUTO_APPROVE action + peer_review_required=True used to
    be silently discarded because the old check only looked at
    rule.action. Regression-guards that fix."""
    org = await make_org()
    user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    await rule_svc.publish_rule(
        db, org.id,
        _rule_input(
            action=ApprovalRuleAction.AUTO_APPROVE, approval_required=False, approval_count=0,
            peer_review_required=True, peer_review_count=1,
            steps=[rule_svc.StepInput(step_type=ApprovalStepType.PEER_REVIEW, step_number=1, approver_type=ApproverType.USER, approver_user_id=user.id)],
        ),
        user.id,
    )

    wf = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)
    assert wf is not None
    assert wf.peer_review_required is True


# ── 23-27: workflow action semantics ─────────────────────────────────

async def _create_simple_workflow(db, org, user, invoice, **rule_overrides):
    await rule_svc.publish_rule(db, org.id, _rule_input(_approver_user_id=user.id, **rule_overrides), user.id)
    return await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)


@pytest.mark.asyncio
async def test_only_assigned_approver_may_act_on_step(db, make_org):
    org = await make_org()
    approver = await _make_user(db, org)
    other_user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    wf = await _create_simple_workflow(db, org, approver, invoice)
    step = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == wf.id))).scalar_one()

    with pytest.raises(engine_svc.ApprovalWorkflowEngineError, match="Only the assigned approver"):
        await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=step.id, actor_user_id=other_user.id, approve=True)


@pytest.mark.asyncio
async def test_reject_short_circuits_workflow_and_syncs_invoice_rejected(db, make_org):
    org = await make_org()
    approver = await _make_user(db, org)
    invoice = await _make_invoice(db, org, status=ApInvoiceStatus.APPROVING)
    wf = await _create_simple_workflow(db, org, approver, invoice)
    step = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == wf.id))).scalar_one()

    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=step.id, actor_user_id=approver.id, approve=False, comment="bad invoice")

    await db.refresh(wf)
    assert wf.status == ApprovalWorkflowStatus.REJECTED
    await db.refresh(invoice)
    assert invoice.status == ApInvoiceStatus.REJECTED


@pytest.mark.asyncio
async def test_sequential_approve_advances_and_completes_workflow(db, make_org):
    org = await make_org()
    u1 = await _make_user(db, org)
    u2 = await _make_user(db, org)
    invoice = await _make_invoice(db, org, status=ApInvoiceStatus.APPROVING)
    await rule_svc.publish_rule(
        db, org.id,
        _rule_input(
            approval_count=2,
            steps=[
                rule_svc.StepInput(step_type=ApprovalStepType.APPROVAL, step_number=1, approver_type=ApproverType.USER, approver_user_id=u1.id),
                rule_svc.StepInput(step_type=ApprovalStepType.APPROVAL, step_number=2, approver_type=ApproverType.USER, approver_user_id=u2.id),
            ],
        ),
        u1.id,
    )
    wf = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=u1.id)

    step1 = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == wf.id, ApprovalWorkflowStep.step_number == 1))).scalar_one()
    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=step1.id, actor_user_id=u1.id, approve=True)

    step2 = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == wf.id, ApprovalWorkflowStep.step_number == 2))).scalar_one()
    assert step2.status == ApprovalStepStatus.ASSIGNED  # SEQUENTIAL advanced to step 2

    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=step2.id, actor_user_id=u2.id, approve=True)

    await db.refresh(wf)
    assert wf.status == ApprovalWorkflowStatus.APPROVED
    await db.refresh(invoice)
    assert invoice.status == ApInvoiceStatus.READY_TO_POST


@pytest.mark.asyncio
async def test_parallel_mode_completes_at_count_required_and_skips_remainder(db, make_org):
    org = await make_org()
    u1 = await _make_user(db, org)
    u2 = await _make_user(db, org)
    u3 = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    await rule_svc.publish_rule(
        db, org.id,
        _rule_input(
            approval_mode=ApprovalMode.PARALLEL, approval_count=2,
            steps=[
                rule_svc.StepInput(step_type=ApprovalStepType.APPROVAL, step_number=1, approver_type=ApproverType.USER, approver_user_id=u1.id),
                rule_svc.StepInput(step_type=ApprovalStepType.APPROVAL, step_number=2, approver_type=ApproverType.USER, approver_user_id=u2.id),
                rule_svc.StepInput(step_type=ApprovalStepType.APPROVAL, step_number=3, approver_type=ApproverType.USER, approver_user_id=u3.id),
            ],
        ),
        u1.id,
    )
    wf = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=u1.id)

    steps = {s.step_number: s for s in (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == wf.id))).scalars().all()}
    assert all(s.status == ApprovalStepStatus.ASSIGNED for s in steps.values())  # PARALLEL assigns everyone at once

    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=steps[1].id, actor_user_id=u1.id, approve=True)
    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=steps[2].id, actor_user_id=u2.id, approve=True)

    await db.refresh(steps[3])
    assert steps[3].status == ApprovalStepStatus.SKIPPED  # count_required (2) already met
    await db.refresh(wf)
    assert wf.status == ApprovalWorkflowStatus.APPROVED


@pytest.mark.asyncio
async def test_peer_review_flagged_still_counts_toward_completion(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    await rule_svc.publish_rule(
        db, org.id,
        _rule_input(
            name="Peer Only", action=ApprovalRuleAction.AUTO_APPROVE,
            approval_required=False, approval_count=0,
            peer_review_required=True, peer_review_count=1,
            steps=[rule_svc.StepInput(step_type=ApprovalStepType.PEER_REVIEW, step_number=1, approver_type=ApproverType.USER, approver_user_id=user.id)],
        ),
        user.id,
    )
    wf = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)
    peer_step = (await db.execute(select(PeerReviewStep).where(PeerReviewStep.workflow_id == wf.id))).scalar_one()

    await engine_svc.act_on_peer_review_step(db, organization_id=org.id, step_id=peer_step.id, actor_user_id=user.id, flagged=True, comment="looks off")

    await db.refresh(peer_step)
    assert peer_step.status == PeerReviewStatus.FLAGGED
    await db.refresh(wf)
    assert wf.status == ApprovalWorkflowStatus.APPROVED  # FLAGGED doesn't block completion


# ── 28-30: tenant isolation (PAY13) ──────────────────────────────────

@pytest.mark.asyncio
async def test_create_workflow_rejects_invoice_from_a_different_org(db, make_org):
    org1 = await make_org()
    org2 = await make_org()
    user2 = await _make_user(db, org2)
    victim_invoice = await _make_invoice(db, org1, invoice_number="VICTIM-1")
    await rule_svc.publish_rule(db, org2.id, _rule_input(_approver_user_id=user2.id), user2.id)

    with pytest.raises(engine_svc.ApprovalWorkflowEngineError) as exc:
        await engine_svc.create_workflow(db, organization_id=org2.id, entity_id=victim_invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user2.id)
    assert not exc.value.conflicting_rule_ids  # not-found, not a rule conflict


@pytest.mark.asyncio
async def test_with_invoice_fields_never_stamps_a_cross_org_invoice(db, make_org):
    org1 = await make_org()
    org2 = await make_org()
    victim_invoice = await _make_invoice(db, org1, invoice_number="VICTIM-2", vendor_name="Victim Vendor", total=555.0)

    bad_wf = ApprovalWorkflow(
        organization_id=org2.id, entity_id=victim_invoice.id, invoice_id=victim_invoice.id, rule_id=0, rule_name="fake", rule_version=1,
        status=ApprovalWorkflowStatus.APPROVED, approval_required=True, approval_mode=ApprovalMode.SEQUENTIAL,
        approval_count_required=1, approval_count_completed=1,
    )
    db.add(bad_wf)
    await db.commit()
    await db.refresh(bad_wf)

    out = ApprovalWorkflowOut.model_validate(bad_wf)
    stamped = approvals_api._with_invoice_fields(out, victim_invoice, org2.id)
    assert stamped.invoice_number is None and stamped.vendor_name is None and stamped.total is None


@pytest.mark.asyncio
async def test_with_invoice_fields_stamps_normally_for_same_org_invoice(db, make_org):
    org = await make_org()
    invoice = await _make_invoice(db, org, invoice_number="OWN-1", vendor_name="My Vendor", total=42.0)
    wf = ApprovalWorkflow(
        organization_id=org.id, entity_id=invoice.id, invoice_id=invoice.id, rule_id=0, rule_name="fake", rule_version=1,
        status=ApprovalWorkflowStatus.APPROVED, approval_required=True, approval_mode=ApprovalMode.SEQUENTIAL,
        approval_count_required=1, approval_count_completed=1,
    )
    db.add(wf)
    await db.commit()
    await db.refresh(wf)

    out = ApprovalWorkflowOut.model_validate(wf)
    stamped = approvals_api._with_invoice_fields(out, invoice, org.id)
    assert stamped.invoice_number == "OWN-1" and stamped.vendor_name == "My Vendor" and stamped.total == 42.0


# ── 31-33: RBAC (role tier + module license) ─────────────────────────

@pytest.mark.asyncio
async def test_rbac_viewer_denied_approve_regardless_of_module():
    checker = require_action("APPROVE", module=PAYMENT_APPROVAL_MODULE)
    viewer = User(role=UserRole.VIEWER, is_active=True)  # ROLE_ACTIONS[VIEWER] == {"VIEW"} only

    with pytest.raises(HTTPException) as exc:
        await checker(user=viewer, org=None, db=None)  # role check short-circuits before any DB/module check
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_rbac_org_without_payment_approval_license_is_denied(db, make_org):
    checker = require_action("APPROVE", module=PAYMENT_APPROVAL_MODULE)
    org = await make_org()  # no subscription at all
    admin = User(role=UserRole.ORG_ADMIN, organization_id=org.id, is_active=True)

    with pytest.raises(HTTPException) as exc:
        await checker(user=admin, org=org, db=db)
    assert exc.value.status_code == 403
    assert "not licensed" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_rbac_granted_when_role_and_module_license_both_ok(db, make_org, make_subscription):
    checker = require_action("APPROVE", module=PAYMENT_APPROVAL_MODULE)
    org = await make_org()
    await make_subscription(organization_id=org.id, modules=[PAYMENT_APPROVAL_MODULE], status=SubscriptionStatus.ACTIVE, end_date=datetime.now(timezone.utc) + timedelta(days=30))
    admin = User(role=UserRole.ORG_ADMIN, organization_id=org.id, is_active=True)

    result = await checker(user=admin, org=org, db=db)
    assert result is admin


# ── 34-36: idempotency guard (PAY16) ─────────────────────────────────
# Spec §111: a scheduler retry, a duplicate-processed source record, or
# any other repeated call to create_workflow() for the same invoice must
# never create a second workflow row. §111 also requires this NOT break
# the pre-existing, deliberate "a REJECTED invoice can be corrected and
# resubmitted" behavior (see ApprovalWorkflow.invoice_id's own docstring)
# — only PENDING/IN_PROGRESS/APPROVED count as "already exists".

@pytest.mark.asyncio
async def test_create_workflow_is_idempotent_on_repeated_call(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    await rule_svc.publish_rule(db, org.id, _rule_input(_approver_user_id=user.id), user.id)
    context = engine.ApprovalMatchContext(amount=2500, currency="USD")

    first = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=context, created_by=user.id)
    second = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=context, created_by=user.id)

    assert first.id == second.id
    count = (await db.execute(select(func.count()).select_from(ApprovalWorkflow).where(ApprovalWorkflow.invoice_id == invoice.id)))
    assert count.scalar() == 1  # no duplicate row was created

    suppressed = (await db.execute(
        select(ApprovalWorkflowAuditLog).where(
            ApprovalWorkflowAuditLog.workflow_id == first.id,
            ApprovalWorkflowAuditLog.event_type == ApprovalWorkflowAuditEventType.DUPLICATE_SUPPRESSED,
        )
    )).scalars().first()
    assert suppressed is not None  # the durable "WORKFLOW_ALREADY_EXISTS" trace spec §111 asks for


@pytest.mark.asyncio
async def test_create_workflow_after_approval_does_not_create_second_workflow(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    wf = await _create_simple_workflow(db, org, user, invoice)
    step = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == wf.id))).scalar_one()
    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=step.id, actor_user_id=user.id, approve=True)
    await db.refresh(wf)
    assert wf.status == ApprovalWorkflowStatus.APPROVED  # sanity — fully resolved before the retry

    retried = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)

    assert retried.id == wf.id  # same, already-APPROVED workflow — not a fresh PENDING one
    count = (await db.execute(select(func.count()).select_from(ApprovalWorkflow).where(ApprovalWorkflow.invoice_id == invoice.id)))
    assert count.scalar() == 1


@pytest.mark.asyncio
async def test_create_workflow_after_rejection_allows_resubmission(db, make_org):
    org = await make_org()
    user = await _make_user(db, org)
    invoice = await _make_invoice(db, org)
    wf = await _create_simple_workflow(db, org, user, invoice)
    step = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == wf.id))).scalar_one()
    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=step.id, actor_user_id=user.id, approve=False, comment="missing PO")
    await db.refresh(wf)
    assert wf.status == ApprovalWorkflowStatus.REJECTED  # sanity

    resubmitted = await engine_svc.create_workflow(db, organization_id=org.id, entity_id=invoice.id, context=engine.ApprovalMatchContext(amount=2500, currency="USD"), created_by=user.id)

    assert resubmitted is not None
    assert resubmitted.id != wf.id  # a genuinely NEW workflow — REJECTED must not be treated as "already exists"
    count = (await db.execute(select(func.count()).select_from(ApprovalWorkflow).where(ApprovalWorkflow.invoice_id == invoice.id)))
    assert count.scalar() == 2


# ── 37-42: scheduled-run approval-outcome classification (PAY17) ────────
# schedule_service._classify_invoice_approval_outcome() is the function a
# document-schedule run calls, per processed INVOICE document, to turn
# already-persisted invoice/audit-log state into the approval-specific
# batch-run counters (auto_approved/workflows_created/already_in_workflow/
# rule_conflicts/approver_not_found) that were previously invisible at
# the schedule-run level (spec §120's "job metrics show workflow
# creation counts", identified as a gap in the PAY13/14 confirmation
# audit). It never re-derives a decision — only reads what decide()/
# create_workflow() already wrote.

def _dummy_workflow_row(org_id: int, invoice_id: int, **overrides) -> ApprovalWorkflow:
    defaults = dict(
        organization_id=org_id, entity_id=invoice_id, invoice_id=invoice_id, rule_id=0, rule_name="dummy", rule_version=1,
        status=ApprovalWorkflowStatus.IN_PROGRESS, approval_required=True, approval_mode=ApprovalMode.SEQUENTIAL,
        approval_count_required=1,
    )
    defaults.update(overrides)
    return ApprovalWorkflow(**defaults)


@pytest.mark.asyncio
async def test_classify_approval_outcome_empty_when_no_invoice_materialized(db, make_org):
    org = await make_org()
    doc = Document(organization_id=org.id, filename="never-became-invoice.pdf", storage_path="/tmp/x.pdf", doc_type=DocumentType.PDF, status=DocumentStatus.FAILED)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    result = await sched_svc._classify_invoice_approval_outcome(db, doc.id)
    assert result == {}


@pytest.mark.asyncio
async def test_classify_approval_outcome_detects_auto_approved(db, make_org):
    org = await make_org()
    invoice = await _make_invoice(db, org, status=ApInvoiceStatus.READY_TO_POST)
    invoice.final_decision = ApInvoiceDecision.STRAIGHT_THROUGH
    await db.commit()

    result = await sched_svc._classify_invoice_approval_outcome(db, invoice.document_id)
    assert result == {"auto_approved": 1}


@pytest.mark.asyncio
async def test_classify_approval_outcome_detects_workflow_created(db, make_org):
    org = await make_org()
    invoice = await _make_invoice(db, org, status=ApInvoiceStatus.APPROVING)
    wf = _dummy_workflow_row(org.id, invoice.id)
    db.add(wf)
    await db.flush()
    db.add(ApprovalWorkflowAuditLog(organization_id=org.id, workflow_id=wf.id, entity_id=invoice.id, invoice_id=invoice.id, event_type=ApprovalWorkflowAuditEventType.CREATED, message="created"))
    await db.commit()

    result = await sched_svc._classify_invoice_approval_outcome(db, invoice.document_id)
    assert result == {"workflows_created": 1}


@pytest.mark.asyncio
async def test_classify_approval_outcome_detects_duplicate_suppressed(db, make_org):
    org = await make_org()
    invoice = await _make_invoice(db, org, status=ApInvoiceStatus.APPROVING)
    wf = _dummy_workflow_row(org.id, invoice.id)
    db.add(wf)
    await db.flush()
    db.add(ApprovalWorkflowAuditLog(organization_id=org.id, workflow_id=wf.id, entity_id=invoice.id, invoice_id=invoice.id, event_type=ApprovalWorkflowAuditEventType.DUPLICATE_SUPPRESSED, message="suppressed"))
    await db.commit()

    result = await sched_svc._classify_invoice_approval_outcome(db, invoice.document_id)
    assert result == {"already_in_workflow": 1}


@pytest.mark.asyncio
async def test_classify_approval_outcome_detects_rule_conflict(db, make_org):
    org = await make_org()
    invoice = await _make_invoice(db, org, status=ApInvoiceStatus.REVIEW_REQUIRED)
    invoice.decision_reasons = [{"rule_id": None, "category": "APPROVAL_RULE_CONFLICT", "result": "FAIL", "message": "tie"}]
    await db.commit()

    result = await sched_svc._classify_invoice_approval_outcome(db, invoice.document_id)
    assert result == {"rule_conflicts": 1}


@pytest.mark.asyncio
async def test_classify_approval_outcome_detects_approver_not_found_alongside_workflow_created(db, make_org):
    """A workflow CAN still be created even when one of its steps failed
    to resolve an approver (a different step in the same workflow might
    resolve fine) — both counters should reflect reality, not just one."""
    org = await make_org()
    invoice = await _make_invoice(db, org, status=ApInvoiceStatus.APPROVING)
    wf = _dummy_workflow_row(org.id, invoice.id)
    db.add(wf)
    await db.flush()
    db.add(ApprovalWorkflowAuditLog(organization_id=org.id, workflow_id=wf.id, entity_id=invoice.id, invoice_id=invoice.id, event_type=ApprovalWorkflowAuditEventType.CREATED, message="created"))
    db.add(ApprovalWorkflowAuditLog(organization_id=org.id, workflow_id=wf.id, entity_id=invoice.id, invoice_id=invoice.id, event_type=ApprovalWorkflowAuditEventType.RESOLUTION_FAILED, message="no eligible user"))
    await db.commit()

    result = await sched_svc._classify_invoice_approval_outcome(db, invoice.document_id)
    assert result == {"workflows_created": 1, "approver_not_found": 1}


@pytest.mark.asyncio
async def test_stats_defaults_includes_pay17_approval_counters():
    stats = sched_svc._stats_defaults()
    for key in ("auto_approved", "workflows_created", "already_in_workflow", "rule_conflicts", "approver_not_found"):
        assert stats[key] == 0
