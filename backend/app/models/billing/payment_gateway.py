"""
PaymentGateway / PaymentGatewayConfiguration — the Seller-configured
payment processor abstraction (spec sections 18-21). One row per
provider; TEST and LIVE credentials both live inside the configuration
row's encrypted_credentials JSON so switching environment never loses
the other mode's config, and a LIVE switch is only allowed once Test
Connection has succeeded in that mode (enforced in
services/billing/payment_gateway_service.py, not here).

Secrets are Fernet-encrypted via app.core.crypto (same mechanism already
used for Seller.smtp_config's password) and NEVER returned to the
frontend in decrypted form — see schemas/billing/payment_gateways.py.
"""
import enum
from datetime import datetime

from sqlalchemy import String, DateTime, Boolean, ForeignKey, Enum, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class GatewayProvider(str, enum.Enum):
    STRIPE = "STRIPE"
    RAZORPAY = "RAZORPAY"
    PAYPAL = "PAYPAL"
    ADYEN = "ADYEN"
    CUSTOM = "CUSTOM"


class GatewayStatus(str, enum.Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class GatewayEnvironment(str, enum.Enum):
    TEST = "TEST"
    LIVE = "LIVE"


class PaymentGateway(PlatformBase):
    __tablename__ = "payment_gateways"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id"), nullable=True, index=True)
    provider: Mapped[GatewayProvider] = mapped_column(Enum(GatewayProvider), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[GatewayStatus] = mapped_column(Enum(GatewayStatus), default=GatewayStatus.NOT_CONFIGURED)
    environment: Mapped[GatewayEnvironment] = mapped_column(Enum(GatewayEnvironment), default=GatewayEnvironment.TEST)
    default_currency: Mapped[str] = mapped_column(String(8), default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set only after a Test Connection has succeeded while environment=LIVE
    # — services/billing/payment_gateway_service.py enforces this is a
    # precondition for is_active=True when environment=LIVE, per spec
    # section 52 ("before LIVE activation, Test Connection must succeed").
    live_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<PaymentGateway {self.provider} {self.status}>"


class PaymentGatewayConfiguration(PlatformBase):
    __tablename__ = "payment_gateway_configurations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("payment_gateways.id"), unique=True, nullable=False, index=True)
    # {"test": {"<field>": "<fernet-encrypted>", ...}, "live": {...}} — per
    # provider fields (e.g. Stripe: publishable_key/secret_key; Razorpay:
    # key_id/key_secret). Every value under both "test" and "live" is
    # individually Fernet-encrypted via app.core.crypto.encrypt_secret();
    # the dict shape itself (which keys exist) is not secret, only values.
    encrypted_credentials: Mapped[dict] = mapped_column(JSON, default=dict)
    # {"test": "<fernet-encrypted>", "live": "<fernet-encrypted>"}
    encrypted_webhook_secret: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<PaymentGatewayConfiguration gateway={self.gateway_id}>"
