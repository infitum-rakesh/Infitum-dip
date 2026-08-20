"""
saml_service — real SAML 2.0 SP-side logic (python3-saml, wrapping the
same OneLogin toolkit used by most production SAML integrations). Builds
signed-request-capable settings per org, drives the redirect-binding
AuthnRequest, and validates+parses a real signed SAMLResponse (XML
signature checked against the org's stored idp_x509_cert — nothing here
is faked or auto-accepted).

Design:
  - SP entity_id / ACS URL / metadata URL are derived from the org's slug
    + settings.api_public_base_url on every call — never persisted (see
    models/sso_configuration.py's docstring for why).
  - JIT (just-in-time) user provisioning: a first-time SSO login for a
    real email address creates a DATA_ENGINEER user for that org (module
    access only, matching the same "trial users get no admin access"
    policy as self-registration — see auth_service.register_user). An
    existing user with that email is just signed in.
  - Seat-limit enforcement is NOT duplicated here — api/sso.py's ACS route
    calls the exact same auth_service.issue_tokens() used by password
    login, so SSO logins are checked against Organization.max_users
    identically (this was the whole reason TT was built before SS).
"""
from urllib.parse import urlsplit

from fastapi import Request
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.settings import OneLogin_Saml2_Settings

from app.core.config import get_settings
from app.models.sso_configuration import SsoConfiguration

settings = get_settings()


class SamlError(Exception):
    """Raised for SAML configuration/validation failures — caller maps to an HTTP error."""


def _clean_cert(cert: str) -> str:
    """Accepts a cert with or without the PEM header/footer/newlines — the
    IdP admin pasting one from an XML metadata file is a common source of
    stray whitespace; python3-saml wants just the base64 body."""
    lines = [
        line.strip() for line in cert.strip().splitlines()
        if line.strip() and "BEGIN CERTIFICATE" not in line and "END CERTIFICATE" not in line
    ]
    return "".join(lines)


def sp_urls_for(org_slug: str) -> dict:
    base = settings.api_public_base_url.rstrip("/")
    return {
        "entity_id": f"{base}/api/auth/sso/{org_slug}/metadata",
        "acs_url": f"{base}/api/auth/sso/{org_slug}/acs",
        "metadata_url": f"{base}/api/auth/sso/{org_slug}/metadata",
    }


def build_settings(org_slug: str, cfg: SsoConfiguration) -> dict:
    if not cfg.idp_entity_id or not cfg.idp_sso_url or not cfg.idp_x509_cert:
        raise SamlError("SSO is not fully configured for this organization yet — IdP Entity ID, SSO URL, and certificate are all required.")

    urls = sp_urls_for(org_slug)
    return {
        "strict": True,
        "debug": settings.app_debug,
        "sp": {
            "entityId": urls["entity_id"],
            "assertionConsumerService": {
                "url": urls["acs_url"],
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        },
        "idp": {
            "entityId": cfg.idp_entity_id,
            "singleSignOnService": {
                "url": cfg.idp_sso_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            **({"singleLogoutService": {"url": cfg.idp_slo_url, "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"}} if cfg.idp_slo_url else {}),
            "x509cert": _clean_cert(cfg.idp_x509_cert),
        },
        "security": {
            "wantAssertionsSigned": True,
            "wantMessagesSigned": False,
            "wantNameIdEncrypted": False,
            "authnRequestsSigned": False,  # SP has no private key configured — IdP just needs to trust an unsigned AuthnRequest, which every major IdP supports
            "logoutRequestSigned": False,
            "logoutResponseSigned": False,
        },
    }


async def _request_data(request: Request) -> dict:
    """Builds the dict python3-saml expects to describe the current HTTP
    request — see onelogin.saml2.auth.OneLogin_Saml2_Auth's docstring.

    Deliberately does NOT take scheme/host from `request.url`: in dev, Vite's
    proxy (changeOrigin) rewrites the Host header to this backend's own port
    before FastAPI ever sees it, and in prod a reverse proxy can do the same
    — either way `request.url` reflects an internal address, not the public
    one this SP's entity_id/ACS URL were built from (settings.api_public_base_url,
    see sp_urls_for()). python3-saml's strict mode compares the SAMLResponse's
    Destination (which the IdP filled in from the ACS URL WE sent in the
    AuthnRequest — the public one) against this self-constructed URL, so a
    mismatch here fails every real login with a confusing signature-adjacent
    error. Only path/query genuinely need to come from the live request.
    """
    public = urlsplit(settings.api_public_base_url)
    path = urlsplit(str(request.url)).path
    form = {}
    if request.method == "POST":
        form_data = await request.form()
        form = dict(form_data)
    return {
        "https": "on" if public.scheme == "https" else "off",
        "http_host": public.netloc,
        "script_name": path,
        "server_port": public.port or (443 if public.scheme == "https" else 80),
        "get_data": dict(request.query_params),
        "post_data": form,
    }


async def build_auth(request: Request, org_slug: str, cfg: SsoConfiguration) -> OneLogin_Saml2_Auth:
    req_data = await _request_data(request)
    saml_settings = build_settings(org_slug, cfg)
    return OneLogin_Saml2_Auth(req_data, old_settings=saml_settings)


def get_metadata(org_slug: str, cfg: SsoConfiguration) -> tuple[str, list[str]]:
    """Returns (metadata_xml, errors) for the SP metadata endpoint — this is
    what an IdP admin uploads/points at when configuring their side. Real
    metadata built by the toolkit from build_settings(), not hand-written."""
    saml_settings = OneLogin_Saml2_Settings(build_settings(org_slug, cfg), sp_validation_only=True)
    metadata = saml_settings.get_sp_metadata()
    errors = saml_settings.validate_metadata(metadata)
    return metadata.decode("utf-8") if isinstance(metadata, bytes) else metadata, list(errors)


def extract_attribute(auth: OneLogin_Saml2_Auth, attribute_name: str) -> str | None:
    values = auth.get_attribute(attribute_name) if attribute_name else None
    if values:
        return values[0]
    return None
