"""
TALLY-H — automated tests for the write-back approval gate: the
entity_type+entity_id generalization of the Payment Approval engine
(ApprovalEntityType.TALLY_VOUCHER) and tally_approval_service.py's
submit_for_approval(). Same "real ORM models, real service functions,
no mocking of the code under test" convention as test_payment_approvals.py.

Scope:
  1-2 rule scoping — a TALLY_VOUCHER context never matches an
      INVOICE-scoped rule and vice versa (the correctness bug TALLY-H's
      own module docstring calls out as the reason entity_type filtering
      is load-bearing, not cosmetic).
  3-5 submit_for_approval() — creates a TALLY_VOUCHER workflow, rejects
      a BLOCKED preview, suppresses a duplicate PENDING submission.
  6   AUTO_APPROVED path — no TALLY_VOUCHER-scoped rule configured ->
      preview.approval_status flips straight to APPROVED.
  7-8 a real human decision (approve/reject via the unmodified,
      entity-agnostic act_on_approval_step()) syncs
      preview.approval_status correctly.
"""
from __future__ import annotations

import pytest

from app.models.approval.rules import ApprovalEntityType, ApprovalMode, ApprovalRuleAction, ApprovalRuleStatus, ApprovalStepType, ApproverType
from app.models.approval.workflow import ApprovalStepStatus, ApprovalWorkflow, ApprovalWorkflowStatus, ApprovalWorkflowStep
from app.models.connection import Connection, ConnectionCategory, ConnectionType
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceSource, ApInvoiceStatus
from app.models.tally_mapping import TallyVoucherApprovalStatus, TallyVoucherPreview, TallyVoucherPreviewStatus
from app.services import tally_approval_service as approval_svc
from app.services.approval import approval_rule_service as rule_svc
from app.services.approval import approval_workflow_engine_service as engine_svc
from app.services.approval import rule_engine_service as engine


# ── Fixtures / helpers ──────────────────────────────────────────────

