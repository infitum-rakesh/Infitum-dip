"""
sso_service — SsoConfiguration CRUD (Admin Settings -> Single Sign-On) and
JIT user provisioning/matching for a validated SAML assertion. The actual
SAML protocol work (AuthnRequest, signature verification) lives in
saml_service.py; this module is the org-scoped config store + the bridge
from "a validated assertion for email X" to "a real User row for X".
"""
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.organization import Organization
from app.models.sso_configuration import SsoConfiguration
from app.models.user import User, UserRole
from app.models.user_activity import UserActivityLog, UserActivityType
from app.schemas.sso import SsoConfigurationOut
from app.services import saml_service


class SsoServiceError(Exception):
    pass


async def get_config(db: AsyncSession, organization_id: int) -> SsoConfiguration | None:
    result = await db.execute(select(SsoConfiguration).where(SsoConfiguration.organization_id == organization_id))
    return result.scalar_one_or_none()


async def get_or_create_config(db: AsyncSession, organization_id: int) -> SsoConfiguration:
    cfg = await get_config(db, organization_id)
    if cfg is None:
        cfg = SsoConfiguration(organization_id=organization_id)
        db.add(cfg)
        await db.flush()
    return cfg


def to_out(cfg: SsoConfiguration, org_slug: str) -> SsoConfigurationOut:
    urls = saml_service.sp_urls_for(org_slug)
    return SsoConfigurationOut(
        enabled=cfg.enabled, enforce_sso=cfg.enforce_sso,
        idp_entity_id=cfg.idp_entity_id, idp_sso_url=cfg.idp_sso_url, idp_slo_url=cfg.idp_slo_url,
        cert_set=bool(cfg.idp_x509_cert), email_attribute=cfg.email_attribute, name_attribute=cfg.name_attribute,
        updated_at=cfg.updated_at,
        sp_entity_id=urls["entity_id"], sp_acs_url=urls["acs_url"], sp_metadata_url=urls["metadata_url"],
    )


async def update_config(db: AsyncSession, cfg: SsoConfiguration, body, actor: User) -> SsoConfiguration:
    data = body.model_dump(exclude_unset=True)
    cert = data.pop("idp_x509_cert", None)
    for field, value in data.items():
        setattr(cfg, field, value)
    if cert:
        cfg.idp_x509_cert = cert
    cfg.updated_by_user_id = actor.id
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def get_org_by_slug(db: AsyncSession, slug: str) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


async def find_or_provision_user(db: AsyncSession, org: Organization, *, email: str, full_name: str | None) -> User:
    """
    JIT provisioning: an existing user with this email in this org is
    signed in as-is (role/permissions untouched). A first-time SSO login
    creates a new DATA_ENGINEER user (module access only, no admin — same
    policy as self-registration; see auth_service.register_user) with
    accessible_modules=None so they inherit everything the org is
    licensed for. An admin can promote them from Administration -> Users
    afterward same as any other account.
    """
    email = email.lower().strip()
    result = await db.execute(select(User).where(User.email == email))
    existing = result.scalar_one_or_none()
    if existing:
        if existing.organization_id != org.id:
            raise SsoServiceError(
                f"{email} already has an account in a different organization and can't sign in "
                f"to {org.name} via SSO."
            )
        if not existing.is_active:
            raise SsoServiceError("This account has been deactivated. Contact your administrator.")
        existing.last_login = datetime.now(timezone.utc)
        db.add(UserActivityLog(user_id=existing.id, event_type=UserActivityType.LOGIN, message="Signed in via SSO (SAML)"))
        await db.commit()
        await db.refresh(existing)
        return existing

    user = User(
        email=email,
        full_name=full_name or email.split("@")[0],
        hashed_password=hash_password(secrets.token_urlsafe(32)),  # unusable — this account only ever authenticates via SSO
        role=UserRole.DATA_ENGINEER,
        is_active=True,
        is_verified=True,  # the IdP already vouched for this identity
        organization_id=org.id,
        last_login=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.LOGIN, message="Provisioned + signed in via SSO (SAML) — first login"))
    await db.commit()
    await db.refresh(user)
    return user
