"""
Shared FastAPI dependencies — current user/org resolution and centralized,
role-tier action gating for protected endpoints.

Additive module: does not modify existing auth flows in app/api/auth.py or
app/core/security.py. Any router that needs "who is calling" or "are they
allowed to do X" should depend on get_current_user / require_action here
rather than re-implementing checks inline.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.database.connection import get_db
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.services.auth_service import get_org_by_id, get_user_by_id
from app.services.billing.access_service import access_service

# tokenUrl is documentation-only (drives the OpenAPI "Authorize" button) —
# the app itself authenticates via POST /api/auth/login.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not token:
        raise _unauthorized()
    try:
        payload = decode_token(token)
    except JWTError:
        raise _unauthorized("Invalid or expired token")
    if payload.get("type") != "access":
        raise _unauthorized("Invalid token type")
    user = await get_user_by_id(db, int(payload["sub"]))
    if not user or not user.is_active:
        raise _unauthorized("User not found or inactive")
    return user


async def get_current_organization(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Organization | None:
    if not user.organization_id:
        return None
    return await get_org_by_id(db, user.organization_id)


# ── Role-tier action gating ──────────────────────────────────────
# Mirrors ROLE_DEFAULT_ACTIONS in frontend/src/utils/permissions.js so
# server-side enforcement matches what the UI already communicates.

ROLE_ACTIONS: dict[UserRole, set[str]] = {
    UserRole.SUPER_ADMIN:   {"VIEW", "CREATE", "EDIT", "DELETE", "EXECUTE", "APPROVE", "ADMIN"},
    UserRole.SELLER:        {"VIEW", "CREATE", "EDIT", "DELETE", "EXECUTE", "APPROVE", "ADMIN"},
    UserRole.ORG_ADMIN:     {"VIEW", "CREATE", "EDIT", "DELETE", "EXECUTE", "APPROVE", "ADMIN"},
    UserRole.DATA_ENGINEER: {"VIEW", "CREATE", "EDIT", "EXECUTE", "APPROVE"},
    UserRole.ANALYST:       {"VIEW", "EXECUTE"},
    UserRole.VIEWER:        {"VIEW"},
}

DOCUMENT_INTELLIGENCE_MODULE = "DOCUMENT_INTELLIGENCE"


def require_action(action: str, module: str | None = DOCUMENT_INTELLIGENCE_MODULE):
    """
    Dependency factory — 403s unless the caller's role tier permits `action`
    AND (when `module` is given and the user belongs to an org) at least one
    of the org's currently-ACTIVE subscriptions grants that module. Defaults
    to the Document Intelligence module for backward compatibility with
    existing Documents/Review Queue routes; pass module=None to skip the
    module-license check entirely (for foundational, always-available
    modules like Connections), or a different module string to gate on
    something else.
    Use per-route: Depends(require_action("EXECUTE")).

    Multi-Subscription Billing & Payments Platform (2026): the license
    check below used to be `module not in (org.licensed_modules or [])` —
    a single cached JSON list overwritten wholesale on every subscription
    change, which could never represent "two different subscriptions each
    granting this module, one of them still active." It now calls
    services/billing/access_service.SubscriptionAccessService, which
    evaluates real Subscription/CustomerModule rows live, per request —
    this is the entire integration point; no other module router changes.
    """
    async def _checker(
        user: User = Depends(get_current_user),
        org: Organization | None = Depends(get_current_organization),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if user.role != UserRole.SUPER_ADMIN and action not in ROLE_ACTIONS.get(user.role, set()):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Your role does not have permission to perform '{action}'.",
            )
        if module is not None and org is not None:
            result = await access_service.has_module_access(db, org.id, module)
            if not result.granted:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"{module.replace('_', ' ').title()} is not licensed for your organization.",
                )
        return user
    return _checker


# ── Seller Portal action gating ──────────────────────────────────
# Separate from require_action() above on purpose: Seller-tier users have
# organization_id = NULL (they aren't a customer), so get_current_organization
# always resolves to None for them and require_action's module-license check
# is a no-op either way. Seller Portal routes are gated on (a) role tier —
# only SELLER/SUPER_ADMIN may enter at all — and (b) an optional finer-grained
# seller_role sub-tier (SELLER PORTAL spec section 6: SELLER_ADMIN /
# SELLER_CUSTOMER_MANAGER / SELLER_LICENSE_MANAGER / SELLER_SUPPORT /
# SELLER_VIEWER), stored on User.seller_role. A Seller-tier user with no
# seller_role set gets full access — same "unset = full tier default"
# convention as User.custom_role_id elsewhere in this codebase.

SELLER_CATEGORIES = [
    "Dashboard", "Customers", "Subscriptions", "Modules", "Usage",
    "Licenses", "SellerUsers", "Audit", "Settings",
    # Multi-Subscription Billing & Payments Platform (2026)
    "Billing", "PaymentGateways",
    # AI Connection Subscription & Organisation-Level Access Management
    "AiConnections",
]

_SELLER_FULL = {"VIEW", "CREATE", "EDIT", "DELETE", "EXECUTE", "ADMIN"}
_SELLER_RW = {"VIEW", "CREATE", "EDIT", "DELETE", "EXECUTE"}
_SELLER_VIEW = {"VIEW"}

# seller_role -> category -> allowed actions. Matches SELLER PORTAL spec
# section 6 ("PERMISSIONS") exactly: SELLER_ADMIN gets everything;
# SELLER_CUSTOMER_MANAGER owns Customers; SELLER_LICENSE_MANAGER owns
# Subscriptions/Purchased Modules/Licenses; SELLER_SUPPORT gets
# Customers/Usage (+ Incidents, not a Seller-Portal category); SELLER_VIEWER
# is read-only everywhere.
SELLER_ROLE_ACTIONS: dict[str, dict[str, set[str]]] = {
    "SELLER_ADMIN": {c: _SELLER_FULL for c in SELLER_CATEGORIES},
    "SELLER_CUSTOMER_MANAGER": {
        "Dashboard": _SELLER_VIEW, "Customers": _SELLER_RW, "Usage": _SELLER_VIEW, "Audit": _SELLER_VIEW,
    },
    "SELLER_LICENSE_MANAGER": {
        "Dashboard": _SELLER_VIEW, "Subscriptions": _SELLER_RW, "Modules": _SELLER_RW,
        "Licenses": _SELLER_RW, "Audit": _SELLER_VIEW, "Billing": _SELLER_RW,
        "AiConnections": _SELLER_RW,
    },
    "SELLER_SUPPORT": {
        "Dashboard": _SELLER_VIEW, "Customers": _SELLER_VIEW, "Usage": _SELLER_VIEW, "Audit": _SELLER_VIEW,
    },
    "SELLER_VIEWER": {c: _SELLER_VIEW for c in SELLER_CATEGORIES},
}


def require_seller_action(action: str, category: str = "Dashboard"):
    """
    Dependency factory for Seller Portal routes — 403s unless the caller is
    Seller-tier (or platform SUPER_ADMIN, which bypasses everything) AND,
    when a seller_role sub-tier is set, that sub-tier permits `action` on
    `category`. Use per-route: Depends(require_seller_action("VIEW", "Customers")).
    """
    async def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in (UserRole.SELLER, UserRole.SUPER_ADMIN):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Seller Portal access required.")
        if user.role == UserRole.SUPER_ADMIN or not user.seller_role:
            return user
        allowed = SELLER_ROLE_ACTIONS.get(user.seller_role, {}).get(category, set())
        if action not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Your seller role ({user.seller_role}) does not permit '{action}' on {category}.",
            )
        return user
    return _checker
