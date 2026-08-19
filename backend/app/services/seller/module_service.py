"""
Seller Portal -> Purchased Modules (Phase 4) — "one of the MOST IMPORTANT
Seller screens" per the spec. Two surfaces:

  - Module catalog (list_catalog): every real, licensable module in this
    platform (the same 12 keys core/deps.require_action()'s `module`
    param and frontend utils/permissions.js's MODULES already gate on —
    grouped into the spec's category scheme). The spec's own catalog
    example lists a few illustrative items (Image Scanner, Connections,
    Pipelines, Schedules) that don't correspond to anything actually
    license-gated in this codebase; those are deliberately omitted here
    rather than faked — see Seller Portal memory / Phase 4 summary.
  - Per-customer module configuration (get_customer_modules/set_module):
    real CustomerModule rows with enable/disable, license type, limits,
    expiry. Enforces the spec's "a module cannot be enabled if: subscription
    is expired / customer is suspended / Seller doesn't license the
    module" rule, plus module dependencies, server-side — not just hidden
    in the UI.

Integration note: set_module() mutates Organization.licensed_modules
directly (adds/removes the one module) so this is REAL gating, not
cosmetic — core/deps.require_action() and the frontend's evaluateAccess()
both read that field. It does NOT touch services/seller/subscription_service.py,
which still sets licensed_modules = plan.modules wholesale on every
subscription change/renewal — so a Seller's per-module override here can
be superseded by a later subscription sync. Disclosed limitation, not
silently papered over: full reconciliation (CustomerModule as the single
source of truth Organization.licensed_modules is *derived* from) is
Phase 10 ("Integrate RBAC + licensing + organization isolation") scope.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer_module import CustomerModule
from app.models.organization import LicenseStatus, Organization
from app.models.seller_audit_log import record_seller_event
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User

# category -> [module keys], matching the spec's grouping applied to the
# 12 modules that actually exist as licensable units in this codebase
# (see frontend/src/utils/constants.js MODULES / backend ALL_MODULES).
MODULE_CATALOG: dict[str, dict] = {
    "DATA_CATALOG":         {"label": "Data Catalog",          "category": "DATA"},
    "DOCUMENT_INTELLIGENCE":{"label": "Document Intelligence", "category": "DOCUMENT INTELLIGENCE"},
    "OCR":                  {"label": "OCR",                   "category": "DOCUMENT INTELLIGENCE"},
    "AI_EXTRACTION":        {"label": "AI Extraction",         "category": "DOCUMENT INTELLIGENCE"},
    "TRANSFORMATION":       {"label": "Transformation",        "category": "TRANSFORMATION"},
    "SQL_WORKSPACE":        {"label": "SQL Workspace",         "category": "TRANSFORMATION"},
    "VALIDATION":           {"label": "Validation",            "category": "QUALITY"},
    "RECONCILIATION":       {"label": "Reconciliation",        "category": "QUALITY"},
    "PIPELINE_SCHEDULER":   {"label": "Pipelines & Schedules",  "category": "OPERATIONS"},
    "INCIDENTS":            {"label": "Incidents",             "category": "OPERATIONS"},
    "LINEAGE":              {"label": "Data Lineage",          "category": "GOVERNANCE"},
    "OBSERVABILITY":        {"label": "Observability",         "category": "GOVERNANCE"},
}
CATALOG_VERSION = "1.0.0"  # matches main.py's FastAPI(version=...) — one platform release, no per-module versioning exists

# Real dependency edges (spec section 3 examples, adapted to the module
# keys that actually exist — "Review Queue requires Documents" has no
# corresponding entry since both already live inside DOCUMENT_INTELLIGENCE
# as one licensable unit in this codebase, so the dependency is inherently satisfied).
MODULE_DEPENDENCIES: dict[str, list[str]] = {
    "AI_EXTRACTION": ["DOCUMENT_INTELLIGENCE"],
    "OCR": ["DOCUMENT_INTELLIGENCE"],
    "RECONCILIATION": ["VALIDATION"],
}


class ModuleServiceError(Exception):
    pass


async def list_catalog(db: AsyncSession) -> list[dict]:
    orgs = (await db.execute(select(Organization))).scalars().all()
    items = []
    for module, meta in MODULE_CATALOG.items():
        customers = sum(1 for o in orgs if module in (o.licensed_modules or []))
        items.append({
            "module": module, "label": meta["label"], "category": meta["category"],
            "version": CATALOG_VERSION, "available": True, "customers": customers,
            "license_type": "SUBSCRIPTION", "status": "ACTIVE",
            "dependencies": MODULE_DEPENDENCIES.get(module, []),
        })
    return items


async def _latest_subscription(db: AsyncSession, organization_id: int) -> Subscription | None:
    result = await db.execute(
        select(Subscription).where(Subscription.organization_id == organization_id)
        .order_by(Subscription.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def get_customer_modules(db: AsyncSession, organization_id: int) -> list[dict]:
    org = await db.get(Organization, organization_id)
    if org is None:
        raise ModuleServiceError("Customer not found.")

    sub = await _latest_subscription(db, organization_id)
    plan_modules = set()
    if sub and sub.plan_id:
        from app.models.plan import Plan
        plan = await db.get(Plan, sub.plan_id)
        if plan:
            plan_modules = set(plan.modules or [])

    rows_result = await db.execute(select(CustomerModule).where(CustomerModule.organization_id == organization_id))
    rows_by_module = {r.module: r for r in rows_result.scalars().all()}
    enabled_now = set(org.licensed_modules or [])

    items = []
    for module, meta in MODULE_CATALOG.items():
        row = rows_by_module.get(module)
        deps = MODULE_DEPENDENCIES.get(module, [])
        items.append({
            "module": module, "label": meta["label"], "category": meta["category"],
            "purchased": module in plan_modules or row is not None,
            "enabled": module in enabled_now,
            "license_type": row.license_type if row else "SUBSCRIPTION",
            "max_users": row.max_users if row else None,
            "monthly_credits": row.monthly_credits if row else None,
            "max_documents": row.max_documents if row else None,
            "start_date": row.start_date if row else None,
            "expiry_date": row.expiry_date if row else None,
            "dependencies": deps,
            "dependencies_met": all(d in enabled_now for d in deps),
        })
    return items


def _block_reason(org: Organization, sub: Subscription | None, module: str) -> str | None:
    if module not in MODULE_CATALOG:
        return f"'{module}' is not a module the Seller licenses on this platform."
    if org.license_status == LicenseStatus.SUSPENDED or not org.is_active:
        return "Customer is suspended — cannot enable modules."
    if org.license_status == LicenseStatus.EXPIRED:
        return "Customer's license has expired — cannot enable modules."
    if sub is not None and sub.status in (SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED):
        return "Customer's subscription is expired or cancelled — cannot enable modules."
    return None


async def set_module(db: AsyncSession, *, organization_id: int, module: str, body, actor: User, ip_address: str | None = None) -> dict:
    org = await db.get(Organization, organization_id)
    if org is None:
        raise ModuleServiceError("Customer not found.")

    sub = await _latest_subscription(db, organization_id)

    if body.enabled:
        reason = _block_reason(org, sub, module)
        if reason:
            raise ModuleServiceError(reason)
        deps = MODULE_DEPENDENCIES.get(module, [])
        enabled_now = set(org.licensed_modules or [])
        missing = [d for d in deps if d not in enabled_now]
        if missing:
            missing_labels = ", ".join(MODULE_CATALOG.get(d, {}).get("label", d) for d in missing)
            raise ModuleServiceError(f"Cannot enable {MODULE_CATALOG.get(module, {}).get('label', module)} — missing required module(s): {missing_labels}.")

    result = await db.execute(select(CustomerModule).where(CustomerModule.organization_id == organization_id, CustomerModule.module == module))
    row = result.scalar_one_or_none()
    before = {"enabled": row.enabled if row else False}

    if row is None:
        row = CustomerModule(organization_id=organization_id, module=module, created_by=actor.id)
        db.add(row)

    # A bare `is not None` check can't tell "field omitted" from "field
    # explicitly sent as null to clear it back to unlimited" — both look
    # like None on body.max_users etc. Use Pydantic's model_fields_set so
    # an explicit `{"max_users": null}` genuinely clears the limit instead
    # of being silently ignored (bug caught live during Phase 5 testing).
    provided = body.model_fields_set
    row.enabled = body.enabled
    if "license_type" in provided:
        row.license_type = body.license_type
    if "max_users" in provided:
        row.max_users = body.max_users
    if "monthly_credits" in provided:
        row.monthly_credits = body.monthly_credits
    if "max_documents" in provided:
        row.max_documents = body.max_documents
    if "expiry_date" in provided:
        row.expiry_date = body.expiry_date
    if row.start_date is None and body.enabled:
        row.start_date = datetime.now(timezone.utc)

    # The real gating list — see module docstring for the subscription-sync caveat.
    licensed = list(org.licensed_modules or [])
    if body.enabled and module not in licensed:
        licensed.append(module)
    elif not body.enabled and module in licensed:
        licensed.remove(module)
    org.licensed_modules = licensed

    await db.commit()
    await db.refresh(row)

    action = "Module Enabled" if body.enabled else "Module Disabled"
    if before["enabled"] == body.enabled:
        action = "Quota Changed"  # same on/off state — this call only touched limits/expiry
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=organization_id, action=action,
        module=module, resource=MODULE_CATALOG.get(module, {}).get("label", module), resource_id=module,
        before_value=before, after_value={"enabled": row.enabled, "license_type": row.license_type, "max_users": row.max_users, "expiry_date": row.expiry_date.isoformat() if row.expiry_date else None},
        ip_address=ip_address,
    )

    return {
        "module": module, "enabled": row.enabled, "license_type": row.license_type,
        "max_users": row.max_users, "monthly_credits": row.monthly_credits, "max_documents": row.max_documents,
        "start_date": row.start_date, "expiry_date": row.expiry_date,
    }
