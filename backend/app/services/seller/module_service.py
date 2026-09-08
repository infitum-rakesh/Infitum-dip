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

from app.models.billing.invoice import Invoice, InvoiceStatus
from app.models.customer_module import CustomerModule
from app.models.organization import LicenseStatus, Organization
from app.models.seller_audit_log import record_seller_event
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User

# Invoice statuses that mean "still owed" — used to surface a
# pending_invoice_id alongside a SUSPENDED (unpaid) module grant so the
# frontend can point straight at the invoice to pay, instead of leaving
# the seller staring at a toggle that silently did nothing.
_UNPAID_INVOICE_STATUSES = (InvoiceStatus.PAYMENT_PENDING, InvoiceStatus.ISSUED, InvoiceStatus.OVERDUE, InvoiceStatus.PARTIALLY_PAID)

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
    "ANOMALY_DETECTION":    {"label": "Anomaly Detection",     "category": "QUALITY"},
    "INVOICE_INTELLIGENCE":{"label": "Invoice Intelligence",  "category": "DOCUMENT INTELLIGENCE"},
    "PAYMENT_APPROVAL":     {"label": "Payment Approvals",      "category": "OPERATIONS"},
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
    # Invoice Intelligence is built entirely on top of the existing
    # Document/OCR/AI-extraction pipeline (see services/invoice/ module
    # docstrings) — an org with no Document Intelligence has nothing for
    # it to process.
    "INVOICE_INTELLIGENCE": ["DOCUMENT_INTELLIGENCE"],
    # The approval rule engine matches on invoice fields (amount/currency/
    # vendor/...) — an org with no Invoice Intelligence has no invoices
    # for an approval rule to ever evaluate against.
    "PAYMENT_APPROVAL": ["INVOICE_INTELLIGENCE"],
}


class ModuleServiceError(Exception):
    pass


# ── Module price catalog (Settings -> Module Prices) ────────────────
# What ad-hoc Purchased-Modules grants (set_module(), via add_module())
# and "Custom" plan subscriptions (subscription_service.create_subscription
# with plan_id=None) are billed at — see models/billing/module_price.py.

async def list_module_prices(db: AsyncSession) -> list[dict]:
    from app.models.billing.module_price import ModulePrice
    result = await db.execute(select(ModulePrice))
    rows_by_module = {r.module: r for r in result.scalars().all()}
    return [
        {
            "module": module, "label": meta["label"], "category": meta["category"],
            "price": rows_by_module[module].price if module in rows_by_module else 0.0,
            "currency": rows_by_module[module].currency if module in rows_by_module else "USD",
        }
        for module, meta in MODULE_CATALOG.items()
    ]


async def set_module_price(db: AsyncSession, *, module: str, price: float, currency: str, actor: User) -> dict:
    from app.models.billing.module_price import ModulePrice
    if module not in MODULE_CATALOG:
        raise ModuleServiceError(f"'{module}' is not a module the Seller licenses on this platform.")
    if price < 0:
        raise ModuleServiceError("Price cannot be negative.")
    result = await db.execute(select(ModulePrice).where(ModulePrice.module == module))
    row = result.scalar_one_or_none()
    before = {"price": row.price if row else None, "currency": row.currency if row else None}
    if row is None:
        row = ModulePrice(module=module, price=price, currency=currency, updated_by=actor.id)
        db.add(row)
    else:
        row.price = price
        row.currency = currency
        row.updated_by = actor.id
    await db.commit()
    await db.refresh(row)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Module Price Updated",
        module=module, resource=MODULE_CATALOG.get(module, {}).get("label", module), resource_id=module,
        before_value=before, after_value={"price": row.price, "currency": row.currency},
    )
    return {"module": row.module, "price": row.price, "currency": row.currency}


async def price_for_modules(db: AsyncSession, modules: list[str]) -> float:
    """Sums the current catalog price for each module — what a "Custom"
    plan subscription's amount (subscription_service.create_subscription)
    and a fresh add_module() grant default to when no explicit amount is
    given. Missing entries (a module never priced) contribute 0.0 rather
    than failing the whole computation — plan_seed.py seeds every catalog
    module at startup so this should be rare in practice."""
    from app.models.billing.module_price import ModulePrice
    if not modules:
        return 0.0
    result = await db.execute(select(ModulePrice).where(ModulePrice.module.in_(modules)))
    price_by_module = {r.module: r.price for r in result.scalars().all()}
    return round(sum(price_by_module.get(m, 0.0) for m in modules), 2)


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


