"""
invoice_validation_service.py — the real business-validation rule engine
(Technical Design categories: SCHEMA/ARITHMETIC/REFERENCE/PO/DUPLICATE/
POLICY/ERP). Interprets versioned ApInvoiceValidationRule rows against
one materialized ApInvoice (+ its ApInvoiceLine rows — see
invoice_processing_service.py, INV5) and persists one
ApInvoiceValidationResult per rule execution: the evidence trail behind
invoice_decision_service.py's (INV7) final PASS/REVIEW/REJECT routing,
not just an aggregate score.

Per-category interpreters (config shape lives on
ApInvoiceValidationRule.config, documented per-category below):

  SCHEMA     config: {"required_fields": ["invoice_number", "vendor_name", ...]}
             Every named ApInvoice column is genuinely non-null. FAIL
             lists exactly which are missing — never a generic message.

  ARITHMETIC config: {"lhs": "subtotal + tax - discount + freight",
                       "rhs": "total", "tolerance": 0.01}
             lhs/rhs are evaluated via a whitelisted arithmetic-only
             expression interpreter (Python's ast module restricted to
             Name/Constant/BinOp/UnaryOp — no Compare, no calls, no
             attribute access, nothing else) — never a bare eval() of
             rule-authored text. A variable name not present on the
             invoice (e.g. no subtotal was ever extracted) makes the
             rule genuinely unevaluable -> WARNING ("insufficient data"),
             not a fabricated PASS or an overreaching FAIL.

  REFERENCE  config: {} (no per-rule config needed — checks the
             vendor resolved during materialization, ApInvoice.vendor_id,
             a real ApInvoiceVendor row — see invoice_processing_service.
             _resolve_vendor()). No live ERP call happens here; this is
             platform-side reference data only.

  PO         config: {"pattern": "^[A-Za-z0-9\\-]{3,30}$"}
             When a purchase_order value is present, checks it against
             the configured regex. Genuinely skipped (PASS, not FAIL)
             when no PO was extracted at all — a PO's very presence being
             required is itself a POLICY-category concern (see below),
             not this rule's job. No live PO/GRN three-way match exists
             in this build (no ERP tenant to query) — disclosed, not
             silently faked as "matched".

  DUPLICATE  config: {} — reads ApInvoice.duplicate_check_result, already
             computed once at materialization time (invoice_processing_
             service._check_duplicate()) rather than recomputed here —
             one source of truth, not two independent implementations
             that could disagree.

  POLICY     config: {"min_pct": 0, "max_pct": 30, "numerator": "tax",
                       "denominator": "subtotal"}
             A configurable percentage-tolerance band (e.g. "tax should
             be 0-30% of subtotal") — same safe-expression path as
             ARITHMETIC underneath. WARNING (not FAIL) when either input
             is missing.

  ERP        Genuinely evaluable piece only: an ERP connection is
             configured for this organization (Connection.category ==
             AI... no — APPLICATION/ORACLE_ERP_CLOUD, see
             connectors/application/oracle_erp.py). Deeper ERP-side
             checks (vendor recognized BY the ERP, GL account valid,
             PO genuinely open in the ERP) need a real Oracle Fusion call
             this build has no live tenant to verify against — NOT
             evaluated here. No ERP-category rule is seeded by default
             for exactly this reason; an organization that later
             configures a real ERP connection can add one via the Rules
             UI (INV17), evaluated honestly against just what's checkable
             today.

Default seed rules (seed_default_rules()) are created idempotently per
organization on first use — a real, editable starting set (INV17's Rules
UI lets an org edit/version them later per the "never mutate a published
version in place" convention on ApInvoiceValidationRule itself), not a
hardcoded, unchangeable policy.
"""
from __future__ import annotations

import ast
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceLine, ApInvoiceStatus
from app.models.invoice_intelligence.rules import (
    ApInvoiceRuleCategory,
    ApInvoiceRuleResult,
    ApInvoiceRuleSeverity,
    ApInvoiceValidationResult,
    ApInvoiceValidationRule,
)


class ExpressionError(Exception):
    """A rule's lhs/rhs/expression genuinely can't be evaluated — a
    missing variable, not a code bug. Callers turn this into a WARNING
    validation result, never a crash and never a fabricated number."""


_ALLOWED_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}
_ALLOWED_UNARYOPS = {ast.UAdd: lambda a: +a, ast.USub: lambda a: -a}


