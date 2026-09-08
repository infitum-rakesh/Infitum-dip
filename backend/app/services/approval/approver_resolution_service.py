"""
approver_resolution_service.py — Approval Rule Engine Phase 5: resolves
one step's approver_type/approver_*_id/approver_pool snapshot (Phase 4's
ApprovalWorkflowStep/PeerReviewStep) down to a concrete assigned_user_id.

Cross-boundary session handling: User and Role are PlatformBase — always
on the shared platform database (see database/connection.py's PlatformBase
docstring), never the tenant-routed session, and this stays true even
after an org cuts over to its own physical database via BYO-Database
(only TenantBase tables move; User/Role never do). Every User/Role lookup
here opens its own `async with AsyncSessionLocal() as platform_db:` —
exactly the pattern already used by review_queue_service._user_name_map()
and document_service.py — never the `db` argument this module's callers
pass in, which is the tenant-routed session for ApprovalWorkflowStep/
PeerReviewStep themselves.

Resolution policy: ROLE/DEPARTMENT/POOL can all match more than one
active user (a whole team), but ApprovalWorkflowStep/PeerReviewStep has
exactly ONE assigned_user_id column (see Phase 4's docstring — POOL
already promised "a configured resolution policy"; applied here
uniformly to all three multi-candidate approver types, not just POOL).
The policy: least current open-ASSIGNED-step load first — a real,
computed signal (counts this org's currently ASSIGNED
ApprovalWorkflowStep/PeerReviewStep rows per candidate, in the tenant db,
right now), not a fabricated round-robin counter — tie-broken by lowest
user id for determinism. USER is always exactly one candidate by
definition; no policy needed there.

A step whose approver_type/ids resolve to ZERO eligible active users (a
misconfigured rule naming a role nobody currently holds, an empty pool,
a deactivated specific user, a department with nobody in it, ...) is a
real, expected failure mode — recorded as a RESOLUTION_FAILED
ApprovalWorkflowAuditLog event and left unassigned/PENDING, never
silently skipped and never assigned to a fabricated placeholder user.

Explicitly OUT of scope for this phase (disclosed, not faked):
  - Deciding WHICH step(s) are "active right now" for a SEQUENTIAL vs
    PARALLEL workflow, and advancing current_approval_step_number/
    current_peer_review_step_number as steps complete — that sequencing
    decision belongs to the workflow ENGINE (a later phase). This module
    only resolves+assigns whatever specific step it's handed.
  - Notifying the assigned user (email/in-app) — a separate module
    (notification_outbox_service.py, Phase 6), called alongside this one
    by whatever orchestrates them, not by this module itself.

resolve() itself is also reused directly by sla_scheduler_service.py
(Phase 7) to resolve an ESCALATION target (escalation_approver_type/
escalation_role_id/escalation_user_id) — the exact same ROLE/USER
resolution logic, just fed a different set of approver_* fields off the
same step row, so escalation resolution never duplicates this logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.models.approval.rules import ApproverType
from app.models.approval.workflow import (
    ApprovalStepStatus,
    ApprovalWorkflow,
    ApprovalWorkflowAuditEventType,
    ApprovalWorkflowAuditLog,
    ApprovalWorkflowStep,
    PeerReviewStatus,
    PeerReviewStep,
)
from app.models.user import User


class ApproverResolutionError(Exception):
    """Raised only for genuinely-wrong input (an ApproverType this module
    doesn't recognize at all) — never for the merely-honest "zero
    eligible users found" outcome, which is reported through
    ResolutionOutcome instead, not an exception."""


@dataclass
class ResolutionOutcome:
    eligible_user_ids: list[int] = field(default_factory=list)
    assigned_user_id: int | None = None
    reason: str = ""
    # Real bug found live (user-reported): the audit-log message built
    # below from this outcome used to read "assigned to user {id}" — a
    # raw numeric id is meaningless to an admin reading the timeline.
    # `chosen` (a real User row) is already in hand at both `resolve()`
    # return points below, so its full_name rides along here for free
    # rather than making resolve_and_assign_step() do a second lookup.
    # None only for the zero-candidates failure path, where there's no
    # user to name.
    assigned_user_name: str | None = None

    @property
    def resolved(self) -> bool:
        return self.assigned_user_id is not None


async def _active_users_by_ids(platform_db: AsyncSession, ids: list[int]) -> list[User]:
    if not ids:
        return []
    result = await platform_db.execute(select(User).where(User.id.in_(ids), User.is_active.is_(True)))
    return list(result.scalars().all())


async def _eligible_users(
    organization_id: int,
    approver_type: ApproverType,
    approver_role_id: int | None,
    approver_user_id: int | None,
    approver_department: str | None,
    approver_pool: list[int] | None,
) -> list[User]:
    """Every active PlatformBase User matching this approver snapshot —
    opens its own platform session, see module docstring. Every branch
    also filters by organization_id defensively: a custom Role
    (approver_role_id) is itself org-scoped so this should never actually
    disagree, but a user's org membership is the genuine tenant boundary
    here, not an assumption about Role's own scoping."""
    async with AsyncSessionLocal() as platform_db:
        if approver_type == ApproverType.USER:
            if approver_user_id is None:
                return []
            users = await _active_users_by_ids(platform_db, [approver_user_id])
            return [u for u in users if u.organization_id == organization_id]

        if approver_type == ApproverType.ROLE:
            if approver_role_id is None:
                return []
            result = await platform_db.execute(
                select(User).where(
                    User.custom_role_id == approver_role_id,
                    User.organization_id == organization_id,
                    User.is_active.is_(True),
                )
            )
            return list(result.scalars().all())

        if approver_type == ApproverType.DEPARTMENT:
            if not (approver_department or "").strip():
                return []
            result = await platform_db.execute(
                select(User).where(
                    User.department == approver_department,
                    User.organization_id == organization_id,
                    User.is_active.is_(True),
                )
            )
            return list(result.scalars().all())

        if approver_type == ApproverType.POOL:
            if not approver_pool:
                return []
            users = await _active_users_by_ids(platform_db, approver_pool)
            return [u for u in users if u.organization_id == organization_id]

        raise ApproverResolutionError(f"Unrecognized approver_type: {approver_type!r}")


async def _current_assigned_load(db: AsyncSession, step_model: type, user_ids: list[int]) -> dict[int, int]:
    """How many of THIS step model's rows are RIGHT NOW status=ASSIGNED
    to each candidate, in the tenant db — a real, computed load signal
    (see module docstring), not a fabricated round-robin counter.
    `step_model` is ApprovalWorkflowStep or PeerReviewStep — both share
    the same assigned_user_id/status column shape, just different status
    enums for "assigned"."""
    if not user_ids:
        return {}
    assigned_status = ApprovalStepStatus.ASSIGNED if step_model is ApprovalWorkflowStep else PeerReviewStatus.ASSIGNED
    result = await db.execute(
        select(step_model.assigned_user_id, func.count())
        .where(step_model.assigned_user_id.in_(user_ids), step_model.status == assigned_status)
        .group_by(step_model.assigned_user_id)
    )
    return {row[0]: row[1] for row in result.all()}


def _pick_least_loaded(candidates: list[User], load: dict[int, int]) -> User:
    return min(candidates, key=lambda u: (load.get(u.id, 0), u.id))


_CANDIDATE_LABEL = {
    ApproverType.USER: lambda role_id, user_id, dept, pool: f"user id {user_id}",
    ApproverType.ROLE: lambda role_id, user_id, dept, pool: f"role id {role_id}",
    ApproverType.DEPARTMENT: lambda role_id, user_id, dept, pool: f"department '{dept}'",
    ApproverType.POOL: lambda role_id, user_id, dept, pool: f"pool {pool}",
}


async def resolve(
    db: AsyncSession,
    *,
    organization_id: int,
    step_model: type,
    approver_type: ApproverType,
    approver_role_id: int | None = None,
    approver_user_id: int | None = None,
    approver_department: str | None = None,
    approver_pool: list[int] | None = None,
) -> ResolutionOutcome:
    """Pure — computes the outcome, mutates nothing. Callers that want it
    persisted use resolve_and_assign_step() below. `step_model` (
    ApprovalWorkflowStep or PeerReviewStep) determines which status enum
    and which table's rows count toward the least-loaded tie-break."""
    candidates = await _eligible_users(organization_id, approver_type, approver_role_id, approver_user_id, approver_department, approver_pool)

    if not candidates:
        label = _CANDIDATE_LABEL[approver_type](approver_role_id, approver_user_id, approver_department, approver_pool)
        return ResolutionOutcome([], None, f"No active user found for {approver_type.value} ({label}) in this organization.")

    if len(candidates) == 1:
        chosen = candidates[0]
        reason = f"Exactly one eligible user for {approver_type.value}."
    else:
        load = await _current_assigned_load(db, step_model, [u.id for u in candidates])
        chosen = _pick_least_loaded(candidates, load)
        reason = (
            f"{len(candidates)} eligible users for {approver_type.value}; "
            f"assigned to the least-loaded (currently {load.get(chosen.id, 0)} open ASSIGNED step(s))."
        )

    return ResolutionOutcome([u.id for u in candidates], chosen.id, reason, chosen.full_name)


async def resolve_and_assign_step(
    db: AsyncSession, step: ApprovalWorkflowStep | PeerReviewStep, workflow: ApprovalWorkflow,
) -> ResolutionOutcome:
    """Resolves + persists (does NOT commit — see module docstring on why
    this stays composable with a future workflow engine's own
    transaction): sets step.assigned_user_id and flips status
    PENDING -> ASSIGNED on success, always writes a durable
    ApprovalWorkflowAuditLog row (STEP_ASSIGNED/PEER_REVIEW_ASSIGNED on
    success, RESOLUTION_FAILED on failure — left PENDING, never silently
    skipped or assigned to a placeholder)."""
    is_peer = isinstance(step, PeerReviewStep)
    step_model = PeerReviewStep if is_peer else ApprovalWorkflowStep

    outcome = await resolve(
        db, organization_id=workflow.organization_id, step_model=step_model, approver_type=step.approver_type,
        approver_role_id=step.approver_role_id, approver_user_id=step.approver_user_id,
        approver_department=step.approver_department, approver_pool=step.approver_pool,
    )

    step_kind = "PEER_REVIEW" if is_peer else "APPROVAL"
    if outcome.resolved:
        step.assigned_user_id = outcome.assigned_user_id
        step.status = PeerReviewStatus.ASSIGNED if is_peer else ApprovalStepStatus.ASSIGNED
        # SLA clock starts now (Phase 7's sla_scheduler_service.py reads
        # these) — re-stamped on escalation too, so a reassigned step
        # gets a fresh window rather than inheriting the elapsed one.
        now = datetime.now(timezone.utc)
        step.assigned_at = now
        step.due_at = now + timedelta(hours=step.sla_hours) if step.sla_hours else None
        event_type = ApprovalWorkflowAuditEventType.PEER_REVIEW_ASSIGNED if is_peer else ApprovalWorkflowAuditEventType.STEP_ASSIGNED
        assignee_label = outcome.assigned_user_name or f"user {outcome.assigned_user_id}"
        message = f"{step_kind} step #{step.step_number} ({step.approver_type.value}) assigned to {assignee_label}. {outcome.reason}"
    else:
        event_type = ApprovalWorkflowAuditEventType.RESOLUTION_FAILED
        message = f"{step_kind} step #{step.step_number} ({step.approver_type.value}) could not be resolved. {outcome.reason}"

    db.add(ApprovalWorkflowAuditLog(
        organization_id=workflow.organization_id, workflow_id=workflow.id, invoice_id=workflow.invoice_id,
        event_type=event_type, message=message,
        details={
            "step_id": step.id, "step_number": step.step_number, "step_kind": step_kind,
            "eligible_user_ids": outcome.eligible_user_ids, "assigned_user_id": outcome.assigned_user_id,
        },
    ))
    return outcome


async def resolve_and_assign_active_steps(db: AsyncSession, workflow: ApprovalWorkflow) -> list[ResolutionOutcome]:
    """Resolves+assigns whichever PENDING step(s) are due RIGHT NOW given
    this workflow's own mode/current-step snapshot — SEQUENTIAL: just the
    step at current_approval_step_number / current_peer_review_step_number;
    PARALLEL: every PENDING step of that type at once. Does NOT decide
    when to advance current_*_step_number as steps complete — that's the
    workflow engine's job (a later phase); this is the primitive it will
    call once per activation. Idempotent: a step already past PENDING
    (already ASSIGNED/decided) is left untouched."""
    from app.models.approval.rules import ApprovalMode  # local import — avoids a hard dependency for callers that only need resolve()/resolve_and_assign_step()

    outcomes: list[ResolutionOutcome] = []

    if workflow.approval_required:
        result = await db.execute(
            select(ApprovalWorkflowStep)
            .where(ApprovalWorkflowStep.workflow_id == workflow.id, ApprovalWorkflowStep.status == ApprovalStepStatus.PENDING)
            .order_by(ApprovalWorkflowStep.step_number)
        )
        pending_steps = list(result.scalars().all())
        if workflow.approval_mode == ApprovalMode.SEQUENTIAL:
            pending_steps = [s for s in pending_steps if s.step_number == workflow.current_approval_step_number]
        for step in pending_steps:
            outcomes.append(await resolve_and_assign_step(db, step, workflow))

    if workflow.peer_review_required:
        result = await db.execute(
            select(PeerReviewStep)
            .where(PeerReviewStep.workflow_id == workflow.id, PeerReviewStep.status == PeerReviewStatus.PENDING)
            .order_by(PeerReviewStep.step_number)
        )
        pending_steps = list(result.scalars().all())
        if workflow.peer_review_mode == ApprovalMode.SEQUENTIAL:
            pending_steps = [s for s in pending_steps if s.step_number == workflow.current_peer_review_step_number]
        for step in pending_steps:
            outcomes.append(await resolve_and_assign_step(db, step, workflow))

    return outcomes
