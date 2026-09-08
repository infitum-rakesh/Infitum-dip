"""
sla_scheduler_service.py — Approval Rule Engine Phase 7: the reminder and
escalation sweeps that finally act on the sla_hours/reminder_after_hours/
escalation_after_hours/due_at/escalated_at columns Phase 4's
ApprovalWorkflowStep/PeerReviewStep have carried since their creation
(disclosed then as "the natural home for that data... populated by a
later phase's scheduler job, not this one" — this is that phase).

Two independent sweeps, run on the same interval by the same scheduler
job (main.py) but logically separate:

  REMINDER — a step that's been ASSIGNED for >= reminder_after_hours
    gets one reminder notification (notification_outbox_service.
    notify_step_reminder(), Phase 6's Bell+EMAIL dual-channel path).
    Naturally idempotent: notify_step_reminder()'s outbox enqueue is
    keyed on (workflow_id, step_id, step_kind, REMINDER, EMAIL), so a
    sweep firing again before the step is decided is a safe no-op — no
    separate "already reminded" flag needed.

  ESCALATION — a step that's been ASSIGNED for >= escalation_after_hours
    AND hasn't been escalated yet (escalated_at IS NULL) gets its
    escalation target (escalation_approver_type/_role_id/_user_id)
    resolved via approver_resolution_service.resolve() — the EXACT SAME
    ROLE/USER resolution logic Phase 5 built for the primary assignment,
    just fed the step's escalation_* fields instead of its approver_*
    fields, so escalation resolution never duplicates that logic.
      - On success: assigned_user_id is REASSIGNED to the escalation
        target, assigned_at/due_at are RESET (the new assignee gets a
        fresh SLA window, not the elapsed remainder of the original
        one), escalated_at is stamped (so this step is never
        re-escalated again — one escalation per step, matching the
        schema's single escalation_* target, not a chain), an ESCALATED
        ApprovalWorkflowAuditLog row is written, and the new assignee is
        notified (notify_step_escalated()).
      - On failure (no eligible escalation target — e.g. the named role
        has nobody active in it): escalated_at is STILL stamped (so a
        permanently-unresolvable escalation doesn't retry forever every
        sweep) and step.status becomes ESCALATED — a genuinely stuck
        state (see ApprovalStepStatus/PeerReviewStatus's own ESCALATED
        member) signaling a human needs to intervene, exactly the same
        "recorded honestly, never silently skipped or faked" standard
        Phase 5's RESOLUTION_FAILED case set for primary assignment.

A step with escalation_after_hours set but escalation_approver_type NULL
(nothing to escalate to) is skipped — Phase 3's validate_rule_definition()
now rejects new rules shaped that way (a small addition made alongside
this phase), but this sweep still defends against it defensively for any
row that predates that check.

Scope limit (disclosed, same precedent as notification_outbox_service.py
and scheduler_poller.py): runs against the platform/default tenant DB
only (AsyncSessionLocal). An org on a separate BYO-Database physical
database won't have its steps swept by this job yet — a pre-existing,
already-disclosed limitation across every scheduled job in this
codebase, not one this phase introduces.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.models.approval.workflow import (
    ApprovalStepStatus,
    ApprovalWorkflow,
    ApprovalWorkflowAuditEventType,
    ApprovalWorkflowAuditLog,
    ApprovalWorkflowStep,
    PeerReviewStatus,
    PeerReviewStep,
)
from app.services.approval import approver_resolution_service as resolver
from app.services.approval import notification_outbox_service as outbox

# (step_model, its own ASSIGNED enum value) — both step tables share the
# identical column shape, only the status enum type differs.
_STEP_MODELS = [
    (ApprovalWorkflowStep, ApprovalStepStatus.ASSIGNED),
    (PeerReviewStep, PeerReviewStatus.ASSIGNED),
]


async def _due_rows(db: AsyncSession, step_model, assigned_status, hours_field: str, *, unescalated_only: bool = False):
    """Every ASSIGNED row of this model with `hours_field` set and
    assigned_at + hours_field <= now. Filtered in Python after a single
    coarse SQL fetch (assigned_at IS NOT NULL, hours_field IS NOT NULL) —
    portable across SQLite/Postgres without per-dialect date arithmetic,
    and the row counts here are small (open approval steps, not bulk
    data), matching this codebase's existing scheduler_poller.py style."""
    hours_col = getattr(step_model, hours_field)
    conditions = [
        step_model.status == assigned_status,
        hours_col.is_not(None),
        step_model.assigned_at.is_not(None),
    ]
    if unescalated_only:
        conditions.append(step_model.escalated_at.is_(None))
    result = await db.execute(select(step_model).where(*conditions))
    now = datetime.now(timezone.utc)
    due = []
    for row in result.scalars().all():
        hours = getattr(row, hours_field)
        if row.assigned_at + timedelta(hours=hours) <= now:
            due.append(row)
    return due