def _safe_eval(expr: str, variables: dict[str, float | None]) -> float:
    """Whitelisted arithmetic-only expression evaluator — Name/Constant/
    BinOp(+-*/)/UnaryOp nodes ONLY. No Call, no Attribute, no Compare, no
    Subscript, nothing that could execute arbitrary code even if a rule's
    config were somehow attacker-controlled. Raises ExpressionError for
    any variable name not in `variables` or whose value is None (missing
    data) rather than silently treating it as zero."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Invalid expression syntax: {expr}") from exc

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise ExpressionError(f"Unknown field '{node.id}' in expression.")
            value = variables[node.id]
            if value is None:
                raise ExpressionError(f"Field '{node.id}' has no value to evaluate.")
            return float(value)
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            return _ALLOWED_BINOPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
            return _ALLOWED_UNARYOPS[type(node.op)](_eval(node.operand))
        raise ExpressionError(f"Unsupported expression element in: {expr}")

    return _eval(tree)


def _invoice_variables(invoice: ApInvoice) -> dict[str, float | None]:
    return {
        "subtotal": invoice.subtotal, "tax": invoice.tax, "discount": invoice.discount,
        "freight": invoice.freight, "total": invoice.total,
    }


def _eval_schema(rule: ApInvoiceValidationRule, invoice: ApInvoice, lines: list[ApInvoiceLine]) -> tuple[ApInvoiceRuleResult, str, dict]:
    required = rule.config.get("required_fields") or []
    missing = [f for f in required if getattr(invoice, f, None) in (None, "")]
    if missing:
        return ApInvoiceRuleResult.FAIL, f"{rule.message} — missing: {', '.join(missing)}", {"missing_fields": missing}
    return ApInvoiceRuleResult.PASS, "All required fields present.", {}


def _eval_arithmetic(rule: ApInvoiceValidationRule, invoice: ApInvoice, lines: list[ApInvoiceLine]) -> tuple[ApInvoiceRuleResult, str, dict]:
    cfg = rule.config
    lhs_expr, rhs_expr, tolerance = cfg.get("lhs"), cfg.get("rhs"), float(cfg.get("tolerance", 0.01))
    if not lhs_expr or not rhs_expr:
        return ApInvoiceRuleResult.WARNING, "Rule misconfigured — no lhs/rhs expression.", {}
    variables = _invoice_variables(invoice)
    try:
        lhs_val = _safe_eval(lhs_expr, variables)
        rhs_val = _safe_eval(rhs_expr, variables)
    except ExpressionError as exc:
        return ApInvoiceRuleResult.WARNING, f"Cannot evaluate — {exc}", {"lhs": lhs_expr, "rhs": rhs_expr}
    diff = round(lhs_val - rhs_val, 4)
    if abs(diff) <= tolerance:
        return ApInvoiceRuleResult.PASS, f"{lhs_expr} matches {rhs_expr} (within {tolerance}).", {"lhs_value": lhs_val, "rhs_value": rhs_val, "diff": diff}
    return (
        ApInvoiceRuleResult.FAIL,
        f"{rule.message} — {lhs_expr} = {lhs_val:.2f}, {rhs_expr} = {rhs_val:.2f} (diff {diff:.2f}).",
        {"lhs_value": lhs_val, "rhs_value": rhs_val, "diff": diff},
    )


def _eval_reference(rule: ApInvoiceValidationRule, invoice: ApInvoice, lines: list[ApInvoiceLine]) -> tuple[ApInvoiceRuleResult, str, dict]:
    if not invoice.vendor_name:
        return ApInvoiceRuleResult.WARNING, "No vendor name extracted — cannot verify against reference data.", {}
    if invoice.vendor_id is None:
        return ApInvoiceRuleResult.FAIL, f"{rule.message} — vendor '{invoice.vendor_name}' not found in reference data.", {"vendor_name": invoice.vendor_name}
    return ApInvoiceRuleResult.PASS, f"Vendor '{invoice.vendor_name}' recognized.", {"vendor_id": invoice.vendor_id}


def _eval_po(rule: ApInvoiceValidationRule, invoice: ApInvoice, lines: list[ApInvoiceLine]) -> tuple[ApInvoiceRuleResult, str, dict]:
    if not invoice.purchase_order:
        return ApInvoiceRuleResult.PASS, "No PO number present — nothing to validate.", {}
    pattern = rule.config.get("pattern")
    if not pattern:
        return ApInvoiceRuleResult.WARNING, "Rule misconfigured — no pattern.", {}
    if re.match(pattern, invoice.purchase_order):
        return ApInvoiceRuleResult.PASS, f"PO '{invoice.purchase_order}' matches the expected format.", {}
    return ApInvoiceRuleResult.FAIL, f"{rule.message} — '{invoice.purchase_order}' doesn't match the expected format.", {"purchase_order": invoice.purchase_order, "pattern": pattern}


def _eval_duplicate(rule: ApInvoiceValidationRule, invoice: ApInvoice, lines: list[ApInvoiceLine]) -> tuple[ApInvoiceRuleResult, str, dict]:
    check = invoice.duplicate_check_result or "NONE"
    if check == "BLOCKED":
        return ApInvoiceRuleResult.FAIL, f"{rule.message} — matches an existing invoice (id {invoice.duplicate_of_invoice_id}).", {"duplicate_of_invoice_id": invoice.duplicate_of_invoice_id}
    if check == "WARN":
        return ApInvoiceRuleResult.WARNING, f"Possible duplicate of invoice id {invoice.duplicate_of_invoice_id}.", {"duplicate_of_invoice_id": invoice.duplicate_of_invoice_id}
    return ApInvoiceRuleResult.PASS, "No duplicate detected.", {}


def _eval_policy(rule: ApInvoiceValidationRule, invoice: ApInvoice, lines: list[ApInvoiceLine]) -> tuple[ApInvoiceRuleResult, str, dict]:
    cfg = rule.config
    numerator, denominator = cfg.get("numerator"), cfg.get("denominator")
    min_pct, max_pct = cfg.get("min_pct"), cfg.get("max_pct")
    if not numerator or not denominator or min_pct is None or max_pct is None:
        return ApInvoiceRuleResult.WARNING, "Rule misconfigured — missing numerator/denominator/min_pct/max_pct.", {}
    variables = _invoice_variables(invoice)
    try:
        num_val = _safe_eval(numerator, variables)
        den_val = _safe_eval(denominator, variables)
    except ExpressionError as exc:
        return ApInvoiceRuleResult.WARNING, f"Cannot evaluate — {exc}", {}
    if den_val == 0:
        return ApInvoiceRuleResult.WARNING, f"Cannot evaluate — {denominator} is zero.", {}
    pct = round((num_val / den_val) * 100, 2)
    if min_pct <= pct <= max_pct:
        return ApInvoiceRuleResult.PASS, f"{numerator}/{denominator} = {pct}% (within {min_pct}-{max_pct}%).", {"pct": pct}
    return ApInvoiceRuleResult.FAIL, f"{rule.message} — {numerator}/{denominator} = {pct}% (expected {min_pct}-{max_pct}%).", {"pct": pct}


def _eval_erp(rule: ApInvoiceValidationRule, invoice: ApInvoice, lines: list[ApInvoiceLine]) -> tuple[ApInvoiceRuleResult, str, dict]:
    if invoice.erp_connection_id is None:
        return ApInvoiceRuleResult.WARNING, "No ERP connection configured for this organization yet.", {}
    return ApInvoiceRuleResult.PASS, "An ERP connection is configured.", {"erp_connection_id": invoice.erp_connection_id}


_INTERPRETERS = {
    ApInvoiceRuleCategory.SCHEMA: _eval_schema,
    ApInvoiceRuleCategory.ARITHMETIC: _eval_arithmetic,
    ApInvoiceRuleCategory.REFERENCE: _eval_reference,
    ApInvoiceRuleCategory.PO: _eval_po,
    ApInvoiceRuleCategory.DUPLICATE: _eval_duplicate,
    ApInvoiceRuleCategory.POLICY: _eval_policy,
    ApInvoiceRuleCategory.ERP: _eval_erp,
}


async def get_current_rules(db: AsyncSession, organization_id: int | None) -> list[ApInvoiceValidationRule]:
    """Every enabled, currently-published rule visible to this
    organization — org-specific rows plus platform-default rows
    (organization_id IS NULL), same "org row wins, else platform
    fallback" convention used elsewhere in this codebase (e.g.
    OrganizationSettings defaults)."""
    result = await db.execute(
        select(ApInvoiceValidationRule).where(
            ApInvoiceValidationRule.enabled.is_(True),
            ApInvoiceValidationRule.is_current.is_(True),
            (ApInvoiceValidationRule.organization_id == organization_id) | (ApInvoiceValidationRule.organization_id.is_(None)),
        )
    )
    rules = result.scalars().all()
    # Org-specific rule overrides a same-named platform default, never both running.
    by_name: dict[str, ApInvoiceValidationRule] = {}
    for rule in rules:
        existing = by_name.get(rule.name)
        if existing is None or (rule.organization_id is not None and existing.organization_id is None):
            by_name[rule.name] = rule
    return list(by_name.values())


DEFAULT_RULES: list[dict] = [
    {"name": "Required Header Fields", "category": ApInvoiceRuleCategory.SCHEMA, "severity": ApInvoiceRuleSeverity.EXCEPTION,
     "config": {"required_fields": ["invoice_number", "vendor_name", "total", "invoice_date"]},
     "message": "One or more required header fields are missing."},
    {"name": "Header Total Reconciles", "category": ApInvoiceRuleCategory.ARITHMETIC, "severity": ApInvoiceRuleSeverity.EXCEPTION,
     "config": {"lhs": "subtotal + tax - discount + freight", "rhs": "total", "tolerance": 0.01},
     "message": "Subtotal + tax - discount + freight does not reconcile to the stated total."},
    {"name": "Vendor Recognized", "category": ApInvoiceRuleCategory.REFERENCE, "severity": ApInvoiceRuleSeverity.WARNING,
     "config": {}, "message": "Vendor is not yet known reference data for this organization."},
    {"name": "PO Number Format", "category": ApInvoiceRuleCategory.PO, "severity": ApInvoiceRuleSeverity.WARNING,
     "config": {"pattern": r"^[A-Za-z0-9\-]{3,30}$"}, "message": "PO number doesn't match the expected format."},
    {"name": "Duplicate Invoice Check", "category": ApInvoiceRuleCategory.DUPLICATE, "severity": ApInvoiceRuleSeverity.EXCEPTION,
     "config": {}, "message": "This invoice appears to be a duplicate."},
    {"name": "Tax Rate Within Expected Range", "category": ApInvoiceRuleCategory.POLICY, "severity": ApInvoiceRuleSeverity.WARNING,
     "config": {"numerator": "tax", "denominator": "subtotal", "min_pct": 0, "max_pct": 30},
     "message": "Tax as a percentage of subtotal is outside the expected range."},
    # No ERP-category rule seeded by default — see module docstring.
]


class InvoiceRuleError(Exception):
    pass


async def list_rules_for_admin(db: AsyncSession, organization_id: int | None) -> list[ApInvoiceValidationRule]:
    """Every currently-published rule visible to this organization —
    unlike get_current_rules() above, this deliberately includes disabled
    rows too (an admin needs to see and re-enable a rule, not just have
    it vanish) and is never itself used by the live evaluate_invoice()
    path. Same org-row-wins-over-platform-default merge."""
    result = await db.execute(
        select(ApInvoiceValidationRule).where(
            ApInvoiceValidationRule.is_current.is_(True),
            (ApInvoiceValidationRule.organization_id == organization_id) | (ApInvoiceValidationRule.organization_id.is_(None)),
        )
    )
    rules = result.scalars().all()
    by_name: dict[str, ApInvoiceValidationRule] = {}
    for rule in rules:
        existing = by_name.get(rule.name)
        if existing is None or (rule.organization_id is not None and existing.organization_id is None):
            by_name[rule.name] = rule
    return sorted(by_name.values(), key=lambda r: (r.category.value, r.name))


async def publish_rule(
    db: AsyncSession, organization_id: int | None, *,
    name: str, category: ApInvoiceRuleCategory, severity: ApInvoiceRuleSeverity,
    config: dict, message: str, enabled: bool, created_by: int | None,
) -> ApInvoiceValidationRule:
    """Publishes a new version of one named rule — never mutates a
    published row in place (same reproducibility guarantee
    ApInvoice.rule_version depends on: a past decision's recorded
    rule_version stays traceable to the exact config that was actually
    current when it ran). Editing a platform-default rule (organization_id
    IS NULL) for the first time creates a fresh org-level override at
    version 1 rather than mutating the shared platform row — the org's
    override then wins in get_current_rules()'s merge from then on."""
    if not name.strip():
        raise InvoiceRuleError("Rule name is required.")
    if category == ApInvoiceRuleCategory.ARITHMETIC and not (config.get("lhs") and config.get("rhs")):
        raise InvoiceRuleError("An ARITHMETIC rule needs both 'lhs' and 'rhs' in its config.")
    if category == ApInvoiceRuleCategory.SCHEMA and not config.get("required_fields"):
        raise InvoiceRuleError("A SCHEMA rule needs a non-empty 'required_fields' list in its config.")

    existing_result = await db.execute(
        select(ApInvoiceValidationRule).where(
            ApInvoiceValidationRule.organization_id == organization_id,
            ApInvoiceValidationRule.name == name,
            ApInvoiceValidationRule.is_current.is_(True),
        )
    )
    existing = existing_result.scalar_one_or_none()
    next_version = (existing.version + 1) if existing else 1
    if existing:
        existing.is_current = False

    rule = ApInvoiceValidationRule(
        organization_id=organization_id, name=name, category=category, severity=severity,
        config=config, message=message, enabled=enabled, version=next_version, is_current=True, created_by=created_by,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


async def set_rule_enabled(db: AsyncSession, organization_id: int | None, rule_id: int, enabled: bool) -> ApInvoiceValidationRule:
    """Cheap enable/disable toggle — does NOT bump the version (no
    config/message/category/severity actually changed, so there's nothing
    new for a re-evaluated invoice's rule_version to need to distinguish)."""
    result = await db.execute(
        select(ApInvoiceValidationRule).where(
            ApInvoiceValidationRule.id == rule_id, ApInvoiceValidationRule.is_current.is_(True),
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise InvoiceRuleError("Rule not found.")
    if rule.organization_id is not None and rule.organization_id != organization_id:
        raise InvoiceRuleError("Rule not found.")
    if rule.organization_id is None:
        # Toggling a platform-default rule creates an org-level override
        # at version 1, same as publish_rule() — never flips the shared
        # platform row's own enabled flag for every other org.
        rule = ApInvoiceValidationRule(
            organization_id=organization_id, name=rule.name, category=rule.category, severity=rule.severity,
            config=rule.config, message=rule.message, enabled=enabled, version=1, is_current=True, created_by=None,
        )
        db.add(rule)
    else:
        rule.enabled = enabled
    await db.commit()
    await db.refresh(rule)
    return rule


async def seed_default_rules(db: AsyncSession, organization_id: int | None, created_by: int | None = None) -> list[ApInvoiceValidationRule]:
    """Idempotent: creates the default rule set for this organization
    only if it has no validation rules of its own yet (an org that has
    already customized/deleted defaults never gets them silently
    reinstated). Version 1, is_current=True for every seeded row."""
    existing = await db.execute(select(ApInvoiceValidationRule.id).where(ApInvoiceValidationRule.organization_id == organization_id).limit(1))
    if existing.scalar_one_or_none() is not None:
        return []
    created = []
    for spec in DEFAULT_RULES:
        rule = ApInvoiceValidationRule(
            organization_id=organization_id, name=spec["name"], category=spec["category"], severity=spec["severity"],
            config=spec["config"], message=spec["message"], enabled=True, version=1, is_current=True, created_by=created_by,
        )
        db.add(rule)
        created.append(rule)
    await db.commit()
    for rule in created:
        await db.refresh(rule)
    return created


async def evaluate_invoice(db: AsyncSession, invoice: ApInvoice) -> list[ApInvoiceValidationResult]:
    """Runs every current, enabled rule visible to invoice.organization_id
    against this invoice and its lines, replacing any prior validation
    results for this invoice wholesale (same "recompute and replace"
    convention as invoice_processing_service.py's line-item
    materialization — no stable per-run identity to diff against)."""
    invoice.status = ApInvoiceStatus.VALIDATING

    lines_result = await db.execute(select(ApInvoiceLine).where(ApInvoiceLine.invoice_id == invoice.id))
    lines = lines_result.scalars().all()

    rules = await get_current_rules(db, invoice.organization_id)

    existing_results = await db.execute(select(ApInvoiceValidationResult).where(ApInvoiceValidationResult.invoice_id == invoice.id))
    for row in existing_results.scalars().all():
        await db.delete(row)
    await db.flush()

    results: list[ApInvoiceValidationResult] = []
    for rule in rules:
        interpreter = _INTERPRETERS.get(rule.category)
        if interpreter is None:
            continue
        outcome, message, details = interpreter(rule, invoice, lines)
        # severity is embedded into details rather than requiring every
        # consumer (e.g. invoice_decision_service.py, INV7) to re-fetch
        # ApInvoiceValidationRule just to know how to weigh a FAIL — the
        # rule itself remains the one source of truth for severity, this
        # is just carrying that value along with its own result row.
        row = ApInvoiceValidationResult(
            organization_id=invoice.organization_id, invoice_id=invoice.id, rule_id=rule.id, rule_version=rule.version,
            category=rule.category, result=outcome, message=message, details={**details, "severity": rule.severity.value},
        )
        db.add(row)
        results.append(row)

    invoice.rule_version = max((r.version for r in rules), default=None)
    await db.commit()
    for row in results:
        await db.refresh(row)
    return results