async def _make_connection(db, org):
    conn = Connection(
        organization_id=org.id, name="Verify Tally H", category=ConnectionCategory.APPLICATION,
        connection_type=ConnectionType.TALLY_PRIME, config={"host": "127.0.0.1", "port": 9999},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


async def _make_invoice(db, org, *, total=1180.0, currency="INR") -> ApInvoice:
    doc = Document(
        organization_id=org.id, filename="tally-h.pdf", storage_path="/tmp/tally-h.pdf",
        doc_type=DocumentType.PDF, status=DocumentStatus.COMPLETED,
    )
    db.add(doc)
    await db.flush()
    invoice = ApInvoice(
        organization_id=org.id, document_id=doc.id, source=ApInvoiceSource.MANUAL_UPLOAD,
        invoice_number="INV-TALLY-H-1", vendor_name="Beta Supplies", currency=currency, total=total,
        status=ApInvoiceStatus.APPROVING,
    )
    db.add(invoice)
    await db.commit()
    await db.refresh(invoice)
    return invoice


async def _make_preview(db, org, conn, invoice, *, status=TallyVoucherPreviewStatus.READY) -> TallyVoucherPreview:
    preview = TallyVoucherPreview(
        organization_id=org.id, connection_id=conn.id, invoice_id=invoice.id, company_name="Verify Tally B",
        voucher_type="Purchase", party_ledger_name="Beta Supplies", ledger_entries=[], status=status,
    )
    db.add(preview)
    await db.commit()
    await db.refresh(preview)
    return preview


async def _make_user(db, org):
    from app.core.security import hash_password
    import uuid
    from app.models.user import User, UserRole
    u = User(
        email=f"u-{uuid.uuid4().hex[:10]}@test.example", full_name="Test Approver",
        hashed_password=hash_password("Test1234!"), role=UserRole.ORG_ADMIN, organization_id=org.id, is_active=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _publish_tally_rule(db, org, approver_user_id: int, **overrides):
    steps = [rule_svc.StepInput(step_type=ApprovalStepType.APPROVAL, step_number=1, approver_type=ApproverType.USER, approver_user_id=approver_user_id)]
    payload = rule_svc.RuleInput(
        name="Tally Voucher Rule", entity_type=ApprovalEntityType.TALLY_VOUCHER, priority=100,
        status=ApprovalRuleStatus.ACTIVE, action=ApprovalRuleAction.REQUIRE_APPROVAL,
        approval_required=True, approval_count=1, approval_mode=ApprovalMode.SEQUENTIAL, steps=steps,
    )
    for k, v in overrides.items():
        setattr(payload, k, v)
    return await rule_svc.publish_rule(db, org.id, payload, None)


# ── 1-2. Rule scoping ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unconditional_invoice_rule_never_matches_a_tally_voucher_context(db, make_org):
    """The exact correctness bug TALLY-H's own docstring warns about:
    an org's pre-existing, unconditional invoice rule must never
    silently also claim a Tally voucher."""
    org = await make_org()
    payload = rule_svc.RuleInput(name="Catch-All Invoice Rule", entity_type=ApprovalEntityType.INVOICE, approval_count=1, steps=[
        rule_svc.StepInput(step_type=ApprovalStepType.APPROVAL, step_number=1, approver_type=ApproverType.USER, approver_user_id=1),
    ])
    await rule_svc.publish_rule(db, org.id, payload, None)

    tally_context = engine.ApprovalMatchContext(amount=1180.0, currency="INR")
    trace = await engine.evaluate(db, org.id, tally_context, ApprovalEntityType.TALLY_VOUCHER)
    assert trace.winner is None
    assert trace.candidates_considered == 0


@pytest.mark.asyncio
async def test_tally_voucher_rule_never_matches_an_invoice_context(db, make_org):
    org = await make_org()
    await _publish_tally_rule(db, org, approver_user_id=1)

    invoice_context = engine.ApprovalMatchContext(amount=1180.0, currency="INR")
    trace = await engine.evaluate(db, org.id, invoice_context, ApprovalEntityType.INVOICE)
    assert trace.winner is None
    assert trace.candidates_considered == 0


# ── 3-5. submit_for_approval() ───────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_for_approval_creates_a_tally_voucher_workflow(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    approver = await _make_user(db, org)
    await _publish_tally_rule(db, org, approver_user_id=approver.id)

    preview, workflow = await approval_svc.submit_for_approval(db, preview_id=preview.id, organization_id=org.id, actor_user_id=None)
    assert workflow.entity_type == ApprovalEntityType.TALLY_VOUCHER
    assert workflow.entity_id == preview.id
    assert workflow.invoice_id is None  # TALLY-H's own invariant: NULL for non-INVOICE rows
    assert workflow.status == ApprovalWorkflowStatus.IN_PROGRESS
    assert preview.approval_status == TallyVoucherApprovalStatus.PENDING
    assert preview.approval_workflow_id == workflow.id


@pytest.mark.asyncio
async def test_submit_for_approval_rejects_a_blocked_preview(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice, status=TallyVoucherPreviewStatus.BLOCKED)

    with pytest.raises(approval_svc.TallyApprovalError, match="READY"):
        await approval_svc.submit_for_approval(db, preview_id=preview.id, organization_id=org.id, actor_user_id=None)


@pytest.mark.asyncio
async def test_submit_for_approval_suppresses_duplicate_while_pending(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    approver = await _make_user(db, org)
    await _publish_tally_rule(db, org, approver_user_id=approver.id)

    _, first = await approval_svc.submit_for_approval(db, preview_id=preview.id, organization_id=org.id, actor_user_id=None)
    _, second = await approval_svc.submit_for_approval(db, preview_id=preview.id, organization_id=org.id, actor_user_id=None)
    assert first.id == second.id  # same workflow returned, not a new one


# ── 6. AUTO_APPROVED (no rule configured) ─────────────────────────────

@pytest.mark.asyncio
async def test_submit_for_approval_auto_approves_when_no_tally_rule_configured(db, make_org):
    """Disclosed, not silent, configuration gap — see tally_approval_
    service.py's own module docstring: identical in spirit to how an
    unconfigured org's invoices are already AUTO_APPROVED."""
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)

    preview, workflow = await approval_svc.submit_for_approval(db, preview_id=preview.id, organization_id=org.id, actor_user_id=None)
    assert workflow.status == ApprovalWorkflowStatus.AUTO_APPROVED
    assert preview.approval_status == TallyVoucherApprovalStatus.APPROVED


# ── 7-8. Real human decision syncs preview.approval_status ────────────

@pytest.mark.asyncio
async def test_approving_the_step_flips_preview_to_approved(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    approver = await _make_user(db, org)
    await _publish_tally_rule(db, org, approver_user_id=approver.id)

    preview, workflow = await approval_svc.submit_for_approval(db, preview_id=preview.id, organization_id=org.id, actor_user_id=None)
    from sqlalchemy import select
    step = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == workflow.id))).scalars().first()
    assert step.status == ApprovalStepStatus.ASSIGNED
    assert step.assigned_user_id == approver.id

    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=step.id, actor_user_id=approver.id, approve=True)

    await db.refresh(preview)
    assert preview.approval_status == TallyVoucherApprovalStatus.APPROVED


