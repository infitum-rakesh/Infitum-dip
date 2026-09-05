"""
Payment gateway registry — the single place that maps
PaymentGateway.provider -> a real PaymentGatewayProvider implementation.
Mirrors connectors/registry.py's own incremental-growth shape exactly:
starts with whichever adapters exist, gains one import + one dict entry
per phase as each provider is built (SB16: Custom; SB17: Stripe/
Razorpay/PayPal), never a placeholder/fake entry.
"""
from app.models.billing.payment_gateway import GatewayProvider
from app.payment_gateways.base import PaymentGatewayProvider
from app.payment_gateways.custom_provider import CustomProvider
from app.payment_gateways.paypal_provider import PayPalProvider
from app.payment_gateways.razorpay_provider import RazorpayProvider
from app.payment_gateways.stripe_provider import StripeProvider

_REGISTRY: dict[GatewayProvider, type[PaymentGatewayProvider]] = {
    GatewayProvider.CUSTOM: CustomProvider,  # live-verified — see custom_provider.py
    GatewayProvider.STRIPE: StripeProvider,  # real, disclosed unverified — see stripe_provider.py
    GatewayProvider.RAZORPAY: RazorpayProvider,  # real, disclosed unverified — see razorpay_provider.py
    GatewayProvider.PAYPAL: PayPalProvider,  # real, disclosed unverified — see paypal_provider.py
}


def register(provider: GatewayProvider, cls: type[PaymentGatewayProvider]) -> None:
    _REGISTRY[provider] = cls


def get_provider_class(provider: GatewayProvider) -> type[PaymentGatewayProvider] | None:
    return _REGISTRY.get(provider)


def get_provider(provider: GatewayProvider, *, credentials: dict, webhook_secret: str | None, environment: str) -> PaymentGatewayProvider | None:
    cls = get_provider_class(provider)
    if cls is None:
        return None
    return cls(credentials=credentials, webhook_secret=webhook_secret, environment=environment)


def registered_providers() -> list[GatewayProvider]:
    return list(_REGISTRY.keys())
