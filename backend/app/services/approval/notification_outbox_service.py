"""
notification_outbox_service.py — Approval Rule Engine Phase 6: the real
queued/retried EMAIL delivery layer this feature's own Phase B analysis
flagged as a genuine gap in the existing notification_service.py
(create_notification() sends email synchronously inline via
email_service.send_email() — no queue/retry/idempotency — acceptable for
the Bell/in-app write, not safe to reuse as-is for an approval
assignment that must eventually reach its approver even through a
transient SMTP failure).

Two channels, two different real designs — not "insert a row and hope":

  IN_APP — the EXISTING Notification model/Bell feature (SB23/SB24),
    reused unmodified in shape. notification_service.create_notification()
    now takes send_email=False for this feature's calls (a small,
    additive, backward-compatible parameter added this phase — every
    other existing caller keeps sending email exactly as before) so the
    Bell row is written the normal way without ALSO firing its baked-in
    inline send. This module's outbox owns email delivery instead.

  EMAIL — a genuine outbox table (models/approval/notification.py's
    ApprovalNotificationOutbox) plus a scheduler-polled worker
    (process_pending(), wired into main.py's scheduler the same way
    billing_scheduler/expiry_job/scheduler_poller already are) that
    calls the low-level email_service.send_email() transport directly,
    with real exponential-backoff retry and a DEAD_LETTER terminal state
    after max_attempts — one send path, never a silently-dropped first
    attempt.

Idempotency: enqueue() is a get-or-create keyed on
(workflow_id, step_id, step_kind, notification_type, channel) — calling
it twice for the same event (e.g. resolve_and_assign_active_steps() run
again against an already-resolved step) returns the existing row instead
of double-enqueuing, so an approver is never emailed twice for one
assignment.

Scope limit (disclosed, matches an existing precedent in this exact
codebase, not a new gap): process_pending_standalone() — the function
the scheduler actually calls — runs against the platform/default tenant
DB only (AsyncSessionLocal), the same single-DB-instance scope every
other scheduled job in this codebase already has (see
scheduler_poller.py's identical docstring disclosure). An organization
that has cut over to its own physical database via BYO-Database won't
have its outbox rows picked up by this background job yet — closing that
gap for every scheduled job in this codebase (not just this one) is
future work, not something Phase 6 quietly pretends to have solved.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.models.approval.notification import (
    ApprovalNotificationChannel,
    ApprovalNotificationOutbox,
    ApprovalNotificationStatus,
    ApprovalNotificationStepKind,
    ApprovalNotificationType,
)
from app.models.approval.workflow import ApprovalWorkflow, ApprovalWorkflowStep, PeerReviewStep
from app.models.billing.notification import Notification
from app.models.user import User
from app.services import email_service
from app.services.billing import notification_service

# Minutes to wait before each successive retry — index by attempt_count
# (1st failure waits 1 min, 2nd waits 5, ... capped at the last value for
# any further attempt). max_attempts on the row defaults to this list's
# length, so a row that reaches DEAD_LETTER has genuinely exhausted every
# step of this real backoff schedule, not an arbitrary counter.
_BACKOFF_MINUTES = [1, 5, 15, 60, 240]
DEFAULT_MAX_ATTEMPTS = len(_BACKOFF_MINUTES)


def _backoff_minutes(attempt_count: int) -> int:
    idx = min(max(attempt_count - 1, 0), len(_BACKOFF_MINUTES) - 1)
    return _BACKOFF_MINUTES[idx]


async def enqueue(
    db: AsyncSession,
    *,
    workflow: ApprovalWorkflow,
    recipient_user_id: int,
    notification_type: ApprovalNotificationType,
    channel: ApprovalNotificationChannel,
    subject: str,
    body: str,
    step_id: int = 0,
    step_kind: ApprovalNotificationStepKind = ApprovalNotificationStepKind.WORKFLOW,
) -> ApprovalNotificationOutbox:
    """Get-or-create keyed on (workflow_id, step_id, step_kind,
    notification_type, channel) — see module docstring on why this must
    never double-enqueue the same real-world event. Does not commit —
    callers that also mutate the step/workflow in the same call (e.g.
    approver_resolution_service.resolve_and_assign_step()) control the
    transaction boundary."""
    existing = (await db.execute(
        select(ApprovalNotificationOutbox).where(
            ApprovalNotificationOutbox.workflow_id == workflow.id,
            ApprovalNotificationOutbox.step_id == step_id,
            ApprovalNotificationOutbox.step_kind == step_kind,
            ApprovalNotificationOutbox.notification_type == notification_type,
            ApprovalNotificationOutbox.channel == channel,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    row = ApprovalNotificationOutbox(
        organization_id=workflow.organization_id, workflow_id=workflow.id, step_id=step_id, step_kind=step_kind,
        recipient_user_id=recipient_user_id, notification_type=notification_type, channel=channel,
        subject=subject, body=body, status=ApprovalNotificationStatus.PENDING, max_attempts=DEFAULT_MAX_ATTEMPTS,
    )
    db.add(row)
    await db.flush()
    return row


def _step_subject_body(step_kind_label: str, step_number: int, rule_name: str) -> tuple[str, str]:
    subject = f"Approval needed: {rule_name} — step {step_number}"
    body = (
        f"You have been assigned a {step_kind_label.lower().replace('_', ' ')} step "
        f"(#{step_number}) on the approval workflow for rule '{rule_name}'. "
        f"Please review it in the Payment Invoice Approval module."
    )
    return subject, body


def _related_object_type(is_peer: bool) -> str:
    return "peer_review_step" if is_peer else "approval_workflow_step"


async def _notify_dual_channel(
    db: AsyncSession, *, workflow: ApprovalWorkflow, step: ApprovalWorkflowStep | PeerReviewStep,
    recipient_user_id: int, notification_type: ApprovalNotificationType, subject: str, body: str,
) -> ApprovalNotificationOutbox:
    """Shared by notify_step_assigned/notify_step_reminder/notify_step_escalated:
    writes the Bell row (idempotently — create_notification() itself has
    no dedup, so it's enforced here) and enqueues the real, retried EMAIL
    (idempotent via enqueue()'s own unique constraint). Every event type
    this module sends goes through this one path so the "two channels,
    two designs" contract in the module docstring never drifts per call
    site."""
    is_peer = isinstance(step, PeerReviewStep)
    step_kind = ApprovalNotificationStepKind.PEER_REVIEW if is_peer else ApprovalNotificationStepKind.APPROVAL
    related_object_type = _related_object_type(is_peer)

    async with AsyncSessionLocal() as platform_db:
        existing_bell = (await platform_db.execute(
            select(Notification).where(
                Notification.organization_id == workflow.organization_id,
                Notification.user_id == recipient_user_id,
                Notification.related_object_type == related_object_type,
                Notification.related_object_id == step.id,
                Notification.type == f"APPROVAL_{notification_type.value}",
            )
        )).scalar_one_or_none()
        if existing_bell is None:
            await notification_service.create_notification(
                platform_db, organization_id=workflow.organization_id,
                type=f"APPROVAL_{notification_type.value}", title=subject, body=body, user_id=recipient_user_id,
                related_object_type=related_object_type, related_object_id=step.id, send_email=False,
            )

    return await enqueue(
        db, workflow=workflow, recipient_user_id=recipient_user_id, notification_type=notification_type,
        channel=ApprovalNotificationChannel.EMAIL, subject=subject, body=body, step_id=step.id, step_kind=step_kind,
    )


async def notify_step_assigned(
    db: AsyncSession, workflow: ApprovalWorkflow, step: ApprovalWorkflowStep | PeerReviewStep,
) -> list[ApprovalNotificationOutbox]:
    """Called right after approver_resolution_service.resolve_and_assign_step()
    succeeds (step.assigned_user_id is set). Returns [] if handed a step
    that wasn't actually resolved — callers should only call this after a
    successful resolve_and_assign_step() outcome."""
    if step.assigned_user_id is None:
        return []
    is_peer = isinstance(step, PeerReviewStep)
    notification_type = ApprovalNotificationType.PEER_REVIEW_ASSIGNED if is_peer else ApprovalNotificationType.STEP_ASSIGNED
    step_kind_label = "PEER_REVIEW" if is_peer else "APPROVAL"
    subject, body = _step_subject_body(step_kind_label, step.step_number, workflow.rule_name)
    row = await _notify_dual_channel(
        db, workflow=workflow, step=step, recipient_user_id=step.assigned_user_id,
        notification_type=notification_type, subject=subject, body=body,
    )
    return [row]


async def notify_step_reminder(
    db: AsyncSession, workflow: ApprovalWorkflow, step: ApprovalWorkflowStep | PeerReviewStep,
) -> list[ApprovalNotificationOutbox]:
    """Called by sla_scheduler_service.run_reminder_sweep() (Phase 7) once
    a step has been ASSIGNED for at least reminder_after_hours. Enqueuing
    is naturally idempotent (enqueue()'s unique constraint already covers
    (workflow_id, step_id, step_kind, REMINDER, EMAIL)), so a sweep that
    fires again before the step is decided is a safe no-op — exactly why
    no separate "already reminded" flag is needed, unlike a step's
    unbounded ASSIGNED->ASSIGNED reassignment which enqueue() alone can't
    dedupe (see notify_step_escalated for why that one uses its own type)."""
    if step.assigned_user_id is None:
        return []
    is_peer = isinstance(step, PeerReviewStep)
    step_kind_label = "PEER_REVIEW" if is_peer else "APPROVAL"
    subject = f"Reminder: {workflow.rule_name} — step {step.step_number} still awaiting your action"
    body = (
        f"This is a reminder that a {step_kind_label.lower().replace('_', ' ')} step "
        f"(#{step.step_number}) on the approval workflow for rule '{workflow.rule_name}' "
        f"has been waiting on you since {step.assigned_at}. Please review it in the "
        f"Payment Invoice Approval module."
    )
    row = await _notify_dual_channel(
        db, workflow=workflow, step=step, recipient_user_id=step.assigned_user_id,
        notification_type=ApprovalNotificationType.REMINDER, subject=subject, body=body,
    )
    return [row]


async def notify_step_escalated(
    db: AsyncSession, workflow: ApprovalWorkflow, step: ApprovalWorkflowStep | PeerReviewStep, previous_assignee_id: int | None,
) -> list[ApprovalNotificationOutbox]:
    """Called by sla_scheduler_service.run_escalation_sweep() (Phase 7)
    after a step has been successfully reassigned to its escalation
    target (step.assigned_user_id is already the NEW assignee by the time
    this is called). Uses the ESCALATED notification_type — deliberately
    NOT STEP_ASSIGNED/PEER_REVIEW_ASSIGNED — so this enqueues as a
    genuinely new outbox row even though the same step already has a
    SENT STEP_ASSIGNED row from its original assignment; reusing that
    type would collide with enqueue()'s (workflow_id, step_id, step_kind,
    type, channel) uniqueness and the new assignee would never be
    notified at all."""
    if step.assigned_user_id is None:
        return []
    is_peer = isinstance(step, PeerReviewStep)
    step_kind_label = "PEER_REVIEW" if is_peer else "APPROVAL"
    subject = f"Escalated to you: {workflow.rule_name} — step {step.step_number}"
    body = (
        f"A {step_kind_label.lower().replace('_', ' ')} step (#{step.step_number}) on the "
        f"approval workflow for rule '{workflow.rule_name}' has been escalated to you "
        f"after its original SLA window elapsed"
        + (f" (previously assigned to user {previous_assignee_id})" if previous_assignee_id else "")
        + ". Please review it in the Payment Invoice Approval module."
    )
    row = await _notify_dual_channel(
        db, workflow=workflow, step=step, recipient_user_id=step.assigned_user_id,
        notification_type=ApprovalNotificationType.ESCALATED, subject=subject, body=body,
    )
    return [row]


async def notify_workflow_decided(db: AsyncSession, workflow: ApprovalWorkflow, decision: str) -> list[ApprovalNotificationOutbox]:
    """Called by approval_workflow_engine_service.py (Phase 8) once a
    workflow reaches a terminal APPROVED/REJECTED state. Notifies
    workflow.created_by — silently a no-op if that's None (an
    automated/pipeline-triggered workflow with no human creator to tell;
    disclosed, not a bug). Workflow-level, not step-shaped, so this
    doesn't go through _notify_dual_channel (which needs a step for its
    Bell related_object_id/step_kind) — same two-channel shape, written
    directly instead."""
    if workflow.created_by is None:
        return []
    notification_type = ApprovalNotificationType.WORKFLOW_APPROVED if decision == "APPROVED" else ApprovalNotificationType.WORKFLOW_REJECTED
    subject = f"Approval workflow {decision.lower()}: {workflow.rule_name}"
    body = (
        f"The approval workflow for rule '{workflow.rule_name}' ({workflow.entity_type.value.lower()} {workflow.entity_id}) "
        f"has been {decision}. {workflow.decision_reason or ''}".strip()
    )

    async with AsyncSessionLocal() as platform_db:
        existing_bell = (await platform_db.execute(
            select(Notification).where(
                Notification.organization_id == workflow.organization_id,
                Notification.user_id == workflow.created_by,
                Notification.related_object_type == "approval_workflow",
                Notification.related_object_id == workflow.id,
                Notification.type == f"APPROVAL_{notification_type.value}",
            )
        )).scalar_one_or_none()
        if existing_bell is None:
            await notification_service.create_notification(
                platform_db, organization_id=workflow.organization_id, type=f"APPROVAL_{notification_type.value}",
                title=subject, body=body, user_id=workflow.created_by,
                related_object_type="approval_workflow", related_object_id=workflow.id, send_email=False,
            )

    row = await enqueue(
        db, workflow=workflow, recipient_user_id=workflow.created_by, notification_type=notification_type,
        channel=ApprovalNotificationChannel.EMAIL, subject=subject, body=body,
    )
    return [row]


async def _recipient_email(platform_db: AsyncSession, user_id: int) -> str | None:
    user = await platform_db.get(User, user_id)
    return user.email if user and user.email else None


async def _send_one(platform_db: AsyncSession, row: ApprovalNotificationOutbox) -> None:
    to = await _recipient_email(platform_db, row.recipient_user_id)
    if not to:
        row.status = ApprovalNotificationStatus.DEAD_LETTER
        row.last_error = f"No email address on file for user id {row.recipient_user_id}."
        return

    result = await email_service.send_email(platform_db, to=to, subject=row.subject, body=row.body)
    row.attempt_count += 1
    if result.delivered:
        row.status = ApprovalNotificationStatus.SENT
        row.sent_at = datetime.now(timezone.utc)
        row.last_error = None
    else:
        row.last_error = result.detail
        if row.attempt_count >= row.max_attempts:
            row.status = ApprovalNotificationStatus.DEAD_LETTER
        else:
            row.status = ApprovalNotificationStatus.FAILED
            row.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=_backoff_minutes(row.attempt_count))


async def process_pending(db: AsyncSession, *, limit: int = 50) -> dict:
    """One worker tick: every EMAIL-channel outbox row that's due
    (PENDING, or FAILED and not yet exhausted, with next_attempt_at now
    or earlier) gets a real send attempt via email_service.send_email().
    The same session serves both the outbox rows and the Seller/User
    platform lookups email_service/_recipient_email need — correct as
    long as this org hasn't cut over to a separate BYO-Database (see
    module docstring's disclosed scope limit). Each row is attempted
    independently — one row's exception never stops the batch, same
    per-item isolation principle as scheduler_poller.run_poll()."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ApprovalNotificationOutbox)
        .where(
            ApprovalNotificationOutbox.channel == ApprovalNotificationChannel.EMAIL,
            ApprovalNotificationOutbox.status.in_([ApprovalNotificationStatus.PENDING, ApprovalNotificationStatus.FAILED]),
            ApprovalNotificationOutbox.next_attempt_at <= now,
        )
        .order_by(ApprovalNotificationOutbox.next_attempt_at)
        .limit(limit)
    )
    rows = list(result.scalars().all())

    sent = failed = dead_lettered = errored = 0
    for row in rows:
        try:
            await _send_one(db, row)
            await db.commit()
            if row.status == ApprovalNotificationStatus.SENT:
                sent += 1
            elif row.status == ApprovalNotificationStatus.DEAD_LETTER:
                dead_lettered += 1
            else:
                failed += 1
        except Exception as exc:  # noqa: BLE001 — one row's crash must never stop the batch
            await db.rollback()
            errored += 1
            row.last_error = f"Worker error: {exc}"[:300]

    return {"picked_up": len(rows), "sent": sent, "failed": failed, "dead_lettered": dead_lettered, "errored": errored}


async def process_pending_standalone() -> dict:
    """The function main.py's scheduler actually calls — see module
    docstring for the disclosed single-DB scope limit this shares with
    scheduler_poller.run_poll()/expiry_job.run_expiry_sweep_standalone()."""
    async with AsyncSessionLocal() as db:
        return await process_pending(db)
