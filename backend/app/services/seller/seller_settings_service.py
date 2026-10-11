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

from app.core import crypto
from app.models.seller import Seller
from app.models.seller_audit_log import record_seller_event
from app.models.user import User


def to_out(seller: Seller) -> dict:
    """Shapes the ORM row into SellerSettingsOut's dict — smtp_config's
    password_encrypted is never included, only a password_set boolean
    (same masking convention as Connection's secret fields)."""
    smtp = seller.smtp_config or {}
    return {
        "id": seller.id, "name": seller.name, "legal_name": seller.legal_name,
        "support_email": seller.support_email, "billing_email": seller.billing_email,
        "website": seller.website,
        "sales_contact_name": seller.sales_contact_name, "sales_email": seller.sales_email,
        "sales_phone": seller.sales_phone, "sales_url": seller.sales_url,
        "branding": seller.branding,
        "digital_signature": seller.digital_signature,
        "default_trial_days": seller.default_trial_days,
        "smtp": {
            "provider": smtp.get("provider") or "SMTP",
            "host": smtp.get("host"), "port": smtp.get("port"), "username": smtp.get("username"),
            "password_set": bool(smtp.get("password_encrypted")),
            "api_key_set": bool(smtp.get("api_key_encrypted")),
            "from_address": smtp.get("from_address"), "use_tls": smtp.get("use_tls", True),
        },
        "created_at": seller.created_at, "updated_at": seller.updated_at,
    }


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
    tracked_fields = (
        "name", "legal_name", "support_email", "billing_email", "website",
        "sales_contact_name", "sales_email", "sales_phone", "sales_url",
        "branding", "digital_signature", "default_trial_days",
    )
    before = {field: getattr(seller, field) for field in tracked_fields}
    changed = []
    for field in tracked_fields:
        if field in provided:
            new_value = getattr(body, field)
            if new_value != getattr(seller, field):
                setattr(seller, field, new_value)
                changed.append(field)

    if not changed:
        return seller

    await db.commit()
    await db.refresh(seller)

    after = {field: getattr(seller, field) for field in tracked_fields}
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller Settings Updated",
        resource=seller.name, resource_id=str(seller.id), ip_address=ip_address,
        details=f"Updated {', '.join(changed)}", before_value=before, after_value=after,
    )
    return seller


async def update_smtp_config(db: AsyncSession, seller: Seller, body, actor: User, ip_address: str | None = None) -> Seller:
    provided = body.model_fields_set
    current = dict(seller.smtp_config or {})
    _SECRET_KEYS = ("password_encrypted", "api_key_encrypted")
    before_masked = {k: v for k, v in current.items() if k not in _SECRET_KEYS}

    for field in ("provider", "host", "port", "username", "from_address", "use_tls"):
        if field in provided:
            value = getattr(body, field)
            if value is None:
                current.pop(field, None)
            else:
                current[field] = value.upper() if field == "provider" else value
    if "password" in provided and body.password:
        current["password_encrypted"] = crypto.encrypt_secret(body.password)
    if "api_key" in provided and body.api_key:
        current["api_key_encrypted"] = crypto.encrypt_secret(body.api_key)

    seller.smtp_config = current
    await db.commit()
    await db.refresh(seller)

    after_masked = {k: v for k, v in current.items() if k not in _SECRET_KEYS}
    after_masked["password_set"] = bool(current.get("password_encrypted"))
    after_masked["api_key_set"] = bool(current.get("api_key_encrypted"))
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller Settings Updated",
        resource=seller.name, resource_id=str(seller.id), ip_address=ip_address,
        details="Updated SMTP configuration", before_value=before_masked, after_value=after_masked,
    )
    return seller
