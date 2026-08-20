"""Auth service — business logic for authentication."""
from datetime import datetime, timedelta, timezone
from typing import Tuple

from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.models.organization import LicenseStatus, Organization
from app.models.user import User, UserRole
from app.models.user_activity import UserActivityLog, UserActivityType
from app.models.user_session import LoginMethod
from app.schemas.auth import RegisterRequest
from app.services import session_service
from app.services.session_service import SeatLimitError


DEFAULT_TRIAL_DAYS = 14
DEFAULT_TRIAL_MODULES = [
    "DOCUMENT_INTELLIGENCE",
    "OCR",
    "AI_EXTRACTION",
    "TRANSFORMATION",
    "VALIDATION",
    "RECONCILIATION",  # Phase 7 — sibling of VALIDATION under DATA QUALITY, same trial default
    "SQL_WORKSPACE",   # Phase 9
]


class AuthError(Exception):
    """Raised for authentication/authorization failures."""


async def authenticate_user(
    db: AsyncSession, email: str, password: str,
    ip_address: str | None = None, user_agent: str | None = None,
) -> User:
    result = await db.execute(select(User).where(User.email == email.lower()))
    user = result.scalar_one_or_none()

    # SSO enforcement (SS4's "enforce_sso" toggle): checked before password
    # verification so a correct-but-blocked password never confirms itself
    # to whoever's typing it. ORG_ADMIN is always exempt — a misconfigured
    # or unreachable IdP can never lock every admin out of their own org.
    if user and user.organization_id and user.role not in (UserRole.ORG_ADMIN, UserRole.SUPER_ADMIN, UserRole.SELLER):
        from app.services import sso_service
        sso_cfg = await sso_service.get_config(db, user.organization_id)
        if sso_cfg and sso_cfg.enabled and sso_cfg.enforce_sso:
            raise AuthError("This organization requires Single Sign-On. Use the \"Sign in with SSO\" option instead of a password.")

    if not user or not verify_password(password, user.hashed_password):
        # Only logged when the email matches a real account — logging every
        # bad guess against an unknown address would just be enumeration
        # noise, not a useful audit trail entry.
        if user:
            db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.LOGIN_FAILED, message="Failed sign-in attempt (wrong password)", ip_address=ip_address, user_agent=user_agent))
            await db.commit()
        raise AuthError("Invalid email or password")
    if not user.is_active:
        raise AuthError("Account is deactivated. Contact your administrator.")
    user.last_login = datetime.now(timezone.utc)
    # Feeds both the Users admin module's "Login History" tab and
    # Administration -> Audit Logs' "Authentication" category — see
    # models/user_activity.py / services/audit_log_service.py.
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.LOGIN, message="Signed in", ip_address=ip_address, user_agent=user_agent))
    await db.commit()
    return user


async def register_user(db: AsyncSession, data: RegisterRequest) -> Tuple[User, Organization]:
    # Check duplicate email
    exists = await db.execute(select(User).where(User.email == data.email.lower()))
    if exists.scalar_one_or_none():
        raise AuthError("An account with this email already exists.")

    # Create organization
    slug = data.company_name.lower().replace(" ", "-")[:50]
    existing_org = await db.execute(select(Organization).where(Organization.slug == slug))
    if existing_org.scalar_one_or_none():
        raise AuthError(
            "An organization with this company name is already registered. "
            "Ask your admin to invite you instead of registering again."
        )

    org = Organization(
        name=data.company_name,
        slug=slug,
        country=data.country,
        license_status=LicenseStatus.TRIAL,
        licensed_modules=DEFAULT_TRIAL_MODULES,
        max_users=5,
        trial_expires_at=datetime.now(timezone.utc) + timedelta(days=DEFAULT_TRIAL_DAYS),
    )
    db.add(org)
    await db.flush()  # get org.id

    # Self-serve trial signups get module access only (DATA_ENGINEER — VIEW/
    # CREATE/EDIT/EXECUTE/APPROVE, no ADMIN/DELETE — see core/deps.ROLE_ACTIONS),
    # not ORG_ADMIN. A trial org therefore has no one who can manage users/
    # roles/settings/SSO for it — that's intentional: real customer orgs with
    # a real admin are provisioned by a Seller (see
    # services/seller/customer_service.create_customer, which does create an
    # ORG_ADMIN contact_user). Registering still counts as their first
    # sign-in (the response hands back live tokens), so last_login is
    # stamped immediately — otherwise the Users admin module would show its
    # own founding user as "Pending Invitation" forever (see
    # user_admin_service.status_of()).
    now = datetime.now(timezone.utc)
    user = User(
        email=data.email.lower(),
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=UserRole.DATA_ENGINEER,
        is_active=True,
        is_verified=False,
        organization_id=org.id,
        last_login=now,
    )
    db.add(user)
    await db.flush()
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.LOGIN, message="Registered organization"))
    await db.commit()
    await db.refresh(user)
    await db.refresh(org)
    return user, org


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_org_by_id(db: AsyncSession, org_id: int) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    return result.scalar_one_or_none()


def _mint_tokens(user: User, sid: str) -> dict:
    extra = {"role": user.role, "org_id": user.organization_id, "sid": sid}
    access = create_access_token(str(user.id), extra=extra)
    refresh = create_refresh_token(str(user.id), extra={"sid": sid, "org_id": user.organization_id})
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}


async def issue_tokens(
    db: AsyncSession, user: User, *,
    login_method: LoginMethod = LoginMethod.PASSWORD,
    ip_address: str | None = None, user_agent: str | None = None,
) -> dict:
    """
    Issues a fresh access+refresh token pair AND persists the UserSession
    row that backs them (see services/session_service.py) — this is the
    single real login/SSO entry point, and it's where seat-limit
    enforcement (Organization.max_users) is checked before a new session
    is allowed to exist. Raises SeatLimitError (caller maps to 403) if the
    org's concurrent-user ceiling is already in use by other users.
    """
    await session_service.check_seat_available(db, user.organization_id, user.id)
    sid = session_service.new_sid()
    tokens = _mint_tokens(user, sid)
    await session_service.create_session(
        db, sid=sid, user_id=user.id, organization_id=user.organization_id,
        login_method=login_method, ip_address=ip_address, user_agent=user_agent,
    )
    await db.commit()
    return tokens


async def refresh_access_token(db: AsyncSession, refresh_token: str) -> dict:
    try:
        payload = decode_token(refresh_token)
    except JWTError:
        raise AuthError("Invalid or expired refresh token.")
    if payload.get("type") != "refresh":
        raise AuthError("Invalid token type.")
    user = await get_user_by_id(db, int(payload["sub"]))
    if not user or not user.is_active:
        raise AuthError("User not found or inactive.")

    sid = payload.get("sid")
    if not sid:
        # Pre-TT refresh tokens issued before session tracking existed —
        # honor them once more but mint a brand-new tracked session so they
        # convert over instead of failing outright.
        return await issue_tokens(db, user)

    # Same session renewing its credentials — not a new login, so no new
    # seat check and no new UserSession row, just extend the existing one.
    still_active = await session_service.touch_session(db, sid)
    if not still_active:
        await db.rollback()
        raise AuthError("Your session has been signed out. Please log in again.")
    await db.commit()
    tokens = _mint_tokens(user, sid)
    return tokens
