"""
Professional, module-specific access-denial messaging (spec sections
27-29) — never a generic "403 Forbidden" or blanket "your subscription
has expired." Shared by GET /api/access/check (api/access.py) and the
frontend's SubscriptionExpiredNotice (SB30), which renders this text
verbatim rather than deriving its own copy from the reason code.
"""
from app.services.billing.access_service import AccessResult, DenialReason
from app.services.seller.module_service import MODULE_CATALOG


def _module_label(module: str) -> str:
    return MODULE_CATALOG.get(module, {}).get("label", module.replace("_", " ").title())


def build_access_message(module: str, result: AccessResult) -> str:
    if result.granted:
        return ""
    label = _module_label(module)
    if result.reason == DenialReason.MODULE_EXPIRED and result.expired_at is not None:
        return (
            f"Your {label} subscription has expired. "
            f"Your access to this module ended on {result.expired_at.strftime('%d %B %Y')}. "
            "Please contact your Sales team to renew this subscription."
        )
    if result.reason == DenialReason.MODULE_EXPIRED:
        return f"Your {label} subscription has expired. Please contact your Sales team to renew this subscription."
    if result.reason == DenialReason.NO_ACTIVE_SUBSCRIPTION:
        return f"Your {label} subscription is not currently active. Please contact your Sales team to renew this subscription."
    if result.reason == DenialReason.MODULE_NOT_PURCHASED:
        return f"{label} is not included in your current subscription. Please contact your Sales team to add this module."
    if result.reason == DenialReason.TENANT_SUSPENDED:
        return "Your account has been suspended. Please contact your Sales team."
    if result.reason == DenialReason.USER_NOT_GRANTED_MODULE:
        return f"Your administrator has not granted you access to {label}."
    if result.reason == DenialReason.USER_INACTIVE:
        return "Your account is inactive. Please contact your administrator."
    if result.reason == DenialReason.TENANT_NOT_FOUND:
        return "No organization is associated with this account."
    return f"{label} is not available for your organization."
