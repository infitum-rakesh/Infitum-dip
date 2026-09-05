"""Invoice validation-rule router — /api/invoice-rules/... (INV17).

Thin admin surface over the real rule engine (services/invoice/
invoice_validation_service.py, INV6) — every row here is a genuine
ApInvoiceValidationRule that evaluate_invoice() actually interprets on
the very next invoice processed; nothing here is a display-only mock of
the rules.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.invoice_intelligence.rules import ApInvoiceRuleCategory, ApInvoiceRuleSeverity
from app.models.user import User, UserRole
from app.schemas.invoices import (
    ApInvoiceValidationRuleOut,
    ApInvoiceValidationRulePublishRequest,
    ApInvoiceValidationRuleToggleRequest,
)
from app.services.invoice import invoice_validation_service as svc

router = APIRouter(prefix="/invoice-rules", tags=["invoices"])

INVOICE_MODULE = "INVOICE_INTELLIGENCE"


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


@router.get("", response_model=list[ApInvoiceValidationRuleOut])
async def list_rules(
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Seeds the real default rule set on first access (idempotent) so
    there's always something concrete to view/edit, same convenience
    convention GET /invoice-mappings uses."""
    org_id = _effective_org_id(user)
    await svc.seed_default_rules(db, org_id, created_by=user.id)
    rules = await svc.list_rules_for_admin(db, org_id)
    return [ApInvoiceValidationRuleOut.model_validate(r) for r in rules]


@router.put("", response_model=ApInvoiceValidationRuleOut, status_code=status.HTTP_201_CREATED)
async def publish_rule(
    body: ApInvoiceValidationRulePublishRequest,
    user: User = Depends(require_action("EDIT", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    try:
        category = ApInvoiceRuleCategory(body.category)
        severity = ApInvoiceRuleSeverity(body.severity)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    try:
        rule = await svc.publish_rule(
            db, _effective_org_id(user), name=body.name, category=category, severity=severity,
            config=body.config, message=body.message, enabled=body.enabled, created_by=user.id,
        )
    except svc.InvoiceRuleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ApInvoiceValidationRuleOut.model_validate(rule)


@router.post("/{rule_id}/toggle", response_model=ApInvoiceValidationRuleOut)
async def toggle_rule(
    rule_id: int, body: ApInvoiceValidationRuleToggleRequest,
    user: User = Depends(require_action("EDIT", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    try:
        rule = await svc.set_rule_enabled(db, _effective_org_id(user), rule_id, body.enabled)
    except svc.InvoiceRuleError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return ApInvoiceValidationRuleOut.model_validate(rule)
