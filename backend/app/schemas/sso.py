from datetime import datetime

from pydantic import BaseModel


class SsoConfigurationOut(BaseModel):
    enabled: bool
    enforce_sso: bool
    idp_entity_id: str
    idp_sso_url: str
    idp_slo_url: str
    cert_set: bool          # never echoes the actual certificate content back
    email_attribute: str
    name_attribute: str
    updated_at: datetime | None = None

    # SP-side values the IdP admin needs to configure their side — derived
    # live from the org's slug, never stored (see saml_service.sp_settings_for).
    sp_entity_id: str
    sp_acs_url: str
    sp_metadata_url: str


class UpdateSsoConfigurationRequest(BaseModel):
    enabled: bool | None = None
    enforce_sso: bool | None = None
    idp_entity_id: str | None = None
    idp_sso_url: str | None = None
    idp_slo_url: str | None = None
    idp_x509_cert: str | None = None   # omit/None to leave the stored cert unchanged
    email_attribute: str | None = None
    name_attribute: str | None = None


class SsoLoginStartOut(BaseModel):
    redirect_url: str  # the IdP SSO URL to send the browser to, with a signed AuthnRequest