@pytest.mark.asyncio
async def test_rejecting_the_step_flips_preview_to_rejected(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    approver = await _make_user(db, org)
    await _publish_tally_rule(db, org, approver_user_id=approver.id)

    preview, workflow = await approval_svc.submit_for_approval(db, preview_id=preview.id, organization_id=org.id, actor_user_id=None)
    from sqlalchemy import select
    step = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == workflow.id))).scalars().first()

    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=step.id, actor_user_id=approver.id, approve=False, comment="not this quarter")

    await db.refresh(preview)
    assert preview.approval_status == TallyVoucherApprovalStatus.REJECTED


@pytest.mark.asyncio
async def test_resubmitting_an_already_approved_preview_does_not_regress_to_pending(db, make_org):
    """Real bug found live (reasoned through before ever exercising it,
    fixed before this test was written): create_workflow()'s duplicate-
    suppression treats APPROVED the same as PENDING/IN_PROGRESS/
    AUTO_APPROVED — resubmitting an already-approved preview returns
    that same pre-existing APPROVED workflow rather than creating a new
    one. submit_for_approval() unconditionally sets approval_status=
    PENDING before calling create_workflow(); without dispatching on the
    RETURNED workflow's actual status afterward, an already-approved
    preview would incorrectly regress to PENDING on a stray resubmission
    (e.g. a double-clicked Submit button), even though nothing about its
    real approval state changed."""
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)
    preview = await _make_preview(db, org, conn, invoice)
    approver = await _make_user(db, org)
    await _publish_tally_rule(db, org, approver_user_id=approver.id)

    from sqlalchemy import select
    preview, workflow = await approval_svc.submit_for_approval(db, preview_id=preview.id, organization_id=org.id, actor_user_id=None)
    step = (await db.execute(select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == workflow.id))).scalars().first()
    await engine_svc.act_on_approval_step(db, organization_id=org.id, step_id=step.id, actor_user_id=approver.id, approve=True)
    await db.refresh(preview)
    assert preview.approval_status == TallyVoucherApprovalStatus.APPROVED

    # A stray resubmission (double-clicked button, retried request) must
    # not regress the preview back to PENDING.
    preview, workflow2 = await approval_svc.submit_for_approval(db, preview_id=preview.id, organization_id=org.id, actor_user_id=None)
    assert workflow2.id == workflow.id
    assert preview.approval_status == TallyVoucherApprovalStatus.APPROVED
