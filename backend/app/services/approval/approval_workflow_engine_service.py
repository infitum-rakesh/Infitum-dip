"""
approval_workflow_engine_service.py — Approval Rule Engine Phase 8: the
workflow ENGINE every prior phase's docstring deferred ("advancing
current_*_step_number... belongs to the workflow ENGINE, a later
phase"). This is that phase: creating a real ApprovalWorkflow from a
Phase 3 evaluation, and the actual "a human clicks Approve/Reject/Review/
Flag" state transitions.

Creation (create_workflow): runs rule_engine_service.evaluate(); a
RULE_CONFLICT (two equally-specific rules tied) raises rather than
picking one arbitrarily — same principle Phase 3 itself never violated;
no matching rule, or a matching rule that requires neither approval nor
peer review, both correctly return None (nothing to create — the
invoice needs no human attention), never a fabricated empty workflow.
Deliberately NOT gated on the rule's `action` field alone — an
AUTO_APPROVE rule can still set peer_review_required=True (a valid,
disclosed "auto-approve the amount gate, but still route it to a peer
reviewer" configuration Phase 3 never rejects), so the real check is
whether EITHER approval_required or peer_review_required is set. On a
match that needs a workflow, materializes ApprovalRuleStep rows into
ApprovalWorkflowStep/PeerReviewStep instances,
snapshotting sla_hours/reminder_after_hours/escalation_* per step (or the
rule's own default_sla_hours when a step didn't set its own) — same
reproducibility guarantee as everywhere else in this feature: editing the
rule later never rewrites what THIS workflow's steps mean.

Automatically triggering create_workflow() when a real invoice enters
processing was deferred here to "a later phase's job" — that phase is
PAY9: invoice_decision_service.decide() now calls create_workflow()
itself for every STRAIGHT_THROUGH invoice (gated on the org actually
holding PAYMENT_APPROVAL), and this module's own _maybe_complete_
workflow()/act_on_approval_step() now sync the result back onto the
ApInvoice row (_sync_invoice_status()) so invoice_erp_service.
post_invoice() can block posting until a real human sign-off lands.
api/approvals.py's POST /workflows route remains available too — for
admin/test-triggered creation outside the automatic pipeline path.

Action semantics (act_on_approval_step / act_on_peer_review_step):
  - Only the step's own assigned_user_id may act on it — Phase 5 already
    resolved ROLE/DEPARTMENT/POOL down to exactly one concrete assignee,
    so that IS the actionable person, no broader "anyone with this role"
    check needed.
  - REJECTED is an immediate, unconditional short-circuit: the whole
    workflow terminates REJECTED right there — a single reviewer's
    rejection is a real business stop, not something a later approver
    can silently override in this build.
  - APPROVED/REVIEWED/FLAGGED all advance their own phase independently:
    SEQUENTIAL mode moves current_*_step_number to the next step in the
    chain (or clears it when the chain is exhausted) and immediately
    resolves+assigns that next step; PARALLEL mode counts toward
    *_count_required and, once met, marks any still-open steps of that
    phase SKIPPED (an M-of-N rule with more than M approvers configured
    shouldn't leave the extra N-M requests sitting there forever once
    the threshold is already satisfied).
  - Peer review's FLAGGED outcome does NOT block workflow completion —
    consistent with Phase 4's own docstring that peer review is "an
    acknowledgement/flagging pass, not an approve/reject gate." It's
    audited prominently (PEER_REVIEW_FLAGGED) for a human to notice, but
    the count is what gates completion, not the specific outcome value.
  - The workflow reaches APPROVED only once BOTH the approval phase
    (if required) and the peer review phase (if required) are done and
    neither ended in REJECTED.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval.rules import ApprovalRule, ApprovalStepType
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
from app.services.approval import approver_resolution_service as resolver
from app.services.approval import notification_outbox_service as outbox
from app.services.approval import rule_engine_service as engine


class ApprovalWorkflowEngineError(Exception):
    def __init__(self, message: str, *, conflicting_rule_ids: list[int] | None = None):
        self.conflicting_rule_ids = conflicting_rule_ids or []
        super().__init__(message)


def _trace_to_dict(trace: engine.EvaluationTrace) -> dict:
    return {
        "candidates_considered": trace.candidates_considered,
        "conflict": trace.conflict,
        "conflicting_rule_ids": trace.conflicting_rule_ids,
        "winner_rule_id": trace.winner.rule_id if trace.winner else None,
        "matched_rule_ids": [e.rule_id for e in trace.matched_rules],
        "evaluations": [
            {"rule_id": e.rule_id, "rule_name": e.rule_name, "matched": e.matched, "specificity": e.specificity, "reason": e.reason}
            for e in trace.evaluations
        ],
    }


async def create_workflow(
    db: AsyncSession, *, organization_id: int, invoice_id: int, context: engine.ApprovalMatchContext, created_by: int | None = None,
) -> ApprovalWorkflow | None:
    """Returns None when nothing needs to happen — no rule matched, or
    the winning rule requires neither approval nor peer review — never a
    fabricated workflow for a "nothing to do" case. Raises
    ApprovalWorkflowEngineError on a genuine RULE_CONFLICT rather than
    guessing.

    Real bug found live: this used to bail out on `rule.action ==
    AUTO_APPROVE` alone, which silently discarded a legitimate
    "auto-approve the amount gate, but still route it to a peer
    reviewer" rule (action=AUTO_APPROVE + peer_review_required=True is a
    valid, unrejected combination — Phase 3's validation only cross-checks
    action against approval_required, never against peer_review_required).
    The actual question is whether EITHER gate is needed, not what the
    approval-specific `action` verdict says."""
    # PAY13 — tenant-isolation hardening: this route is reachable directly
    # (POST /approvals/workflows, "admin/test-triggered creation outside
    # the automatic pipeline path" per the module docstring above), so
    # invoice_id is caller-supplied and was never checked against the
    # caller's own organization_id before this fix. Every org currently
    # shares one physical database until it actually cuts over via
    # Bring-Your-Own-Database (see tenant_routing.py's own docstring), so
    # an unchecked invoice_id let one org create a workflow that silently
    # pointed at another org's real invoice row — never surfaced by this
    # endpoint's own response, but later fetched cross-org by
    # api/approvals.py's own invoice-enrichment code (also hardened in
    # this phase, defense-in-depth). Real bug found live, not theoretical.
    from app.models.invoice_intelligence.core import ApInvoice

    invoice = await db.get(ApInvoice, invoice_id)
    if invoice is None or (invoice.organization_id is not None and invoice.organization_id != organization_id):
        raise ApprovalWorkflowEngineError(f"Invoice {invoice_id} was not found in this organization.")

    # PAY16 — idempotency guard (spec §111): every intake path that can
    # reach this function (the scheduled document pipeline, Review Queue
    # re-decision, reprocess/revalidate, and the direct admin API route)
    # can legitimately be invoked more than once for the very same
    # invoice — a scheduler retry re-discovering an already-processed
    # source record, a duplicate webhook, a double-clicked "Run Now".
    # An invoice that already has a workflow still PENDING/IN_PROGRESS
    # (awaiting a human) or already APPROVED (the decision is already
    # made) must never get a second workflow — that would double-notify
    # approvers, or reopen an approval question that's already resolved.
    # Deliberately NOT checked against REJECTED/CANCELLED/EXPIRED: the
    # model's own docstring on invoice_id (see class ApprovalWorkflow
    # above) documents that a REJECTED invoice, once corrected, is
    # expected to be resubmitted and get a genuinely new workflow — this
    # guard must not break that resubmission path.
    # PAY22 — AUTO_APPROVED added alongside the pre-existing terminal/
    # active statuses: it's just as "already decided" as APPROVED is
    # (nothing further will ever happen to it), so a repeat call
    # (reprocess/revalidate, a scheduler re-discovering the same source
    # record) must suppress-and-return it exactly like it already did
    # for APPROVED, not spawn a second AUTO_APPROVED row every re-run.
    existing = (await db.execute(
        select(ApprovalWorkflow)
        .where(
            ApprovalWorkflow.invoice_id == invoice_id,
            ApprovalWorkflow.organization_id == organization_id,
            ApprovalWorkflow.status.in_([
                ApprovalWorkflowStatus.PENDING, ApprovalWorkflowStatus.IN_PROGRESS,
                ApprovalWorkflowStatus.APPROVED, ApprovalWorkflowStatus.AUTO_APPROVED,
            ]),
        )
        .order_by(ApprovalWorkflow.created_at.desc())
    )).scalars().first()
    if existing is not None:
        db.add(ApprovalWorkflowAuditLog(
            organization_id=organization_id, workflow_id=existing.id, invoice_id=invoice_id,
            event_type=ApprovalWorkflowAuditEventType.DUPLICATE_SUPPRESSED,
            message=(
                f"create_workflow() called again for invoice {invoice_id}, which already has "
                f"workflow #{existing.id} (status={existing.status.value}) — suppressed, no duplicate created."
            ),
            details={"existing_workflow_id": existing.id, "existing_status": existing.status.value},
        ))
        await db.commit()
        return existing

    trace = await engine.evaluate(db, organization_id, context)
    if trace.conflict:
        raise ApprovalWorkflowEngineError(
            f"RULE_CONFLICT: {len(trace.conflicting_rule_ids)} equally-specific rules tie for this invoice — "
            f"resolve the conflict (adjust priority or conditions) before a workflow can be created.",
            conflicting_rule_ids=trace.conflicting_rule_ids,
        )
    if trace.winner is None:
        # PAY22 — no ApprovalRule matched this invoice at all. Previously
        # this returned None (nothing persisted); now it persists a
        # terminal AUTO_APPROVED record with rule_id=NULL so every
        # invoice that reaches this function has SOME workflow row to
        # show in the Approval Workflows list, per the explicit ask that
        # motivated this phase. rule=None is the "no rule to reference"
        # case; the sibling "a rule matched but needed no gate" case
        # below passes the actual rule through instead.
        return await _create_auto_approved(db, organization_id=organization_id, invoice_id=invoice_id, context=context, trace=trace, rule=None, created_by=created_by)

    rule = await db.get(ApprovalRule, trace.winner.rule_id)
    if rule is None or not (rule.approval_required or rule.peer_review_required):
        return await _create_auto_approved(db, organization_id=organization_id, invoice_id=invoice_id, context=context, trace=trace, rule=rule, created_by=created_by)

    now = datetime.now(timezone.utc)
    workflow = ApprovalWorkflow(
        organization_id=organization_id, invoice_id=invoice_id, rule_id=rule.id, rule_name=rule.name, rule_version=rule.version,
        status=ApprovalWorkflowStatus.PENDING, approval_required=rule.approval_required, approval_mode=rule.approval_mode,
        approval_count_required=rule.approval_count, current_approval_step_number=1 if rule.approval_required else None,
        peer_review_required=rule.peer_review_required, peer_review_mode=rule.peer_review_mode,
        peer_review_count_required=rule.peer_review_count, current_peer_review_step_number=1 if rule.peer_review_required else None,
        context_snapshot={"amount": context.amount, "currency": context.currency, "invoice_type": context.invoice_type, "conditions": context.conditions},
        evaluation_trace=_trace_to_dict(trace), created_by=created_by, started_at=now,
    )
    db.add(workflow)
    await db.flush()

    rule_steps = await engine.get_rule_steps(db, rule.id)
    for rs in rule_steps:
        sla_hours = rs.sla_hours if rs.sla_hours is not None else rule.default_sla_hours
        common = dict(
            organization_id=organization_id, workflow_id=workflow.id, rule_step_id=rs.id, step_number=rs.step_number,
            approver_type=rs.approver_type, approver_role_id=rs.approver_role_id, approver_user_id=rs.approver_user_id,
            approver_department=rs.approver_department, approver_pool=rs.approver_pool,
            sla_hours=sla_hours, reminder_after_hours=rs.reminder_after_hours, escalation_after_hours=rs.escalation_after_hours,
            escalation_approver_type=rs.escalation_approver_type, escalation_role_id=rs.escalation_role_id,
            escalation_user_id=rs.escalation_user_id,
        )
        if rs.step_type == ApprovalStepType.APPROVAL:
            db.add(ApprovalWorkflowStep(status=ApprovalStepStatus.PENDING, **common))
        else:
            db.add(PeerReviewStep(status=PeerReviewStatus.PENDING, **common))

    workflow.status = ApprovalWorkflowStatus.IN_PROGRESS
    db.add(ApprovalWorkflowAuditLog(
        organization_id=organization_id, workflow_id=workflow.id, invoice_id=invoice_id,
        event_type=ApprovalWorkflowAuditEventType.CREATED,
        message=f"Workflow created from rule '{rule.name}' v{rule.version}.", actor_id=created_by,
        details={"rule_id": rule.id, "rule_version": rule.version},
    ))
    await db.commit()
    await db.refresh(workflow)

    await _resolve_and_notify_active_steps(db, workflow)
    return workflow


async def _create_auto_approved(
    db: AsyncSession, *, organization_id: int, invoice_id: int, context: engine.ApprovalMatchContext,
    trace: engine.EvaluationTrace, rule: ApprovalRule | None, created_by: int | None,
) -> ApprovalWorkflow:
    """PAY22 — the "nothing needs a human" outcome of create_workflow(),
    now a real persisted row instead of a bare None return. Terminal from
    the moment it's created (no PENDING/IN_PROGRESS window — there is no
    step for anyone to act on), zero ApprovalWorkflowStep/PeerReviewStep
    rows, and explicitly excluded from every "does this invoice need to
    wait on approval" check (invoice_decision_service.decide() only ever
    flips the invoice to APPROVING for PENDING/IN_PROGRESS — see its own
    comment) — this function changes what gets recorded, never what
    happens to the invoice.

    `rule` distinguishes the two ways this is reached: None when no
    ApprovalRule matched at all (rule_id stays NULL — see the model's own
    comment on why that's a real, distinct fact worth keeping), or the
    actual matched ApprovalRule when it matched but needed neither
    approval_required nor peer_review_required (rule_id populated, for
    an honest "yes, a rule owned this decision" record)."""
    now = datetime.now(timezone.utc)
    workflow = ApprovalWorkflow(
        organization_id=organization_id, invoice_id=invoice_id,
        rule_id=rule.id if rule else None, rule_name=rule.name if rule else None, rule_version=rule.version if rule else None,
        status=ApprovalWorkflowStatus.AUTO_APPROVED, approval_required=False, peer_review_required=False,
        approval_count_required=0, peer_review_count_required=0,
        context_snapshot={"amount": context.amount, "currency": context.currency, "invoice_type": context.invoice_type, "conditions": context.conditions},
        evaluation_trace=_trace_to_dict(trace), created_by=created_by, started_at=now, completed_at=now,
    )
    db.add(workflow)
    await db.flush()

    message = (
        f"No approval rule matched — auto-approved, no human review needed." if rule is None
        else f"Rule '{rule.name}' v{rule.version} matched but requires neither approval nor peer review — auto-approved."
    )
    db.add(ApprovalWorkflowAuditLog(
        organization_id=organization_id, workflow_id=workflow.id, invoice_id=invoice_id,
        event_type=ApprovalWorkflowAuditEventType.AUTO_APPROVED, message=message, actor_id=created_by,
        details={"rule_id": rule.id if rule else None, "rule_version": rule.version if rule else None},
    ))
    await db.commit()
    await db.refresh(workflow)
    return workflow


async def _resolve_and_notify_active_steps(db: AsyncSession, workflow: ApprovalWorkflow) -> None:
    """Resolves whatever step(s) are due right now (Phase 5's own
    primitive) and notifies (Phase 6's own primitive) anyone who ends up
    ASSIGNED — safe to call repeatedly; both underlying calls are
    idempotent, so re-notifying an already-notified assignee is a no-op.

    Real bug found live (Phase 8): notify_step_assigned() opens its own
    `AsyncSessionLocal()` connection for the Bell write while `db` (this
    function's tenant connection) still has an open, uncommitted
    transaction from enqueue()'s flush() a moment earlier — on SQLite
    that's two connections each wanting the single write lock in the
    same coroutine's call stack, which can't resolve itself and surfaces
    as "database is locked" once busy_timeout expires. This only ever
    showed up with >=2 steps notified in one call (PARALLEL mode
    assigning several steps at once) — a single-step SEQUENTIAL advance
    never hit it. Fix: commit `db` after each per-step notify, so its
    write lock is released before the NEXT iteration's separate
    connection needs one."""
    await resolver.resolve_and_assign_active_steps(db, workflow)
    await db.commit()

    assigned_steps = (await db.execute(
        select(ApprovalWorkflowStep).where(ApprovalWorkflowStep.workflow_id == workflow.id, ApprovalWorkflowStep.status == ApprovalStepStatus.ASSIGNED)
    )).scalars().all()
    for s in assigned_steps:
        await outbox.notify_step_assigned(db, workflow, s)
        await db.commit()

    assigned_peer_steps = (await db.execute(
        select(PeerReviewStep).where(PeerReviewStep.workflow_id == workflow.id, PeerReviewStep.status == PeerReviewStatus.ASSIGNED)
    )).scalars().all()
    for s in assigned_peer_steps:
        await outbox.notify_step_assigned(db, workflow, s)
        await db.commit()


async def _phase_done(db: AsyncSession, step_model, workflow_id: int) -> bool:
    """True once no step of this model remains PENDING or ASSIGNED for
    this workflow — the phase has genuinely run its course (every step
    is APPROVED/REJECTED/REVIEWED/FLAGGED/SKIPPED/EXPIRED/ESCALATED)."""
    open_status_values = (
        [ApprovalStepStatus.PENDING, ApprovalStepStatus.ASSIGNED] if step_model is ApprovalWorkflowStep
        else [PeerReviewStatus.PENDING, PeerReviewStatus.ASSIGNED]
    )
    count = (await db.execute(
        select(func.count()).select_from(step_model).where(step_model.workflow_id == workflow_id, step_model.status.in_(open_status_values))
    )).scalar()
    return count == 0


async def _sync_invoice_status(db: AsyncSession, invoice_id: int, *, approved: bool, reason: str | None = None) -> None:
    """PAY9 — the other half of invoice_decision_service.decide()'s own
    APPROVING transition: once the workflow that put an invoice into
    APPROVING resolves, the invoice needs to move again too, or it would
    sit in APPROVING forever with nothing ever clearing it (and
    invoice_erp_service.post_invoice()'s own approval-gate would block it
    permanently even after a real APPROVED outcome). Only touches an
    invoice actually still APPROVING — defensive against a workflow
    resolving twice or an invoice that moved on for an unrelated reason;
    never overwrites a state this workflow didn't put it in.

    Lazy-imported (not a top-level import) so this approval package
    doesn't take a hard dependency on invoice_intelligence at module load
    time — mirrors this file's own existing style of importing
    review_queue_service/outbox lazily elsewhere in this codebase."""
    from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceDecision, ApInvoiceStatus

    invoice = await db.get(ApInvoice, invoice_id)
    if invoice is None or invoice.status != ApInvoiceStatus.APPROVING:
        return
    if approved:
        invoice.status = ApInvoiceStatus.READY_TO_POST
    else:
        invoice.status = ApInvoiceStatus.REJECTED
        invoice.final_decision = ApInvoiceDecision.REJECT
        invoice.decision_reasons = [*(invoice.decision_reasons or []), {"rule_id": None, "category": "APPROVAL_REJECTED", "result": "FAIL", "message": reason or "Rejected by the Payment Approval workflow."}]
    await db.commit()


async def _maybe_complete_workflow(db: AsyncSession, workflow: ApprovalWorkflow) -> None:
    if workflow.status != ApprovalWorkflowStatus.IN_PROGRESS:
        return  # already terminal (REJECTED/APPROVED/CANCELLED/EXPIRED)

    approval_done = (not workflow.approval_required) or await _phase_done(db, ApprovalWorkflowStep, workflow.id)
    peer_review_done = (not workflow.peer_review_required) or await _phase_done(db, PeerReviewStep, workflow.id)
    if not (approval_done and peer_review_done):
        return

    workflow.status = ApprovalWorkflowStatus.APPROVED
    workflow.completed_at = datetime.now(timezone.utc)
    db.add(ApprovalWorkflowAuditLog(
        organization_id=workflow.organization_id, workflow_id=workflow.id, invoice_id=workflow.invoice_id,
        event_type=ApprovalWorkflowAuditEventType.COMPLETED,
        message="Workflow approved — all required approvals/peer reviews complete.",
    ))
    await db.commit()
    await _sync_invoice_status(db, workflow.invoice_id, approved=True)
    await outbox.notify_workflow_decided(db, workflow, "APPROVED")
    await db.commit()


def _check_organization(workflow: ApprovalWorkflow, organization_id: int | None) -> None:
    if organization_id is not None and workflow.organization_id is not None and workflow.organization_id != organization_id:
        raise ApprovalWorkflowEngineError("Workflow not found.")


async def act_on_approval_step(
    db: AsyncSession, *, organization_id: int | None, step_id: int, actor_user_id: int, approve: bool,
    comment: str | None = None, workflow_id: int | None = None,
) -> ApprovalWorkflowStep:
    step = await db.get(ApprovalWorkflowStep, step_id)
    if step is None:
        raise ApprovalWorkflowEngineError("Step not found.")
    # PAY13 — API-honesty hardening: the URL carries both /workflows/{workflow_id}/
    # and /steps/{step_id}/, but only step_id was ever actually used to look
    # anything up — a caller could pass any workflow_id in the path (even one
    # from a different workflow, or one they can't otherwise see) and it was
    # silently ignored rather than checked. Org + assigned-user checks below
    # already made this safe from an authorization standpoint, but a mismatched
    # workflow_id in the path should 404, not silently succeed against a
    # different workflow than the URL claims.
    if workflow_id is not None and step.workflow_id != workflow_id:
        raise ApprovalWorkflowEngineError("Step not found.")
    workflow = await db.get(ApprovalWorkflow, step.workflow_id)
    _check_organization(workflow, organization_id)

    if step.status != ApprovalStepStatus.ASSIGNED:
        raise ApprovalWorkflowEngineError(f"Step #{step.step_number} is not awaiting action (status={step.status.value}).")
    if step.assigned_user_id != actor_user_id:
        raise ApprovalWorkflowEngineError("Only the assigned approver may act on this step.")

    now = datetime.now(timezone.utc)
    step.status = ApprovalStepStatus.APPROVED if approve else ApprovalStepStatus.REJECTED
    step.decision_comment = comment
    step.acted_by = actor_user_id
    step.acted_at = now

    db.add(ApprovalWorkflowAuditLog(
        organization_id=workflow.organization_id, workflow_id=workflow.id, invoice_id=workflow.invoice_id,
        event_type=ApprovalWorkflowAuditEventType.STEP_APPROVED if approve else ApprovalWorkflowAuditEventType.STEP_REJECTED,
        message=f"Step #{step.step_number} {'approved' if approve else 'rejected'} by user {actor_user_id}." + (f" Comment: {comment}" if comment else ""),
        actor_id=actor_user_id, details={"step_id": step.id, "comment": comment},
    ))

    if not approve:
        workflow.status = ApprovalWorkflowStatus.REJECTED
        workflow.completed_at = now
        workflow.decision_reason = comment or f"Rejected at step #{step.step_number}."
        db.add(ApprovalWorkflowAuditLog(
            organization_id=workflow.organization_id, workflow_id=workflow.id, invoice_id=workflow.invoice_id,
            event_type=ApprovalWorkflowAuditEventType.COMPLETED, message=f"Workflow rejected at step #{step.step_number}.",
        ))
        await db.commit()
        await _sync_invoice_status(db, workflow.invoice_id, approved=False, reason=workflow.decision_reason)
        await outbox.notify_workflow_decided(db, workflow, "REJECTED")
        await db.commit()
        return step

    workflow.approval_count_completed += 1
    if workflow.approval_mode.value == "PARALLEL":
        if workflow.approval_count_completed >= workflow.approval_count_required:
            remaining = (await db.execute(
                select(ApprovalWorkflowStep).where(
                    ApprovalWorkflowStep.workflow_id == workflow.id,
                    ApprovalWorkflowStep.id != step.id,
                    ApprovalWorkflowStep.status.in_([ApprovalStepStatus.PENDING, ApprovalStepStatus.ASSIGNED]),
                )
            )).scalars().all()
            for r in remaining:
                r.status = ApprovalStepStatus.SKIPPED
                db.add(ApprovalWorkflowAuditLog(
                    organization_id=workflow.organization_id, workflow_id=workflow.id, invoice_id=workflow.invoice_id,
                    event_type=ApprovalWorkflowAuditEventType.STEP_SKIPPED,
                    message=f"Step #{r.step_number} skipped — approval_count_required already met.",
                ))
    else:  # SEQUENTIAL
        next_step = (await db.execute(
            select(ApprovalWorkflowStep)
            .where(ApprovalWorkflowStep.workflow_id == workflow.id, ApprovalWorkflowStep.step_number > step.step_number)
            .order_by(ApprovalWorkflowStep.step_number)
            .limit(1)
        )).scalar_one_or_none()
        workflow.current_approval_step_number = next_step.step_number if next_step else None

    await db.commit()
    await _resolve_and_notify_active_steps(db, workflow)
    await _maybe_complete_workflow(db, workflow)
    return step


async def act_on_peer_review_step(
    db: AsyncSession, *, organization_id: int | None, step_id: int, actor_user_id: int, flagged: bool,
    comment: str | None = None, workflow_id: int | None = None,
) -> PeerReviewStep:
    step = await db.get(PeerReviewStep, step_id)
    if step is None:
        raise ApprovalWorkflowEngineError("Peer review step not found.")
    # PAY13 — see the identical note in act_on_approval_step() above.
    if workflow_id is not None and step.workflow_id != workflow_id:
        raise ApprovalWorkflowEngineError("Peer review step not found.")
    workflow = await db.get(ApprovalWorkflow, step.workflow_id)
    _check_organization(workflow, organization_id)

    if step.status != PeerReviewStatus.ASSIGNED:
        raise ApprovalWorkflowEngineError(f"Peer review step #{step.step_number} is not awaiting action (status={step.status.value}).")
    if step.assigned_user_id != actor_user_id:
        raise ApprovalWorkflowEngineError("Only the assigned reviewer may act on this step.")

    now = datetime.now(timezone.utc)
    step.status = PeerReviewStatus.FLAGGED if flagged else PeerReviewStatus.REVIEWED
    step.review_comment = comment
    step.reviewed_by = actor_user_id
    step.reviewed_at = now

    db.add(ApprovalWorkflowAuditLog(
        organization_id=workflow.organization_id, workflow_id=workflow.id, invoice_id=workflow.invoice_id,
        event_type=ApprovalWorkflowAuditEventType.PEER_REVIEW_FLAGGED if flagged else ApprovalWorkflowAuditEventType.PEER_REVIEW_REVIEWED,
        message=f"Peer review step #{step.step_number} {'flagged' if flagged else 'reviewed'} by user {actor_user_id}." + (f" Comment: {comment}" if comment else ""),
        actor_id=actor_user_id, details={"step_id": step.id, "comment": comment},
    ))

    # FLAGGED still counts toward completion — see module docstring on
    # why peer review's outcome value never blocks workflow completion.
    workflow.peer_review_count_completed += 1
    if workflow.peer_review_mode.value == "PARALLEL":
        if workflow.peer_review_count_completed >= workflow.peer_review_count_required:
            remaining = (await db.execute(
                select(PeerReviewStep).where(
                    PeerReviewStep.workflow_id == workflow.id,
                    PeerReviewStep.id != step.id,
                    PeerReviewStep.status.in_([PeerReviewStatus.PENDING, PeerReviewStatus.ASSIGNED]),
                )
            )).scalars().all()
            for r in remaining:
                r.status = PeerReviewStatus.SKIPPED
                db.add(ApprovalWorkflowAuditLog(
                    organization_id=workflow.organization_id, workflow_id=workflow.id, invoice_id=workflow.invoice_id,
                    event_type=ApprovalWorkflowAuditEventType.STEP_SKIPPED,
                    message=f"Peer review step #{r.step_number} skipped — peer_review_count_required already met.",
                ))
    else:  # SEQUENTIAL
        next_step = (await db.execute(
            select(PeerReviewStep)
            .where(PeerReviewStep.workflow_id == workflow.id, PeerReviewStep.step_number > step.step_number)
            .order_by(PeerReviewStep.step_number)
            .limit(1)
        )).scalar_one_or_none()
        workflow.current_peer_review_step_number = next_step.step_number if next_step else None

    await db.commit()
    await _resolve_and_notify_active_steps(db, workflow)
    await _maybe_complete_workflow(db, workflow)
    return step
