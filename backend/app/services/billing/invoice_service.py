"""
Invoice generation — spec section 13: an invoice is generated for EVERY
subscription lifecycle event (create incl. trial, renew, upgrade,
downgrade, add-module, extend), never only for paid ones. A trial
invoice posts at $0.00 / NO_PAYMENT_REQUIRED immediately (spec section
10); a paid one posts PAYMENT_PENDING until services/billing/
payment_service.py's record_payment() flips it to PAID.

Invoice numbers are generated here, server-side only (spec section 14 —
"never generate invoice numbers in frontend JavaScript"), via a
year-scoped InvoiceCounter row incremented inside a committed
transaction. Combined with SQLite's single-writer WAL semantics and the
in-process asyncio.Lock below (used ONLY here, never nested with any
other lock — see the asyncio-lock-deadlock bug on
[[dip-byo-database]] this project hit once before), two concurrent
invoice creations can never produce the same number.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing.invoice import Invoice, InvoiceCounter, InvoiceLineItem, InvoiceStatus
from app.models.customer_module import CustomerModule
from app.models.organization import Organization
from app.models.plan import Plan
from app.models.seller import Seller
from app.models.seller_audit_log import record_seller_event
from app.models.subscription import Subscription, SubscriptionStatus
from app.services.billing import tax_service
from app.services.seller.module_service import MODULE_CATALOG

_counter_lock = asyncio.Lock()


async def next_invoice_number(db: AsyncSession, *, year: int | None = None) -> str:
    year = year or datetime.now(timezone.utc).year
    async with _counter_lock:
        result = await db.execute(select(InvoiceCounter).where(InvoiceCounter.year == year))
        counter = result.scalar_one_or_none()
        if counter is None:
            counter = InvoiceCounter(year=year, last_number=0)
            db.add(counter)
            await db.flush()
        counter.last_number += 1
        number = counter.last_number
        await db.commit()
        await db.refresh(counter)
    return f"INV-{year}-{number:06d}"


def _module_label(module: str) -> str:
    return MODULE_CATALOG.get(module, {}).get("label", module.replace("_", " ").title())


async def create_invoice_for_subscription(
    db: AsyncSession, *, subscription: Subscription, reason: str, actor_id: int | None = None,
) -> Invoice:
    """Creates and persists a real Invoice + InvoiceLineItem rows for
    `subscription`. `reason` is one of CREATE|TRIAL|RENEWAL|UPGRADE|
    DOWNGRADE|ADD_MODULE|EXTENSION — purely descriptive (spec section 13's
    list of what triggers an invoice), no behavior branches on it besides
    the InvoiceStatus choice below."""
    org = await db.get(Organization, subscription.organization_id)
    plan = await db.get(Plan, subscription.plan_id) if subscription.plan_id else None
    seller_row = (await db.execute(select(Seller).limit(1))).scalar_one_or_none()

    result = await db.execute(select(CustomerModule).where(CustomerModule.subscription_id == subscription.id))
    entitlements = list(result.scalars().all())

    is_trial = subscription.status == SubscriptionStatus.TRIAL

    line_items_data = []
    if entitlements:
        per_module = 0.0 if is_trial else (round(subscription.amount / len(entitlements), 2) if subscription.amount else 0.0)
        for cm in entitlements:
            line_items_data.append({"module": cm.module, "description": _module_label(cm.module), "unit_price": per_module})
    else:
        # No entitlement rows yet (shouldn't normally happen — every
        # subscription gets its CustomerModule rows created first — but
        # never let invoicing itself fail because of it).
        description = f"{plan.name} plan" if plan else "Subscription"
        line_items_data.append({"module": None, "description": description, "unit_price": 0.0 if is_trial else subscription.amount})

    subtotal = round(sum(li["unit_price"] for li in line_items_data), 2)
    discount = 0.0
    tax = 0.0 if is_trial else await tax_service.calculate_tax(subtotal - discount, org)
    total = 0.0 if is_trial else round(subtotal - discount + tax, 2)

    invoice_number = await next_invoice_number(db)
    now = datetime.now(timezone.utc)
    invoice = Invoice(
        invoice_number=invoice_number,
        organization_id=org.id,
        subscription_id=subscription.id,
        seller_id=seller_row.id if seller_row else None,
        status=InvoiceStatus.NO_PAYMENT_REQUIRED if is_trial else InvoiceStatus.PAYMENT_PENDING,
        currency=subscription.currency,
        subtotal=0.0 if is_trial else subtotal,
        discount=0.0 if is_trial else discount,
        tax=tax,
        total=total,
        billing_period_start=subscription.current_period_start or subscription.start_date,
        billing_period_end=subscription.current_period_end or subscription.end_date,
        issue_date=now,
        due_date=None if is_trial else now + timedelta(days=15),
        reason=reason,
        created_by=actor_id,
    )
    db.add(invoice)
    await db.flush()

    for li in line_items_data:
        db.add(InvoiceLineItem(
            invoice_id=invoice.id, module=li["module"], description=li["description"],
            quantity=1, unit_price=li["unit_price"], discount=0.0, tax=0.0, total=li["unit_price"],
        ))
    await db.commit()
    await db.refresh(invoice)

    # SB32, found live: invoice creation had no audit-trail entry of its
    # own — every trial/renewal/upgrade/downgrade/add-module/extension
    # invoice was a real financial record with no "who/when/what"
    # attached, distinct from the subscription-level "Subscription
    # Changed" event that already exists for the action that triggered it.
    await record_seller_event(
        db, seller_user_id=actor_id, customer_id=org.id, action="Invoice Created",
        module="Billing", resource=invoice.invoice_number, resource_id=str(invoice.id),
        details=f"{reason}: {invoice.currency} {invoice.total:,.2f} ({invoice.status.value})",
    )
    return invoice


async def list_invoices(
    db: AsyncSession, *, organization_id: int | None = None, status_filter: str | None = None,
    page: int = 1, page_size: int = 20,
) -> tuple[list[dict], int]:
    stmt = select(Invoice, Organization).join(Organization, Organization.id == Invoice.organization_id)
    if organization_id is not None:
        stmt = stmt.where(Invoice.organization_id == organization_id)
    if status_filter:
        stmt = stmt.where(Invoice.status == status_filter)
    stmt = stmt.order_by(Invoice.issue_date.desc())

    count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = count_result.scalar_one()

    result = await db.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    items = []
    for inv, org in result.all():
        items.append({
            "id": inv.id, "invoice_number": inv.invoice_number, "organization_id": org.id,
            "organization_name": org.name, "subscription_id": inv.subscription_id,
            "status": inv.status.value, "currency": inv.currency, "total": inv.total,
            "issue_date": inv.issue_date, "due_date": inv.due_date, "reason": inv.reason,
        })
    return items, total


async def get_invoice(db: AsyncSession, invoice_id: int) -> Invoice | None:
    return await db.get(Invoice, invoice_id)


async def get_invoice_line_items(db: AsyncSession, invoice_id: int) -> list[InvoiceLineItem]:
    result = await db.execute(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id))
    return list(result.scalars().all())
