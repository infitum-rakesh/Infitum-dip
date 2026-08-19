"""
Seller Portal -> Seller Settings service (Phase 9) — Company, Branding
and Default License Settings, all real columns on the singleton Seller
row (models/seller.py, seeded by plan_seed.ensure_seller_seeded at
startup). Plan catalog management (the spec's "Plans" settings section)
is intentionally NOT duplicated here — services/seller/subscription_service.
create_plan()/update_plan() already do this (built in Phase 3, never
surfaced in any UI until now); this phase's frontend Plans tab calls
those existing endpoints directly rather than adding a second code path.

Partial update uses body.model_fields_set (not `is not None`) so a caller
can explicitly clear an optional field (e.g. legal_name) back to null —
see the null-vs-omitted bug fixed in module_service.set_module() during
Phase 5; the same mistake is deliberately avoided here from the start.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.seller import Seller
from app.models.seller_audit_log import record_seller_event
from app.models.user import User


async def get_settings(db: AsyncSession) -> Seller:
    result = await db.execute(select(Seller).limit(1))
    seller = result.scalar_one_or_none()
    if seller is None:
        # Defensive only — plan_seed.ensure_seller_seeded() always creates
        # this row at startup before any request can reach here.
        seller = Seller(name="Platform Seller", default_trial_days=14)
        db.add(seller)
        await db.commit()
        await db.refresh(seller)
    return seller


async def update_settings(db: AsyncSession, seller: Seller, body, actor: User, ip_address: str | None = None) -> Seller:
    provided = body.model_fields_set
    before = {
        "name": seller.name, "legal_name": seller.legal_name, "support_email": seller.support_email,
        "billing_email": seller.billing_email, "website": seller.website, "branding": seller.branding,
        "default_trial_days": seller.default_trial_days,
    }
    changed = []
    for field in ("name", "legal_name", "support_email", "billing_email", "website", "branding", "default_trial_days"):
        if field in provided:
            new_value = getattr(body, field)
            if new_value != getattr(seller, field):
                setattr(seller, field, new_value)
                changed.append(field)

    if not changed:
        return seller

    await db.commit()
    await db.refresh(seller)

    after = {
        "name": seller.name, "legal_name": seller.legal_name, "support_email": seller.support_email,
        "billing_email": seller.billing_email, "website": seller.website, "branding": seller.branding,
        "default_trial_days": seller.default_trial_days,
    }
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller Settings Updated",
        resource=seller.name, resource_id=str(seller.id), ip_address=ip_address,
        details=f"Updated {', '.join(changed)}", before_value=before, after_value=after,
    )
    return seller
