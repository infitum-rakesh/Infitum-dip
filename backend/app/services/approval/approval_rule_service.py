"""
approval_rule_service.py — Approval Rule Engine Phase 8: the CRUD/
versioning layer api/approvals.py's rule routes sit on top of. Phases
2-7 built the model (rules.py), the evaluation/validation logic
(rule_engine_service.py), approver resolution, notification, and SLA
scheduling — but nothing yet actually PERSISTED a rule an admin
authored. This module is that missing piece.

Same "never mutate a published version in place, publish a new row
instead" convention as ApInvoiceValidationRule/ColumnMappingVersion
(see rules.py's own module docstring): publish_rule() always creates a
new version row (1 for a brand-new name, N+1 for an existing one),
flips the prior version's is_current off, and never edits a published
row's fields directly. Every publish runs rule_engine_service.
validate_rule_definition() against the transient (not-yet-added-to-
session) ORM objects FIRST — a rule with real validation errors is never
even flushed to the database, let alone activated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval.rules import (
    ApprovalConditionField,
    ApprovalConditionOperator,
    ApprovalEntityType,
    ApprovalMode,
    ApprovalRule,
    ApprovalRuleAction,
    ApprovalRuleAuditEventType,
    ApprovalRuleAuditLog,
    ApprovalRuleCondition,
    ApprovalRuleStatus,
    ApprovalRuleStep,
    ApprovalStepType,
    ApproverType,
)
from app.services.approval import rule_engine_service as engine


class ApprovalRuleServiceError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class ConditionInput:
    field: ApprovalConditionField
    operator: ApprovalConditionOperator
    value: object


@dataclass
class StepInput:
    step_type: ApprovalStepType
    step_number: int
    approver_type: ApproverType
    approver_role_id: int | None = None
    approver_user_id: int | None = None
    approver_department: str | None = None
    approver_pool: list[int] | None = None
    label: str | None = None
    sla_hours: int | None = None
    reminder_after_hours: int | None = None
    escalation_after_hours: int | None = None
    escalation_approver_type: ApproverType | None = None
    escalation_role_id: int | None = None
    escalation_user_id: int | None = None


@dataclass
class RuleInput:
    name: str
    entity_type: ApprovalEntityType = ApprovalEntityType.INVOICE
    description: str | None = None
    priority: int = 100
    status: ApprovalRuleStatus = ApprovalRuleStatus.ACTIVE
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None
    invoice_type: str | None = None
    action: ApprovalRuleAction = ApprovalRuleAction.REQUIRE_APPROVAL
    approval_required: bool = True
    approval_count: int = 1
    approval_mode: ApprovalMode = ApprovalMode.SEQUENTIAL
    peer_review_required: bool = False
    peer_review_count: int = 0
    peer_review_mode: ApprovalMode = ApprovalMode.SEQUENTIAL
    default_sla_hours: int | None = None
    conditions: list[ConditionInput] = None
    steps: list[StepInput] = None

    def __post_init__(self):
        if self.conditions is None:
            self.conditions = []
        if self.steps is None:
            self.steps = []


async def list_rules_for_admin(
    db: AsyncSession, organization_id: int | None, entity_type: ApprovalEntityType = ApprovalEntityType.INVOICE,
) -> list[ApprovalRule]:
    """Every currently-published rule visible to this organization AND
    scoped to entity_type — includes INACTIVE rows too (an admin needs to
    see and re-activate a rule, not just have it vanish), same convention
    as invoice_validation_service.list_rules_for_admin(). Never used by
    the live evaluation path (rule_engine_service.resolve_candidate_rules()
    filters status itself)."""
    result = await db.execute(
        select(ApprovalRule).where(
            ApprovalRule.is_current.is_(True),
            ApprovalRule.entity_type == entity_type,
            (ApprovalRule.organization_id == organization_id) | (ApprovalRule.organization_id.is_(None)),
        )
    )
    rules = result.scalars().all()
    by_name: dict[str, ApprovalRule] = {}
    for rule in rules:
        existing = by_name.get(rule.name)
        if existing is None or (rule.organization_id is not None and existing.organization_id is None):
            by_name[rule.name] = rule
    return sorted(by_name.values(), key=lambda r: (r.priority, r.name))


async def get_rule(db: AsyncSession, organization_id: int | None, rule_id: int) -> ApprovalRule | None:
    rule = await db.get(ApprovalRule, rule_id)
    if rule is None:
        return None
    if rule.organization_id is not None and rule.organization_id != organization_id:
        return None
    return rule


async def get_rule_children(db: AsyncSession, rule_id: int) -> tuple[list[ApprovalRuleCondition], list[ApprovalRuleStep]]:
    conditions = (await db.execute(select(ApprovalRuleCondition).where(ApprovalRuleCondition.rule_id == rule_id))).scalars().all()
    steps = await engine.get_rule_steps(db, rule_id)
    return list(conditions), list(steps)


def _build_transient(organization_id: int | None, version: int, payload: RuleInput) -> tuple[ApprovalRule, list[ApprovalRuleCondition], list[ApprovalRuleStep]]:
    rule = ApprovalRule(
        organization_id=organization_id, entity_type=payload.entity_type, name=payload.name, description=payload.description,
        version=version, is_current=True, status=payload.status, priority=payload.priority,
        effective_from=payload.effective_from, effective_to=payload.effective_to,
        min_amount=payload.min_amount, max_amount=payload.max_amount, currency=payload.currency,
        invoice_type=payload.invoice_type, action=payload.action, approval_required=payload.approval_required,
        approval_count=payload.approval_count, approval_mode=payload.approval_mode,
        peer_review_required=payload.peer_review_required, peer_review_count=payload.peer_review_count,
        peer_review_mode=payload.peer_review_mode, default_sla_hours=payload.default_sla_hours,
    )
    conditions = [
        ApprovalRuleCondition(organization_id=organization_id, field=c.field, operator=c.operator, value=c.value)
        for c in payload.conditions
    ]
    steps = [
        ApprovalRuleStep(
            organization_id=organization_id, step_type=s.step_type, step_number=s.step_number,
            approver_type=s.approver_type, approver_role_id=s.approver_role_id, approver_user_id=s.approver_user_id,
            approver_department=s.approver_department, approver_pool=s.approver_pool, label=s.label,
            sla_hours=s.sla_hours, reminder_after_hours=s.reminder_after_hours,
            escalation_after_hours=s.escalation_after_hours, escalation_approver_type=s.escalation_approver_type,
            escalation_role_id=s.escalation_role_id, escalation_user_id=s.escalation_user_id,
        )
        for s in payload.steps
    ]
    return rule, conditions, steps


async def publish_rule(db: AsyncSession, organization_id: int | None, payload: RuleInput, actor_id: int | None) -> ApprovalRule:
    """Publishes a new version of one named rule. Validates BEFORE
    touching the session — raises ApprovalRuleServiceError(errors) with
    every problem found (rule_engine_service.validate_rule_definition()
    doesn't stop at the first one) rather than persisting a partially-bad
    definition."""
    if not payload.name.strip():
        raise ApprovalRuleServiceError(["Rule name is required."])

    existing_result = await db.execute(
        select(ApprovalRule).where(
            ApprovalRule.organization_id == organization_id, ApprovalRule.name == payload.name,
            ApprovalRule.entity_type == payload.entity_type, ApprovalRule.is_current.is_(True),
        )
    )
    existing = existing_result.scalar_one_or_none()
    next_version = (existing.version + 1) if existing else 1

    rule, conditions, steps = _build_transient(organization_id, next_version, payload)
    rule.created_by = actor_id

    errors = engine.validate_rule_definition(rule, conditions, steps)
    if errors:
        raise ApprovalRuleServiceError(errors)

    if existing:
        existing.is_current = False

    db.add(rule)
    await db.flush()
    for c in conditions:
        c.rule_id = rule.id
        db.add(c)
    for s in steps:
        s.rule_id = rule.id
        db.add(s)

    event_type = ApprovalRuleAuditEventType.VERSION_PUBLISHED if existing else ApprovalRuleAuditEventType.CREATED
    db.add(ApprovalRuleAuditLog(
        organization_id=organization_id, rule_name=rule.name, rule_version_id=rule.id, event_type=event_type,
        message=f"{'Published version' if existing else 'Created'} {rule.name} v{rule.version}.", actor_id=actor_id,
        details={"version": rule.version, "condition_count": len(conditions), "step_count": len(steps)},
    ))
    await db.commit()
    await db.refresh(rule)
    return rule


async def set_rule_status(db: AsyncSession, organization_id: int | None, rule_id: int, status: ApprovalRuleStatus, actor_id: int | None) -> ApprovalRule:
    """Cheap ACTIVE/INACTIVE toggle — does NOT bump the version (nothing
    about the rule's actual matching logic changed). Toggling a
    platform-default rule (organization_id IS NULL) creates an org-level
    override at version 1 instead, same convention as invoice_validation_
    service.set_rule_enabled()."""
    rule = await get_rule(db, organization_id, rule_id)
    if rule is None or not rule.is_current:
        raise ApprovalRuleServiceError(["Rule not found."])

    if rule.organization_id is None and organization_id is not None:
        override = ApprovalRule(
            organization_id=organization_id, entity_type=rule.entity_type, name=rule.name, description=rule.description, version=1, is_current=True,
            status=status, priority=rule.priority, effective_from=rule.effective_from, effective_to=rule.effective_to,
            min_amount=rule.min_amount, max_amount=rule.max_amount, currency=rule.currency, invoice_type=rule.invoice_type,
            action=rule.action, approval_required=rule.approval_required, approval_count=rule.approval_count,
            approval_mode=rule.approval_mode, peer_review_required=rule.peer_review_required,
            peer_review_count=rule.peer_review_count, peer_review_mode=rule.peer_review_mode,
            default_sla_hours=rule.default_sla_hours, created_by=actor_id,
        )
        db.add(override)
        await db.flush()
        conditions, steps = await get_rule_children(db, rule.id)
        for c in conditions:
            db.add(ApprovalRuleCondition(organization_id=organization_id, rule_id=override.id, field=c.field, operator=c.operator, value=c.value))
        for s in steps:
            db.add(ApprovalRuleStep(
                organization_id=organization_id, rule_id=override.id, step_type=s.step_type, step_number=s.step_number,
                approver_type=s.approver_type, approver_role_id=s.approver_role_id, approver_user_id=s.approver_user_id,
                approver_department=s.approver_department, approver_pool=s.approver_pool, label=s.label,
                sla_hours=s.sla_hours, reminder_after_hours=s.reminder_after_hours, escalation_after_hours=s.escalation_after_hours,
                escalation_approver_type=s.escalation_approver_type, escalation_role_id=s.escalation_role_id,
                escalation_user_id=s.escalation_user_id,
            ))
        target = override
    else:
        rule.status = status
        target = rule

    event_type = ApprovalRuleAuditEventType.ACTIVATED if status == ApprovalRuleStatus.ACTIVE else ApprovalRuleAuditEventType.DEACTIVATED
    db.add(ApprovalRuleAuditLog(
        organization_id=organization_id, rule_name=target.name, rule_version_id=target.id, event_type=event_type,
        message=f"{target.name} set to {status.value}.", actor_id=actor_id,
    ))
    await db.commit()
    await db.refresh(target)
    return target


async def simulate_rule(
    db: AsyncSession, organization_id: int | None, context: engine.ApprovalMatchContext, actor_id: int | None,
    entity_type: ApprovalEntityType = ApprovalEntityType.INVOICE,
) -> engine.EvaluationTrace:
    """Dry run — evaluates every currently-active rule (scoped to
    entity_type) against a hypothetical context, changes nothing, and
    logs a SIMULATION_RUN audit event against the winning (or
    conflicting) rule(s) so a "why did/didn't this match" question is
    answerable later without reproducing the exact inputs."""
    trace = await engine.evaluate(db, organization_id, context, entity_type)
    target_names = [e.rule_name for e in (trace.matched_rules or [])] or ["<no match>"]
    db.add(ApprovalRuleAuditLog(
        organization_id=organization_id, rule_name=", ".join(target_names)[:150],
        event_type=ApprovalRuleAuditEventType.SIMULATION_RUN,
        message=f"Simulation against amount={context.amount} currency={context.currency} invoice_type={context.invoice_type} -> "
                f"{'CONFLICT' if trace.conflict else (trace.winner.rule_name if trace.winner else 'no match')}.",
        actor_id=actor_id,
        details={"conflict": trace.conflict, "winner_rule_id": trace.winner.rule_id if trace.winner else None},
    ))
    await db.commit()
    return trace
