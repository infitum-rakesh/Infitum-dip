"""
Payment gateway configuration lifecycle (spec sections 18-21, 52).
Encrypts every credential field via app.core.crypto (same Fernet
mechanism already used for Seller SMTP passwords) and NEVER returns a
decrypted value to any caller outside this module — schemas/billing/
payment_gateways.py's output models only ever carry masked identifiers
and connection status, enforced here at the source rather than trusted
to schema field selection alone.

Subscription/invoice code never imports payment_gateways/* directly —
only this service and payment_service.py do, keeping every provider-
specific detail out of the billing domain logic (spec section 21).
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.billing.payment_gateway import (
    GatewayEnvironment,
    GatewayProvider,
    GatewayStatus,
    PaymentGateway,
    PaymentGatewayConfiguration,
)
from app.models.seller import Seller
from app.models.seller_audit_log import record_seller_event
from app.models.user import User
from app.payment_gateways import registry
from app.payment_gateways.base import PaymentGatewayError


class PaymentGatewayServiceError(Exception):
    pass


async def list_gateways(db: AsyncSession) -> list[PaymentGateway]:
    result = await db.execute(select(PaymentGateway))
    return list(result.scalars().all())


async def get_gateway(db: AsyncSession, gateway_id: int) -> PaymentGateway | None:
    return await db.get(PaymentGateway, gateway_id)


async def get_or_create_gateway(db: AsyncSession, provider: GatewayProvider, *, actor: User) -> PaymentGateway:
    """One row per provider (spec section 19's screen shows one card per
    provider) — created NOT_CONFIGURED on first touch."""
    result = await db.execute(select(PaymentGateway).where(PaymentGateway.provider == provider))
    gateway = result.scalar_one_or_none()
    if gateway is not None:
        return gateway
    seller_row = (await db.execute(select(Seller).limit(1))).scalar_one_or_none()
    gateway = PaymentGateway(
        seller_id=seller_row.id if seller_row else None, provider=provider,
        display_name=provider.value.title(), status=GatewayStatus.NOT_CONFIGURED,
        environment=GatewayEnvironment.TEST, default_currency="USD", is_active=False,
    )
    db.add(gateway)
    await db.flush()
    db.add(PaymentGatewayConfiguration(gateway_id=gateway.id, encrypted_credentials={}, encrypted_webhook_secret={}))
    await db.commit()
    await db.refresh(gateway)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Payment Gateway Created",
        module="PaymentGateways", resource=gateway.display_name, resource_id=str(gateway.id),
    )
    return gateway


async def _get_config(db: AsyncSession, gateway_id: int) -> PaymentGatewayConfiguration:
    result = await db.execute(select(PaymentGatewayConfiguration).where(PaymentGatewayConfiguration.gateway_id == gateway_id))
    config = result.scalar_one_or_none()
    if config is None:
        config = PaymentGatewayConfiguration(gateway_id=gateway_id, encrypted_credentials={}, encrypted_webhook_secret={})
        db.add(config)
        await db.flush()
    return config


def masked_credential_keys(config: PaymentGatewayConfiguration, environment: str) -> list[str]:
    """Field names configured for `environment`, never their values —
    used by the API layer to show "Publishable Key: configured" without
    ever transmitting the real value."""
    return sorted((config.encrypted_credentials or {}).get(environment, {}).keys())


async def get_gateway_summary(db: AsyncSession, gateway: PaymentGateway) -> dict:
    """Everything the API layer needs to render one gateway card,
    already stripped to non-secret shape — credential presence/field
    names only, never a decrypted value (see module docstring)."""
    config = await _get_config(db, gateway.id)
    creds_by_env = config.encrypted_credentials or {}
    webhook_by_env = config.encrypted_webhook_secret or {}
    return {
        "configured_environments": sorted(env for env, fields in creds_by_env.items() if fields),
        "configured_credential_keys": {env: masked_credential_keys(config, env) for env in ("TEST", "LIVE")},
        "has_webhook_secret": {env: bool(webhook_by_env.get(env)) for env in ("TEST", "LIVE")},
    }


async def update_credentials(
    db: AsyncSession, gateway: PaymentGateway, *, environment: str, credentials: dict[str, str],
    webhook_secret: str | None, actor: User,
) -> PaymentGateway:
    config = await _get_config(db, gateway.id)
    encrypted = dict(config.encrypted_credentials or {})
    encrypted[environment] = {k: encrypt_secret(v) for k, v in credentials.items() if v}
    config.encrypted_credentials = encrypted

    if webhook_secret:
        webhook_map = dict(config.encrypted_webhook_secret or {})
        webhook_map[environment] = encrypt_secret(webhook_secret)
        config.encrypted_webhook_secret = webhook_map

    # New credentials for the active environment invalidate any prior
    # live-verification — must re-run Test Connection (spec section 52).
    if environment == gateway.environment.value:
        gateway.status = GatewayStatus.NOT_CONFIGURED
        gateway.live_verified_at = None
        gateway.is_active = False

    await db.commit()
    await db.refresh(gateway)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Payment Gateway Configured",
        module="PaymentGateways", resource=gateway.display_name, resource_id=str(gateway.id),
        details=f"Credentials updated for {environment} environment",
    )
    return gateway


def _decrypted_credentials(config: PaymentGatewayConfiguration, environment: str) -> dict[str, str]:
    raw = (config.encrypted_credentials or {}).get(environment, {})
    return {k: decrypt_secret(v) for k, v in raw.items() if decrypt_secret(v) is not None}


def _decrypted_webhook_secret(config: PaymentGatewayConfiguration, environment: str) -> str | None:
    raw = (config.encrypted_webhook_secret or {}).get(environment)
    return decrypt_secret(raw) if raw else None


async def build_provider(db: AsyncSession, gateway: PaymentGateway, *, environment: str | None = None):
    """Constructs the real PaymentGatewayProvider instance for `gateway`
    with decrypted credentials for `environment` (defaults to the
    gateway's own current environment) — the ONLY place in the codebase
    outside this module that ever sees decrypted values."""
    env = environment or gateway.environment.value
    config = await _get_config(db, gateway.id)
    credentials = _decrypted_credentials(config, env)
    webhook_secret = _decrypted_webhook_secret(config, env)
    provider = registry.get_provider(gateway.provider, credentials=credentials, webhook_secret=webhook_secret, environment=env)
    if provider is None:
        raise PaymentGatewayServiceError(f"{gateway.provider.value} is not yet implemented.")
    return provider


async def test_connection(db: AsyncSession, gateway: PaymentGateway, *, environment: str, actor: User) -> dict:
    try:
        provider = await build_provider(db, gateway, environment=environment)
    except PaymentGatewayServiceError as exc:
        gateway.status = GatewayStatus.ERROR
        await db.commit()
        return {"success": False, "detail": str(exc)}

    try:
        result = await provider.test_connection()
    except PaymentGatewayError as exc:
        gateway.status = GatewayStatus.ERROR
        await db.commit()
        await record_seller_event(
            db, seller_user_id=actor.id, customer_id=None, action="Payment Gateway Tested",
            module="PaymentGateways", resource=gateway.display_name, resource_id=str(gateway.id),
            details=f"Test Connection failed ({environment}): {exc}",
        )
        return {"success": False, "detail": str(exc)}

    if result.success:
        gateway.status = GatewayStatus.CONNECTED
        if environment == "LIVE":
            gateway.live_verified_at = datetime.now(timezone.utc)
    else:
        gateway.status = GatewayStatus.ERROR
    await db.commit()
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Payment Gateway Tested",
        module="PaymentGateways", resource=gateway.display_name, resource_id=str(gateway.id),
        details=f"Test Connection ({environment}): {'succeeded' if result.success else 'failed'} — {result.detail}",
    )
    return {"success": result.success, "detail": result.detail, "latency_ms": result.latency_ms}


async def set_environment(db: AsyncSession, gateway: PaymentGateway, *, environment: str, actor: User) -> PaymentGateway:
    new_env = GatewayEnvironment(environment)
    if new_env == GatewayEnvironment.LIVE and gateway.live_verified_at is None:
        raise PaymentGatewayServiceError(
            "Test Connection must succeed in LIVE mode before switching this gateway to LIVE."
        )
    before = gateway.environment.value
    gateway.environment = new_env
    await db.commit()
    await db.refresh(gateway)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Payment Gateway Configured",
        module="PaymentGateways", resource=gateway.display_name, resource_id=str(gateway.id),
        before_value={"environment": before}, after_value={"environment": new_env.value},
    )
    return gateway


async def activate_gateway(db: AsyncSession, gateway: PaymentGateway, *, actor: User) -> PaymentGateway:
    if gateway.status != GatewayStatus.CONNECTED:
        raise PaymentGatewayServiceError("Test Connection must succeed before this gateway can be activated.")
    if gateway.environment == GatewayEnvironment.LIVE and gateway.live_verified_at is None:
        raise PaymentGatewayServiceError("LIVE mode requires a successful LIVE Test Connection first.")
    gateway.is_active = True
    await db.commit()
    await db.refresh(gateway)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Payment Gateway Activated",
        module="PaymentGateways", resource=gateway.display_name, resource_id=str(gateway.id),
    )
    return gateway


async def deactivate_gateway(db: AsyncSession, gateway: PaymentGateway, *, actor: User) -> PaymentGateway:
    gateway.is_active = False
    await db.commit()
    await db.refresh(gateway)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Payment Gateway Deactivated",
        module="PaymentGateways", resource=gateway.display_name, resource_id=str(gateway.id),
    )
    return gateway
