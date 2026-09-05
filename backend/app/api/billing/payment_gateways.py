"""
GET/POST /api/payment-gateways, PUT/{id}, POST /{id}/test|activate|
deactivate (SB18, spec sections 18-21/52). Seller Portal only — this is
the Seller's own processor configuration, never customer-facing (see
api/payments/pay.py and api/payments/webhook.py for the public-facing
counterparts). A distinct top-level prefix ("/payment-gateways", not
nested under "/billing" or "/seller") — the plan's own reasoning for
keeping this separate from /api/seller/subscriptions'
/api/seller/licenses' existing namespaces.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_seller_action
from app.database.connection import get_db
from app.models.billing.payment_gateway import GatewayProvider, PaymentGateway
from app.models.user import User
from app.schemas.billing.payment_gateways import (
    PROVIDER_FIELD_CATALOGUE,
    CreateGatewayRequest,
    PaymentGatewayListOut,
    PaymentGatewayOut,
    TestConnectionRequest,
    TestConnectionResultOut,
    UpdateGatewayRequest,
)
from app.services.billing import payment_gateway_service as svc

router = APIRouter(prefix="/payment-gateways", tags=["payment-gateways"])

_view = require_seller_action("VIEW", "PaymentGateways")
_edit = require_seller_action("EDIT", "PaymentGateways")


async def _to_out(db: AsyncSession, gateway: PaymentGateway) -> PaymentGatewayOut:
    summary = await svc.get_gateway_summary(db, gateway)
    return PaymentGatewayOut(
        id=gateway.id, provider=gateway.provider.value, display_name=gateway.display_name,
        status=gateway.status.value, environment=gateway.environment.value, default_currency=gateway.default_currency,
        is_active=gateway.is_active, live_verified_at=gateway.live_verified_at,
        credential_fields=PROVIDER_FIELD_CATALOGUE.get(gateway.provider.value, {}),
        created_at=gateway.created_at, updated_at=gateway.updated_at, **summary,
    )


async def _get_or_404(db: AsyncSession, gateway_id: int) -> PaymentGateway:
    gateway = await svc.get_gateway(db, gateway_id)
    if gateway is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment gateway not found.")
    return gateway


@router.get("", response_model=PaymentGatewayListOut)
async def list_payment_gateways(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    # One card per provider (spec section 19) — ensure every registered
    # provider has at least a NOT_CONFIGURED row so the frontend never
    # has to synthesize placeholder cards itself.
    for provider in GatewayProvider:
        await svc.get_or_create_gateway(db, provider, actor=user)
    gateways = await svc.list_gateways(db)
    gateways.sort(key=lambda g: g.provider.value)
    return PaymentGatewayListOut(items=[await _to_out(db, g) for g in gateways])


@router.post("", response_model=PaymentGatewayOut)
async def create_payment_gateway(
    body: CreateGatewayRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db),
):
    gateway = await svc.get_or_create_gateway(db, GatewayProvider(body.provider), actor=user)
    return await _to_out(db, gateway)


@router.put("/{gateway_id}", response_model=PaymentGatewayOut)
async def update_payment_gateway(
    gateway_id: int, body: UpdateGatewayRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db),
):
    gateway = await _get_or_404(db, gateway_id)

    if body.credentials is not None:
        env = body.credentials_environment or gateway.environment.value
        gateway = await svc.update_credentials(
            db, gateway, environment=env, credentials=body.credentials, webhook_secret=body.webhook_secret, actor=user,
        )

    if body.set_environment is not None:
        try:
            gateway = await svc.set_environment(db, gateway, environment=body.set_environment, actor=user)
        except svc.PaymentGatewayServiceError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    return await _to_out(db, gateway)


@router.post("/{gateway_id}/test", response_model=TestConnectionResultOut)
async def test_payment_gateway(
    gateway_id: int, body: TestConnectionRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db),
):
    gateway = await _get_or_404(db, gateway_id)
    result = await svc.test_connection(db, gateway, environment=body.environment or gateway.environment.value, actor=user)
    return TestConnectionResultOut(**result)


@router.post("/{gateway_id}/activate", response_model=PaymentGatewayOut)
async def activate_payment_gateway(gateway_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    gateway = await _get_or_404(db, gateway_id)
    try:
        gateway = await svc.activate_gateway(db, gateway, actor=user)
    except svc.PaymentGatewayServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, gateway)


@router.post("/{gateway_id}/deactivate", response_model=PaymentGatewayOut)
async def deactivate_payment_gateway(gateway_id: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    gateway = await _get_or_404(db, gateway_id)
    gateway = await svc.deactivate_gateway(db, gateway, actor=user)
    return await _to_out(db, gateway)