async def run_reminder_sweep(db: AsyncSession) -> dict:
    """Note on the returned count: notify_step_reminder() is safe to call
    every tick for the same overdue step (its outbox enqueue dedupes), so
    `reminders_processed` counts overdue steps CONSIDERED this tick, not
    distinct new sends — the dedup guarantee lives in the outbox, not in
    this counter. Actual delivery still happens exactly once per step,
    via process_pending()'s own SENT/DEAD_LETTER bookkeeping.

    Commits after EACH step (not once at the end) — found live
    (approval_workflow_engine_service.py's identical fix has the full
    explanation): notify_step_reminder() opens its own AsyncSessionLocal()
    connection for the Bell write while `db` may still hold an open,
    uncommitted transaction from the previous iteration's enqueue()
    flush — two connections wanting SQLite's one write lock in the same
    call stack, surfacing as "database is locked" once >=2 steps are due
    in the same sweep tick."""
    processed = 0
    for step_model, assigned_status in _STEP_MODELS:
        for step in await _due_rows(db, step_model, assigned_status, "reminder_after_hours"):
            workflow = await db.get(ApprovalWorkflow, step.workflow_id)
            if workflow is None:
                continue
            await outbox.notify_step_reminder(db, workflow, step)
            await db.commit()
            processed += 1
    return {"reminders_processed": processed}


async def _resolve_escalation_target(db: AsyncSession, workflow: ApprovalWorkflow, step_model, step) -> resolver.ResolutionOutcome:
    return await resolver.resolve(
        db, organization_id=workflow.organization_id, step_model=step_model,
        approver_type=step.escalation_approver_type, approver_role_id=step.escalation_role_id,
        approver_user_id=step.escalation_user_id, approver_department=None, approver_pool=None,
    )


async def run_escalation_sweep(db: AsyncSession) -> dict:
    """Commits after EACH step for the same reason as run_reminder_sweep
    above — notify_step_escalated()'s nested AsyncSessionLocal() write
    must never overlap an uncommitted write on `db` from a prior
    iteration."""
    escalated = failed = skipped_unconfigured = 0
    for step_model, assigned_status in _STEP_MODELS:
        is_peer = step_model is PeerReviewStep
        for step in await _due_rows(db, step_model, assigned_status, "escalation_after_hours", unescalated_only=True):
            if step.escalation_approver_type is None:
                # Defensive only — Phase 3's validate_rule_definition()
                # now rejects new rules shaped this way (escalation_after_
                # hours without escalation_approver_type); a pre-existing
                # row could still lack it. Nothing to escalate to, so
                # leave it alone rather than guessing a target.
                skipped_unconfigured += 1
                continue

            workflow = await db.get(ApprovalWorkflow, step.workflow_id)
            if workflow is None:
                continue

            previous_assignee_id = step.assigned_user_id
            outcome = await _resolve_escalation_target(db, workflow, step_model, step)
            now = datetime.now(timezone.utc)

            if outcome.resolved:
                step.assigned_user_id = outcome.assigned_user_id
                step.assigned_at = now
                step.due_at = now + timedelta(hours=step.sla_hours) if step.sla_hours else None
                step.escalated_at = now
                message = (
                    f"Step #{step.step_number} escalated from user {previous_assignee_id} to "
                    f"user {outcome.assigned_user_id} after {step.escalation_after_hours}h. {outcome.reason}"
                )
                db.add(ApprovalWorkflowAuditLog(
                    organization_id=workflow.organization_id, workflow_id=workflow.id, invoice_id=workflow.invoice_id,
                    event_type=ApprovalWorkflowAuditEventType.ESCALATED, message=message,
                    details={
                        "step_id": step.id, "step_kind": "PEER_REVIEW" if is_peer else "APPROVAL", "resolved": True,
                        "previous_assignee_id": previous_assignee_id, "new_assignee_id": outcome.assigned_user_id,
                    },
                ))
                await db.commit()
                await outbox.notify_step_escalated(db, workflow, step, previous_assignee_id)
                await db.commit()
                escalated += 1
            else:
                step.escalated_at = now  # never retry an unresolvable escalation forever
                step.status = PeerReviewStatus.ESCALATED if is_peer else ApprovalStepStatus.ESCALATED
                message = f"Step #{step.step_number} escalation FAILED after {step.escalation_after_hours}h — {outcome.reason}"
                db.add(ApprovalWorkflowAuditLog(
                    organization_id=workflow.organization_id, workflow_id=workflow.id, invoice_id=workflow.invoice_id,
                    event_type=ApprovalWorkflowAuditEventType.ESCALATED, message=message,
                    details={
                        "step_id": step.id, "step_kind": "PEER_REVIEW" if is_peer else "APPROVAL", "resolved": False,
                        "previous_assignee_id": previous_assignee_id, "reason": outcome.reason,
                    },
                ))
                await db.commit()
                failed += 1

    return {"escalated": escalated, "escalation_failed": failed, "escalation_skipped_unconfigured": skipped_unconfigured}


async def run_sla_sweep_standalone() -> dict:
    """The function main.py's scheduler actually calls — see module
    docstring for the disclosed single-DB scope limit."""
    async with AsyncSessionLocal() as db:
        reminder_stats = await run_reminder_sweep(db)
        escalation_stats = await run_escalation_sweep(db)
    return {**reminder_stats, **escalation_stats}
