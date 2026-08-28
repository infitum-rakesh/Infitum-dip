"""
SsoConfiguration — one org-scoped SAML 2.0 Identity Provider config,
Admin-configurable from Settings -> Single Sign-On (SS4). One row per
organization (enforced by the unique constraint on organization_id) — an
org either has SAML configured or it doesn't; multi-IdP is out of scope.

The SP side (entity_id, ACS URL, metadata URL) is deliberately NOT stored
here — see services/saml_service.py's sp_settings_for(org), which derives
them from the org's slug + settings.api_public_base_url on every request.
That keeps them always correct if the deployment's base URL changes,
instead of getting stale in the database.

`idp_x509_cert` is the IdP's public signing certificate (PEM, no
BEGIN/END markers required — see saml_service._clean_cert) used to verify
the signature on every SAMLResponse; it is not a secret, so unlike the SMTP
password in Seller.smtp_config this is stored in plaintext.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class SsoConfiguration(PlatformBase):
    __tablename__ = "sso_configurations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), unique=True, nullable=False, index=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # If true, password login is blocked for this org's non-admin users —
    # see auth_service.authenticate_user(). ORG_ADMIN can always still use
    # a password, so a misconfigured IdP can never lock an org out for good.
    enforce_sso: Mapped[bool] = mapped_column(Boolean, default=False)

    idp_entity_id: Mapped[str] = mapped_column(String(500), default="")
    idp_sso_url: Mapped[str] = mapped_column(String(500), default="")
    idp_slo_url: Mapped[str] = mapped_column(String(500), default="")
    idp_x509_cert: Mapped[str] = mapped_column(Text, default="")

    # SAML assertion attribute names to read email/name from. Defaults
    # cover the two most common IdP conventions (Okta/Azure AD's URN claims
    # and a plain "email"/"name" fallback many test IdPs use) — see
    # saml_service._extract_attr(), which tries NameID last if neither hits.
    email_attribute: Mapped[str] = mapped_column(String(300), default="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")
    name_attribute: Mapped[str] = mapped_column(String(300), default="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    def __repr__(self) -> str:
        return f"<SsoConfiguration org={self.organization_id} enabled={self.enabled}>"
