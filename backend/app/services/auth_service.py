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
from app.schemas.auth import RegisterRequest


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

    # Create org admin user. Registering counts as their first sign-in (the
    # response hands back live tokens), so last_login is stamped immediately —
    # otherwise the Users admin module would show its own founding admin as
    # "Pending Invitation" forever (see user_admin_service.status_of()).
    now = datetime.now(timezone.utc)
    user = User(
        email=data.email.lower(),
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=UserRole.ORG_ADMIN,
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


def issue_tokens(user: User) -> dict:
    extra = {"role": user.role, "org_id": user.organization_id}
    access = create_access_token(str(user.id), extra=extra)
    refresh = create_refresh_token(str(user.id))
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}


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
    return issue_tokens(user)
