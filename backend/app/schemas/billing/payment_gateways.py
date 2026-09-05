"""
Request/response models for /api/payment-gateways (SB18). Every output
model is built from masked_credential_keys()/PaymentGateway's own
non-secret columns — no schema here ever carries a decrypted credential
value or webhook secret; that boundary is enforced in
services/billing/payment_gateway_service.py and this file simply never
gives itself the ability to leak it.
"""
from datetime import datetime

from pydantic import BaseModel, Field

PROVIDER_FIELD_CATALOGUE = {
    # Disclosed to the frontend so the Configure modal can render the
    # right form fields per provider without hardcoding this list twice.
    "STRIPE": {"secret_key": "Secret Key", "publishable_key": "Publishable Key"},
    "RAZORPAY": {"key_id": "Key ID", "key_secret": "Key Secret"},
    "PAYPAL": {"client_id": "Client ID", "client_secret": "Client Secret"},
    "ADYEN": {"api_key": "API Key", "merchant_account": "Merchant Account"},
    "CUSTOM": {"internal_secret": "Internal Signing Secret"},
}


class PaymentGatewayOut(BaseModel):
    id: int
    provider: str
    display_name: str
    status: str
    environment: str
    default_currency: str
    is_active: bool
    live_verified_at: datetime | None
    configured_environments: list[str] = Field(description="Environments ('TEST'/'LIVE') with at least one credential field saved")
    credential_fields: dict[str, str] = Field(description="This provider's expected credential field names -> human labels")
    configured_credential_keys: dict[str, list[str]] = Field(description="environment -> which of credential_fields are currently saved")
    has_webhook_secret: dict[str, bool] = Field(description="environment -> whether a webhook secret is saved")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PaymentGatewayListOut(BaseModel):
    items: list[PaymentGatewayOut]


class CreateGatewayRequest(BaseModel):
    provider: str = Field(pattern="^(STRIPE|RAZORPAY|PAYPAL|ADYEN|CUSTOM)$")


class UpdateGatewayRequest(BaseModel):
    """PUT /api/payment-gateways/{id} — both parts are optional and
    independent: save credentials for one environment slot, and/or
    switch which environment the gateway is currently pointed at.
    Sending neither is a no-op; the two together let the Configure
    modal save-and-switch in one request."""
    credentials_environment: str | None = Field(None, pattern="^(TEST|LIVE)$", description="Which slot to write `credentials`/`webhook_secret` into")
    credentials: dict[str, str] | None = None
    webhook_secret: str | None = None
    set_environment: str | None = Field(None, pattern="^(TEST|LIVE)$", description="Switch the gateway's active environment")


class TestConnectionRequest(BaseModel):
    environment: str | None = Field(None, pattern="^(TEST|LIVE)$", description="Defaults to the gateway's current environment")


class TestConnectionResultOut(BaseModel):
    success: bool
    detail: str
    latency_ms: float | None = None
