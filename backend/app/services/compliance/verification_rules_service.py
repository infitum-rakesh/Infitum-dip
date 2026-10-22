"""
verification_rules_service.py — Verification Rules Engine (Phase Group
4, IV19). Resolves which VerificationRule applies to a case's
(business_process, subject_type) and evaluates whether a case satisfies
it — mirrors app/services/approval/rule_engine_service.py's resolve/
evaluate shape (specificity ladder, EvaluationTrace/RuleEvaluation
dataclasses, conflict detection) applied to this module's simpler,
two-condition domain.

Three distinct consumers:
 - simulate_rule() / evaluate(): "if a case with these attributes existed,
   which rule would apply, and why" — used by the Rules admin page's
   Simulate panel, no VerificationCase required.
 - evaluate_case_compliance(): "does this REAL case's actual results
   satisfy its applicable rule" — used by api/compliance.py's _case_out()
   so every case response carries a `compliance` field for free, and by
   verification_orchestrator.py to compute expires_at / the rule-aware
   case.overall_status (see derive_case_overall_status's new params).
 - resolve_applicable_rule(): the single lookup both of the above build
   on — also called directly by verification_job_service.py to find
   which check_types a scheduled refresh advisory should consider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.india_verification import OverallVerificationStatus, VerificationCase, VerificationCheckType, VerificationResult
from app.models.india_verification_rules import VerificationRule, VerificationRuleStatus

_SATISFIED_STATUSES = {OverallVerificationStatus.VERIFIED, OverallVerificationStatus.VERIFIED_WITH_WARNINGS}


class VerificationRuleServiceError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class VerificationRuleMatchContext:
    business_process: str
    subject_type: str | None = None
    as_of: datetime | None = None


@dataclass
class ConditionCheck:
    field: str
    operator: str
    expected: object
    actual: object
    passed: bool
    note: str = ""


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
    context: VerificationRuleMatchContext
    candidates_considered: int
    evaluations: list[RuleEvaluation]
    matched_rules: list[RuleEvaluation] = field(default_factory=list)
    winner: RuleEvaluation | None = None
    conflict: bool = False
    conflicting_rule_ids: list[int] = field(default_factory=list)


@dataclass
class ComplianceResult:
    """`rule is None` means "no VerificationRule applies to this case's
    business_process" — `compliant` is then also None (not True), since
    "nothing to check against" is a different, non-fabricated claim from
    "checked and it passed". Never silently reads as compliant."""
    rule: VerificationRule | None
    required_check_types: list[str]
    satisfied_check_types: list[str]
    missing_check_types: list[str]
    compliant: bool | None
    reason: str


async def resolve_candidate_rules(
    db: AsyncSession, organization_id: int | None, business_process: str, *, as_of: datetime | None = None,
) -> list[VerificationRule]:
    as_of = as_of or datetime.now(timezone.utc)
    stmt = select(VerificationRule).where(
        VerificationRule.is_current.is_(True),
        VerificationRule.status == VerificationRuleStatus.ACTIVE,
        VerificationRule.business_process == business_process,
    )
    if organization_id is not None:
        stmt = stmt.where((VerificationRule.organization_id == organization_id) | (VerificationRule.organization_id.is_(None)))
    else:
        stmt = stmt.where(VerificationRule.organization_id.is_(None))
    rows = (await db.execute(stmt)).scalars().all()

    def _in_window(r: VerificationRule) -> bool:
        if r.effective_from and as_of < r.effective_from:
            return False
        if r.effective_to and as_of > r.effective_to:
            return False
        return True

    candidates = [r for r in rows if _in_window(r)]
    # Merge by name — an org-specific row overrides a platform row of the
    # same name, same convention as ApprovalRule's resolver.
    by_name: dict[str, VerificationRule] = {}
    for r in candidates:
        existing = by_name.get(r.name)
        if existing is None or (existing.organization_id is None and r.organization_id is not None):
            by_name[r.name] = r
    return list(by_name.values())


def _evaluate_one(rule: VerificationRule, context: VerificationRuleMatchContext) -> RuleEvaluation:
    checks = [ConditionCheck(
        field="business_process", operator="eq", expected=rule.business_process, actual=context.business_process,
        passed=rule.business_process == context.business_process,
    )]
    specificity = 0
    if rule.subject_type is not None:
        passed = rule.subject_type == context.subject_type
        checks.append(ConditionCheck(field="subject_type", operator="eq", expected=rule.subject_type, actual=context.subject_type, passed=passed, note="" if passed else "subject_type does not match"))
        specificity += 1
    matched = all(c.passed for c in checks)
    reason = "All conditions matched." if matched else "; ".join(c.note or f"{c.field} did not match" for c in checks if not c.passed)
    return RuleEvaluation(
        rule_id=rule.id, rule_name=rule.name, version=rule.version, organization_id=rule.organization_id,
        priority=rule.priority, specificity=specificity, matched=matched, checks=checks, reason=reason,
    )


async def evaluate(db: AsyncSession, organization_id: int | None, context: VerificationRuleMatchContext) -> EvaluationTrace:
    candidates = await resolve_candidate_rules(db, organization_id, context.business_process, as_of=context.as_of)
    evaluations = [_evaluate_one(r, context) for r in candidates]
    matched = [e for e in evaluations if e.matched]
    if not matched:
        return EvaluationTrace(context=context, candidates_considered=len(candidates), evaluations=evaluations)

    matched.sort(key=lambda e: (-e.specificity, 0 if e.organization_id is not None else 1, e.priority))
    top = matched[0]
    ties = [e for e in matched if (e.specificity, 0 if e.organization_id is not None else 1, e.priority) == (top.specificity, 0 if top.organization_id is not None else 1, top.priority)]
    conflict = len(ties) > 1
    return EvaluationTrace(
        context=context, candidates_considered=len(candidates), evaluations=evaluations, matched_rules=matched,
        winner=None if conflict else top, conflict=conflict, conflicting_rule_ids=[e.rule_id for e in ties] if conflict else [],
    )


async def resolve_applicable_rule(
    db: AsyncSession, organization_id: int | None, *, business_process: str | None, subject_type: str | None,
) -> VerificationRule | None:
    """The single lookup every other consumer in this module builds on.
    Returns None (not an error) when business_process is unset or no
    rule matches — a case/check with no applicable rule is a normal,
    valid state (Phase 1-3's entire behavior before rules existed)."""
    if not business_process:
        return None
    trace = await evaluate(db, organization_id, VerificationRuleMatchContext(business_process=business_process, subject_type=subject_type))
    if trace.conflict or trace.winner is None:
        return None
    return await db.get(VerificationRule, trace.winner.rule_id)


async def evaluate_case_compliance(db: AsyncSession, case: VerificationCase) -> ComplianceResult:
    rule = await resolve_applicable_rule(db, case.organization_id, business_process=case.business_process, subject_type=case.subject_type)
    if rule is None:
        reason = "No business process set on this case." if not case.business_process else f"No active verification rule matches business process '{case.business_process}'."
        return ComplianceResult(rule=None, required_check_types=[], satisfied_check_types=[], missing_check_types=[], compliant=None, reason=reason)

    results = (await db.execute(select(VerificationResult).where(VerificationResult.case_id == case.id))).scalars().all()
    satisfied_by_type = {r.check_type.value: r.overall_status in _SATISFIED_STATUSES for r in results}
    required = list(rule.required_check_types or [])
    satisfied = [t for t in required if satisfied_by_type.get(t)]
    missing = [t for t in required if t not in satisfied]
    compliant = len(missing) == 0
    reason = "All required checks are verified." if compliant else f"Missing or unverified: {', '.join(missing)}."
    return ComplianceResult(rule=rule, required_check_types=required, satisfied_check_types=satisfied, missing_check_types=missing, compliant=compliant, reason=reason)


# ── CRUD / admin ─────────────────────────────────────────────────────

_KNOWN_CHECK_TYPES = {t.value for t in VerificationCheckType}


def validate_rule_definition(*, business_process: str, required_check_types: list[str], freshness_days: int | None, priority: int) -> list[str]:
    errors: list[str] = []
    if not business_process or not business_process.strip():
        errors.append("business_process is required.")
    if not required_check_types:
        errors.append("At least one required check type is needed.")
    unknown = [t for t in (required_check_types or []) if t not in _KNOWN_CHECK_TYPES]
    if unknown:
        errors.append(f"Unknown check type(s): {', '.join(unknown)}. Must be one of: {', '.join(sorted(_KNOWN_CHECK_TYPES))}.")
    if freshness_days is not None and freshness_days < 1:
        errors.append("freshness_days must be at least 1 when set.")
    if priority < 0:
        errors.append("priority must be non-negative.")
    return errors


async def list_rules_for_admin(db: AsyncSession, organization_id: int | None) -> list[VerificationRule]:
    stmt = select(VerificationRule).where(VerificationRule.is_current.is_(True))
    if organization_id is not None:
        stmt = stmt.where((VerificationRule.organization_id == organization_id) | (VerificationRule.organization_id.is_(None)))
    else:
        stmt = stmt.where(VerificationRule.organization_id.is_(None))
    rows = (await db.execute(stmt.order_by(VerificationRule.business_process, VerificationRule.priority))).scalars().all()
    return list(rows)


async def get_rule(db: AsyncSession, organization_id: int | None, rule_id: int) -> VerificationRule | None:
    rule = await db.get(VerificationRule, rule_id)
    if rule is None:
        return None
    if rule.organization_id is not None and organization_id is not None and rule.organization_id != organization_id:
        return None
    if rule.organization_id is not None and organization_id is None:
        return None
    return rule


async def publish_rule(
    db: AsyncSession, organization_id: int | None, *, name: str, description: str | None, business_process: str,
    subject_type: str | None, required_check_types: list[str], freshness_days: int | None, priority: int,
    effective_from: datetime | None, effective_to: datetime | None, actor_id: int | None,
) -> VerificationRule:
    errors = validate_rule_definition(business_process=business_process, required_check_types=required_check_types, freshness_days=freshness_days, priority=priority)
    if errors:
        raise VerificationRuleServiceError(errors)

    prior = (await db.execute(
        select(VerificationRule).where(VerificationRule.organization_id == organization_id, VerificationRule.name == name, VerificationRule.is_current.is_(True))
    )).scalar_one_or_none()
    next_version = (prior.version + 1) if prior else 1
    if prior is not None:
        prior.is_current = False

    rule = VerificationRule(
        organization_id=organization_id, name=name, description=description, version=next_version, is_current=True,
        status=VerificationRuleStatus.ACTIVE, priority=priority, business_process=business_process, subject_type=subject_type,
        required_check_types=required_check_types, freshness_days=freshness_days,
        effective_from=effective_from, effective_to=effective_to, created_by=actor_id,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


async def set_rule_status(db: AsyncSession, organization_id: int | None, rule_id: int, new_status: VerificationRuleStatus) -> VerificationRule:
    rule = await get_rule(db, organization_id, rule_id)
    if rule is None:
        raise VerificationRuleServiceError([f"Rule {rule_id} not found."])
    rule.status = new_status
    await db.commit()
    await db.refresh(rule)
    return rule


async def simulate_rule(db: AsyncSession, organization_id: int | None, context: VerificationRuleMatchContext) -> EvaluationTrace:
    return await evaluate(db, organization_id, context)
