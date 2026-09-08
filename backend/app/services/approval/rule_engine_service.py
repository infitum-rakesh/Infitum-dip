"""
rule_engine_service.py — the Approval Rule Engine's evaluation core
(Payment Invoice Approval Workflow, Phase 3). Pure rule-matching logic
against the Phase 2 models (models/approval/rules.py) — no
ApprovalWorkflow/instance concept exists yet (Phase 4+), so this module
is deliberately invoice-shape-agnostic: callers build an
ApprovalMatchContext from whatever they have in hand (an ApInvoice today,
something else tomorrow) rather than this module importing
ApInvoice directly.

Same "org row wins, else platform-default fallback" merge convention as
invoice_validation_service.get_current_rules() / ApInvoiceValidationRule:
resolve_candidate_rules() merges by `name`, org-specific row overriding
a same-named platform-default (organization_id IS NULL) row.

Two concerns live here, kept in one file the same way
invoice_validation_service.py keeps its per-category interpreters and
evaluate_invoice() together:

  1. VALIDATION — validate_rule_definition() checks a candidate
     ApprovalRule + its ApprovalRuleCondition/ApprovalRuleStep rows for
     internal consistency (contradictory action/approval_required flags,
     an approver_type missing its required id, a condition operator
     given the wrong value shape, ...) BEFORE it's ever persisted. No
     create/publish/CRUD service exists yet (a later phase) — this is
     the reusable check that service will call, built now so the
     validation rules are pinned down before there's a caller.

  2. EVALUATION — resolve_candidate_rules() + evaluate() + the specificity
     ladder + conflict detection, producing an EvaluationTrace a human
     (or a later Rule Simulation UI, spec's SIMULATION_RUN audit event)
     can actually read: every candidate considered, whether it matched
     and why/why not, and — when more than one rule matches — which one
     won or whether it's a genuine RULE_CONFLICT (spec §5).

Specificity ladder (see ApprovalRule.priority's own docstring in
models/approval/rules.py): a rule's specificity is simply how many
conditions it actually carries (first-class amount/currency/invoice_type
columns that are non-NULL, plus every ApprovalRuleCondition row) — more
conditions means a more specific rule. Among rules that MATCH the same
invoice, higher specificity wins; a tie is broken by organization-specific
beating platform-default; a further tie falls back to the raw `priority`
number (lower wins); a rule that is STILL tied on all three is a genuine
conflict this engine refuses to silently resolve by picking one
arbitrarily — the caller (a future workflow-creation step) must decide
what to do with EvaluationTrace.conflict, never this module.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval.rules import (
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


class ApprovalRuleValidationError(Exception):
    """Raised only by callers that want fail-fast; validate_rule_definition()
    itself always returns the full list of problems rather than raising,
    so a future Rule Builder UI can show every issue at once instead of
    one round-trip per fix."""


# --------------------------------------------------------------------------
# 1. VALIDATION — checked before a rule definition is ever persisted.
# --------------------------------------------------------------------------

def validate_rule_definition(
    rule: ApprovalRule, conditions: list[ApprovalRuleCondition], steps: list[ApprovalRuleStep],
) -> list[str]:
    """Returns a list of human-readable problems (empty = valid). Operates
    on the actual ORM instances a future create/publish service would
    build pre-commit — no separate transient DTO shape to keep in sync
    with the model."""
    errors: list[str] = []

    if not (rule.name or "").strip():
        errors.append("Rule name is required.")

    if rule.priority is not None and rule.priority < 1:
        errors.append("Priority must be a positive integer (lower number = higher priority).")

    if rule.min_amount is not None and rule.min_amount < 0:
        errors.append("min_amount cannot be negative.")
    if rule.max_amount is not None and rule.max_amount < 0:
        errors.append("max_amount cannot be negative.")
    if rule.min_amount is not None and rule.max_amount is not None and rule.min_amount > rule.max_amount:
        errors.append(f"min_amount ({rule.min_amount}) cannot be greater than max_amount ({rule.max_amount}).")

    if rule.currency is not None and (len(rule.currency) != 3 or not rule.currency.isalpha()):
        errors.append(f"currency '{rule.currency}' doesn't look like a 3-letter ISO code.")

    if rule.effective_from is not None and rule.effective_to is not None and rule.effective_from > rule.effective_to:
        errors.append("effective_from cannot be after effective_to.")

    # THEN-clause consistency — action is the plain-language verdict,
    # approval_required/peer_review_required are what the engine actually
    # acts on; these must never contradict each other (see
    # ApprovalRuleAction's own docstring on why both exist).
    if rule.action == ApprovalRuleAction.AUTO_APPROVE and rule.approval_required:
        errors.append("A rule whose action is AUTO_APPROVE cannot also set approval_required=True.")
    if rule.action == ApprovalRuleAction.REQUIRE_APPROVAL and not rule.approval_required:
        errors.append("A rule whose action is REQUIRE_APPROVAL must set approval_required=True.")

    approval_steps = [s for s in steps if s.step_type == ApprovalStepType.APPROVAL]
    peer_review_steps = [s for s in steps if s.step_type == ApprovalStepType.PEER_REVIEW]

    if rule.approval_required:
        if rule.approval_count < 1:
            errors.append("approval_count must be at least 1 when approval_required is True.")
        if not approval_steps:
            errors.append("approval_required is True but no APPROVAL steps are defined.")
        elif rule.approval_count > len(approval_steps):
            errors.append(f"approval_count ({rule.approval_count}) cannot exceed the number of APPROVAL steps ({len(approval_steps)}).")
    elif rule.approval_count != 0:
        errors.append("approval_count must be 0 when approval_required is False.")

    if rule.peer_review_required:
        if rule.peer_review_count < 1:
            errors.append("peer_review_count must be at least 1 when peer_review_required is True.")
        if not peer_review_steps:
            errors.append("peer_review_required is True but no PEER_REVIEW steps are defined.")
        elif rule.peer_review_count > len(peer_review_steps):
            errors.append(f"peer_review_count ({rule.peer_review_count}) cannot exceed the number of PEER_REVIEW steps ({len(peer_review_steps)}).")
    elif rule.peer_review_count != 0:
        errors.append("peer_review_count must be 0 when peer_review_required is False.")

    # step_number must be unique within its own step_type (mirrors the DB's
    # uq_approval_rule_step constraint — checked here too so the caller
    # gets a clean message instead of an IntegrityError at commit time).
    seen: dict[tuple[ApprovalStepType, int], int] = {}
    for s in steps:
        key = (s.step_type, s.step_number)
        if key in seen:
            errors.append(f"Duplicate step_number {s.step_number} for step_type {s.step_type.value}.")
        seen[key] = seen.get(key, 0) + 1
        if s.step_number < 1:
            errors.append(f"step_number must be >= 1 (got {s.step_number} on a {s.step_type.value} step).")

    for s in steps:
        label = f"{s.step_type.value} step #{s.step_number}"
        if s.approver_type == ApproverType.ROLE and s.approver_role_id is None:
            errors.append(f"{label}: approver_type ROLE requires approver_role_id.")
        elif s.approver_type == ApproverType.USER and s.approver_user_id is None:
            errors.append(f"{label}: approver_type USER requires approver_user_id.")
        elif s.approver_type == ApproverType.DEPARTMENT and not (s.approver_department or "").strip():
            errors.append(f"{label}: approver_type DEPARTMENT requires approver_department.")
        elif s.approver_type == ApproverType.POOL and not s.approver_pool:
            errors.append(f"{label}: approver_type POOL requires a non-empty approver_pool.")

        for hours_field in ("sla_hours", "reminder_after_hours", "escalation_after_hours"):
            value = getattr(s, hours_field)
            if value is not None and value < 1:
                errors.append(f"{label}: {hours_field} must be a positive number of hours.")
        if (
            s.reminder_after_hours is not None and s.escalation_after_hours is not None
            and s.escalation_after_hours <= s.reminder_after_hours
        ):
            errors.append(f"{label}: escalation_after_hours must be greater than reminder_after_hours.")

        if s.escalation_approver_type is not None:
            if s.escalation_approver_type not in (ApproverType.ROLE, ApproverType.USER):
                errors.append(f"{label}: escalation only supports ROLE or USER approver types, not {s.escalation_approver_type.value}.")
            elif s.escalation_approver_type == ApproverType.ROLE and s.escalation_role_id is None:
                errors.append(f"{label}: escalation_approver_type ROLE requires escalation_role_id.")
            elif s.escalation_approver_type == ApproverType.USER and s.escalation_user_id is None:
                errors.append(f"{label}: escalation_approver_type USER requires escalation_user_id.")
        elif s.escalation_after_hours is not None:
            # Real gap found while building Phase 7's escalation sweep
            # (sla_scheduler_service.py): a step could set
            # escalation_after_hours with no escalation_approver_type at
            # all — "escalate after 24h" but to nobody — which the sweep
            # can only skip silently. Caught here instead, at
            # rule-authoring time.
            errors.append(f"{label}: escalation_after_hours is set but escalation_approver_type is not — nothing to escalate to.")

    for c in conditions:
        if c.operator in (ApprovalConditionOperator.IN, ApprovalConditionOperator.NOT_IN):
            if not isinstance(c.value, list) or not c.value:
                errors.append(f"Condition {c.field.value} {c.operator.value} requires a non-empty list value.")
        else:
            if isinstance(c.value, (list, dict)):
                errors.append(f"Condition {c.field.value} {c.operator.value} requires a scalar value, not a list/dict.")

    return errors


# --------------------------------------------------------------------------
# 2. EVALUATION — resolve candidate rules, match against a context, trace.
# --------------------------------------------------------------------------

@dataclass
class ApprovalMatchContext:
    """Everything the engine needs to evaluate rules against one invoice
    (or invoice-like) instance — built by the caller from whatever real
    object it has (ApInvoice today). `conditions` keys are
    ApprovalConditionField values (e.g. "SUPPLIER", "DEPARTMENT") mapped
    to the actual string value on the real invoice; a dimension this
    invoice doesn't carry is simply absent from the dict, not set to
    None (absence and "genuinely blank" should read differently in the
    trace)."""
    amount: float | None
    currency: str | None = None
    invoice_type: str | None = None
    as_of: datetime | None = None
    conditions: dict[str, str] = field(default_factory=dict)


@dataclass
class ConditionCheck:
    field: str
    operator: str
    expected: object
    actual: object
    passed: bool
    note: str


@dataclass
class RuleEvaluation:
    rule_id: int
    rule_name: str
    version: int
    organization_id: int | None
    priority: int
    specificity: int
    matched: bool
    checks: list[ConditionCheck]
    reason: str


@dataclass
class EvaluationTrace:
    context: ApprovalMatchContext
    candidates_considered: int
    evaluations: list[RuleEvaluation]
    matched_rules: list[RuleEvaluation]
    winner: RuleEvaluation | None
    conflict: bool
    conflicting_rule_ids: list[int]


async def resolve_candidate_rules(db: AsyncSession, organization_id: int | None) -> list[ApprovalRule]:
    """Every currently-published, ACTIVE rule visible to this
    organization — org-specific rows plus platform-default rows
    (organization_id IS NULL), merged by name exactly like
    invoice_validation_service.get_current_rules(). INACTIVE rules never
    reach live evaluation (they still exist for a Rule admin screen to
    list and re-activate, a later phase's concern)."""
    result = await db.execute(
        select(ApprovalRule).where(
            ApprovalRule.is_current.is_(True),
            ApprovalRule.status == ApprovalRuleStatus.ACTIVE,
            (ApprovalRule.organization_id == organization_id) | (ApprovalRule.organization_id.is_(None)),
        )
    )
    rules = result.scalars().all()
    by_name: dict[str, ApprovalRule] = {}
    for rule in rules:
        existing = by_name.get(rule.name)
        if existing is None or (rule.organization_id is not None and existing.organization_id is None):
            by_name[rule.name] = rule
    return list(by_name.values())


async def get_rule_steps(db: AsyncSession, rule_id: int) -> list[ApprovalRuleStep]:
    """A rule's full THEN-clause, ordered for deterministic SEQUENTIAL
    processing — APPROVAL steps first, then PEER_REVIEW, each ascending
    by step_number. (PARALLEL mode reorders/fans-out at the workflow
    layer, Phase 4+; this is just a stable read order.)"""
    result = await db.execute(
        select(ApprovalRuleStep)
        .where(ApprovalRuleStep.rule_id == rule_id)
        .order_by(ApprovalRuleStep.step_type, ApprovalRuleStep.step_number)
    )
    return list(result.scalars().all())


async def _load_conditions_by_rule(db: AsyncSession, rule_ids: list[int]) -> dict[int, list[ApprovalRuleCondition]]:
    if not rule_ids:
        return {}
    result = await db.execute(select(ApprovalRuleCondition).where(ApprovalRuleCondition.rule_id.in_(rule_ids)))
    grouped: dict[int, list[ApprovalRuleCondition]] = defaultdict(list)
    for c in result.scalars().all():
        grouped[c.rule_id].append(c)
    return grouped


def _check_first_class(field_name: str, operator: str, expected, actual, passed_fn) -> ConditionCheck | None:
    if expected is None:
        return None
    if actual is None:
        return ConditionCheck(field_name, operator, expected, actual, False, f"{field_name}: no value supplied on the invoice to compare against {expected}.")
    passed = passed_fn(actual, expected)
    note = f"{field_name}: {actual} {operator} {expected} -> {'pass' if passed else 'fail'}."
    return ConditionCheck(field_name, operator, expected, actual, passed, note)


def _check_condition_row(condition: ApprovalRuleCondition, context: ApprovalMatchContext) -> ConditionCheck:
    field_name = condition.field.value
    actual = context.conditions.get(field_name)
    if actual is None:
        return ConditionCheck(field_name, condition.operator.value, condition.value, None, False, f"{field_name}: no value supplied in context — cannot evaluate.")

    op = condition.operator
    if op == ApprovalConditionOperator.EQ:
        passed = actual == condition.value
    elif op == ApprovalConditionOperator.NEQ:
        passed = actual != condition.value
    elif op == ApprovalConditionOperator.IN:
        passed = actual in (condition.value or [])
    elif op == ApprovalConditionOperator.NOT_IN:
        passed = actual not in (condition.value or [])
    else:  # pragma: no cover — exhaustive over ApprovalConditionOperator
        passed = False
    note = f"{field_name}: {actual!r} {op.value} {condition.value!r} -> {'pass' if passed else 'fail'}."
    return ConditionCheck(field_name, op.value, condition.value, actual, passed, note)


def _evaluate_rule(rule: ApprovalRule, conditions: list[ApprovalRuleCondition], context: ApprovalMatchContext) -> RuleEvaluation:
    checks: list[ConditionCheck] = []

    if rule.min_amount is not None:
        c = _check_first_class("min_amount", ">=", rule.min_amount, context.amount, lambda a, e: a >= e)
        if c:
            checks.append(c)
    if rule.max_amount is not None:
        c = _check_first_class("max_amount", "<=", rule.max_amount, context.amount, lambda a, e: a <= e)
        if c:
            checks.append(c)
    if rule.currency is not None:
        actual_currency = context.currency.upper() if context.currency else None
        c = _check_first_class("currency", "==", rule.currency.upper(), actual_currency, lambda a, e: a == e)
        if c:
            checks.append(c)
    if rule.invoice_type is not None:
        actual_type = context.invoice_type.upper() if context.invoice_type else None
        c = _check_first_class("invoice_type", "==", rule.invoice_type.upper(), actual_type, lambda a, e: a == e)
        if c:
            checks.append(c)

    as_of = context.as_of or datetime.now(timezone.utc)
    if rule.effective_from is not None and as_of < rule.effective_from:
        checks.append(ConditionCheck("effective_from", ">=", rule.effective_from, as_of, False, f"effective_from: rule not yet effective (effective {rule.effective_from}, evaluated at {as_of})."))
    if rule.effective_to is not None and as_of > rule.effective_to:
        checks.append(ConditionCheck("effective_to", "<=", rule.effective_to, as_of, False, f"effective_to: rule has expired (expired {rule.effective_to}, evaluated at {as_of})."))

    for condition in conditions:
        checks.append(_check_condition_row(condition, context))

    matched = all(c.passed for c in checks)
    specificity = len(checks)
    if matched:
        reason = "Matches all conditions." if checks else "Matches unconditionally (no conditions defined — a generic fallback rule)."
    else:
        reason = "; ".join(c.note for c in checks if not c.passed)

    return RuleEvaluation(
        rule_id=rule.id, rule_name=rule.name, version=rule.version, organization_id=rule.organization_id,
        priority=rule.priority, specificity=specificity, matched=matched, checks=checks, reason=reason,
    )


async def evaluate(db: AsyncSession, organization_id: int | None, context: ApprovalMatchContext) -> EvaluationTrace:
    """The engine's single entry point: resolves every candidate rule
    visible to this org, matches each against `context`, applies the
    specificity ladder to pick a winner among the matches, and returns
    the full trace — every candidate, matched or not, and why."""
    if context.as_of is None:
        context = replace(context, as_of=datetime.now(timezone.utc))

    candidates = await resolve_candidate_rules(db, organization_id)
    conditions_by_rule = await _load_conditions_by_rule(db, [r.id for r in candidates])

    evaluations = [_evaluate_rule(rule, conditions_by_rule.get(rule.id, []), context) for rule in candidates]
    evaluations.sort(key=lambda e: e.rule_name)

    matched = [e for e in evaluations if e.matched]
    # Specificity ladder: more conditions wins; tie -> org-specific beats
    # platform-default; tie -> lower priority number wins; a rule that
    # ties on ALL THREE of another matched rule is a genuine conflict
    # (spec §5 RULE_CONFLICT) never resolved by picking one arbitrarily.
    matched.sort(key=lambda e: (-e.specificity, 0 if e.organization_id is not None else 1, e.priority))

    winner: RuleEvaluation | None = None
    conflict = False
    conflicting_ids: list[int] = []
    if matched:
        top = matched[0]
        tied = [
            e for e in matched
            if e.specificity == top.specificity
            and (e.organization_id is not None) == (top.organization_id is not None)
            and e.priority == top.priority
        ]
        if len(tied) > 1:
            conflict = True
            conflicting_ids = [e.rule_id for e in tied]
        else:
            winner = top

    return EvaluationTrace(
        context=context, candidates_considered=len(candidates), evaluations=evaluations,
        matched_rules=matched, winner=winner, conflict=conflict, conflicting_rule_ids=conflicting_ids,
    )