async def _active_subscription_ids(db: AsyncSession, organization_id: int) -> set[int]:
    """An org can hold several simultaneous Subscription rows (the main
    plan subscription plus any number of add_module()-created single-
    module ones — see subscription_service.add_module()). "The latest
    subscription" stops being a reliable stand-in for "the subscription
    that grants module X" the moment a second one exists, so anything
    that needs to find/mutate the CustomerModule row actually granting a
    module scopes to this set instead — the same set
    access_service.has_module_access() itself evaluates against."""
    from app.services.billing.access_service import access_service
    active_subs = await access_service.get_active_subscriptions(db, organization_id)
    return {s.id for s in active_subs}


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

    # Prefer the row tied to a CURRENTLY ACTIVE subscription (what actually
    # grants access — see set_module()'s comment); fall back to any row for
    # a module so a quota/limit set under a since-replaced subscription
    # still shows for reference rather than looking un-purchased.
    active_sub_ids = await _active_subscription_ids(db, organization_id)
    rows_result = await db.execute(select(CustomerModule).where(CustomerModule.organization_id == organization_id))
    all_rows = list(rows_result.scalars().all())
    rows_by_module: dict[str, CustomerModule] = {}
    for r in all_rows:
        if r.module not in rows_by_module or r.subscription_id in active_sub_ids:
            rows_by_module[r.module] = r
    enabled_now = set(org.licensed_modules or [])

    # Batch-fetch, per subscription, the most recent still-owed invoice —
    # lets a SUSPENDED (unpaid) row point straight at what needs paying
    # instead of the toggle just silently staying off with no explanation
    # (found live: enabling a module gave zero feedback, which is exactly
    # what prompted repeated clicks and duplicate subscriptions).
    sub_ids = {r.subscription_id for r in rows_by_module.values() if r.subscription_id is not None}
    pending_invoice_by_sub: dict[int, int] = {}
    if sub_ids:
        inv_result = await db.execute(
            select(Invoice.subscription_id, Invoice.id).where(
                Invoice.subscription_id.in_(sub_ids), Invoice.status.in_(_UNPAID_INVOICE_STATUSES),
            ).order_by(Invoice.created_at.desc())
        )
        for sub_id, inv_id in inv_result.all():
            pending_invoice_by_sub.setdefault(sub_id, inv_id)

    items = []
    for module, meta in MODULE_CATALOG.items():
        row = rows_by_module.get(module)
        deps = MODULE_DEPENDENCIES.get(module, [])
        items.append({
            "module": module, "label": meta["label"], "category": meta["category"],
            "purchased": module in plan_modules or row is not None,
            "enabled": module in enabled_now,
            "status": row.status if row else None,
            "license_type": row.license_type if row else "SUBSCRIPTION",
            "max_users": row.max_users if row else None,
            "monthly_credits": row.monthly_credits if row else None,
            "max_documents": row.max_documents if row else None,
            "start_date": row.start_date if row else None,
            "expiry_date": row.expiry_date if row else None,
            "dependencies": deps,
            "dependencies_met": all(d in enabled_now for d in deps),
            # Only surface as "pending payment" when the grant itself isn't
            # ACTIVE — an already-active row can still have an old, since-
            # superseded unpaid invoice sitting on the same subscription
            # (e.g. a renewal invoice from before it was paid off) and that
            # must never make an already-working module look blocked.
            # Found live: OCR/AI Extraction/Transformation (all genuinely
            # ACTIVE) showed a false "Pending Payment" badge without this.
            "pending_invoice_id": pending_invoice_by_sub.get(row.subscription_id) if row and row.status != "ACTIVE" else None,
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


def _row_out(row: CustomerModule, pending_invoice_id: int | None = None) -> dict:
    return {
        "module": row.module, "enabled": row.enabled, "status": row.status, "license_type": row.license_type,
        "max_users": row.max_users, "monthly_credits": row.monthly_credits, "max_documents": row.max_documents,
        "start_date": row.start_date, "expiry_date": row.expiry_date,
        "pending_invoice_id": pending_invoice_id,
    }


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

        from app.services.billing.access_service import access_service
        already_granted = (await access_service.has_module_access(db, organization_id, module)).granted
        if not already_granted:
            # Granting a module the org doesn't already have real access to
            # is a real purchase — delegate to subscription_service.
            # add_module() (spec section 38's "Add Module"), the path that
            # actually creates a Subscription + a real, priced Invoice for
            # it. Before this fix, this endpoint just flipped
            # CustomerModule.enabled directly and never billed anything —
            # found live: enabling a module from Purchased Modules produced
            # no invoice at all. Trial-mode orgs still get it free and
            # immediate (add_module()'s own gating); everyone else gets a
            # real PAYMENT_PENDING invoice and no access until it's paid.
            from app.services.seller import subscription_service as sub_svc
            try:
                new_sub = await sub_svc.add_module(db, organization_id=organization_id, module=module, actor=actor)
            except sub_svc.SubscriptionServiceError as exc:
                # add_module() raises its own error type (e.g. the
                # duplicate-purchase guard above) — the API route only
                # catches ModuleServiceError, so without this conversion a
                # legitimate double-click surfaced as a raw 500 instead of
                # a clean message. Found live while fixing the duplicate-
                # subscription bug this guard exists to prevent.
                raise ModuleServiceError(str(exc)) from exc
            row_result = await db.execute(
                select(CustomerModule).where(CustomerModule.subscription_id == new_sub.id, CustomerModule.module == module)
            )
            row = row_result.scalar_one()
            pending_invoice_id = None
            if row.status != "ACTIVE":
                # Non-trial org: add_module() left this row SUSPENDED
                # pending payment — surface the invoice it just created so
                # the caller can show "awaiting payment" + a direct link
                # instead of the toggle looking like it did nothing.
                inv_result = await db.execute(
                    select(Invoice.id).where(Invoice.subscription_id == new_sub.id).order_by(Invoice.created_at.desc()).limit(1)
                )
                pending_invoice_id = inv_result.scalar_one_or_none()
            await record_seller_event(
                db, seller_user_id=actor.id, customer_id=organization_id, action="Module Enabled",
                module=module, resource=MODULE_CATALOG.get(module, {}).get("label", module), resource_id=module,
                before_value={"enabled": False},
                after_value={"enabled": row.enabled, "license_type": row.license_type, "billed_via_subscription_id": new_sub.id},
                ip_address=ip_address,
            )
            return _row_out(row, pending_invoice_id=pending_invoice_id)
        # else: already has real access (e.g. re-saving quota fields on an
        # already-active module) — fall through to the edit path below,
        # scoped to whichever currently-active subscription's row grants it.

    # Disable, or edit quota fields on an already-granted module: operate
    # on the row tied to a CURRENTLY ACTIVE subscription (see
    # _active_subscription_ids()'s docstring — "the latest subscription"
    # is not reliable once an org can hold several at once). Falls back to
    # the org's single latest subscription for the create-if-missing case
    # (disabling a module that was never actually granted is a no-op-ish
    # bookkeeping write, not a real entitlement).
    active_sub_ids = await _active_subscription_ids(db, organization_id)
    result = await db.execute(
        select(CustomerModule).where(
            CustomerModule.organization_id == organization_id,
            CustomerModule.module == module,
            CustomerModule.subscription_id.in_(active_sub_ids) if active_sub_ids else CustomerModule.subscription_id == (sub.id if sub else None),
        )
    )
    row = result.scalars().first()
    before = {"enabled": row.enabled if row else False}

    if row is None:
        row = CustomerModule(
            organization_id=organization_id, module=module, created_by=actor.id,
            subscription_id=sub.id if sub else None,
        )
        db.add(row)

    # Real enforcement gates on CustomerModule.status (see access_service's
    # _entitlement_active), not on `enabled` — keep them in lockstep so a
    # row this endpoint just "enabled" is actually grantable.
    row.status = "ACTIVE" if body.enabled else "SUSPENDED"

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

    return _row_out(row)
