"""
SSO router — /api/auth/sso/... — real SAML 2.0 login (SS3).

Flow:
  1. Browser navigates (full page nav, not fetch) to GET /{org_slug}/login
     -> we build a signed-binding-compatible AuthnRequest and 302 the
     browser to the IdP's SSO URL.
  2. User authenticates at the IdP. IdP POSTs a signed SAMLResponse to
     POST /{org_slug}/acs. We validate the signature (real, against the
     org's stored idp_x509_cert), JIT-provision/match the User, and run it
     through the EXACT SAME auth_service.issue_tokens() password login
     uses — so Organization.max_users seat-limit enforcement (TT2) applies
     identically here. We then 302 the browser to the frontend's SSO
     callback page with a short-lived single-use handoff code (never the
     JWTs themselves — those never touch the URL/browser history).
  3. Frontend's /sso/callback page calls POST /exchange with that code to
     get the real TokenResponse (same shape as /auth/login) and completes
     sign-in exactly like a password login would.

Handoff codes live in an in-memory dict with a short TTL (60s, single-use)
— consistent with this codebase's existing pattern for ephemeral
same-process state (see execution/active_run_registry.py's approach for
SQL Workspace query cancellation).
"""
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.connection import get_db
from app.models.user_activity import UserActivityLog, UserActivityType
from app.schemas.auth import OrgOut, TokenResponse, UserOut
from app.services import saml_service, sso_service
from app.services.auth_service import get_org_by_id, issue_tokens
from app.services.session_service import SeatLimitError
from app.models.user_session import LoginMethod

router = APIRouter(prefix="/auth/sso", tags=["sso"])
settings = get_settings()

_HANDOFF_TTL_SECONDS = 60
_pending_handoffs: dict[str, tuple[dict, float]] = {}


def _store_handoff(tokens: dict) -> str:
    code = secrets.token_urlsafe(32)
    _pending_handoffs[code] = (tokens, time.time() + _HANDOFF_TTL_SECONDS)
    # Opportunistic sweep of expired entries so this dict can't grow
    # unbounded in a long-running process.
    now = time.time()
    for k in [k for k, (_, exp) in _pending_handoffs.items() if exp < now]:
        _pending_handoffs.pop(k, None)
    return code


def _pop_handoff(code: str) -> dict | None:
    entry = _pending_handoffs.pop(code, None)
    if entry is None:
        return None
    tokens, expires_at = entry
    if expires_at < time.time():
        return None
    return tokens


def _login_error_redirect(message: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"{settings.frontend_base_url}/login?sso_error={quote(message)}", status_code=status.HTTP_302_FOUND)


@router.get("/{org_slug}/login")
async def sso_login(org_slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    org = await sso_service.get_org_by_slug(db, org_slug)
    if org is None:
        return _login_error_redirect("Unknown organization.")
    cfg = await sso_service.get_config(db, org.id)
    if cfg is None or not cfg.enabled:
        return _login_error_redirect("Single Sign-On is not enabled for this organization.")

    try:
        auth = await saml_service.build_auth(request, org_slug, cfg)
        redirect_url = auth.login()
    except saml_service.SamlError as exc:
        return _login_error_redirect(str(exc))
    except Exception:
        return _login_error_redirect("SSO is misconfigured for this organization — check the IdP settings (Entity ID, SSO URL, certificate) and try again.")
    return RedirectResponse(redirect_url, status_code=status.HTTP_302_FOUND)


@router.post("/{org_slug}/acs")
async def sso_acs(org_slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    org = await sso_service.get_org_by_slug(db, org_slug)
    if org is None:
        return _login_error_redirect("Unknown organization.")
    cfg = await sso_service.get_config(db, org.id)
    if cfg is None or not cfg.enabled:
        return _login_error_redirect("Single Sign-On is not enabled for this organization.")

    try:
        auth = await saml_service.build_auth(request, org_slug, cfg)
    except saml_service.SamlError as exc:
        return _login_error_redirect(str(exc))

    try:
        auth.process_response()
    except Exception:
        # python3-saml/lxml raise a range of exception types (XMLSyntaxError,
        # binascii.Error, etc.) for a malformed/tampered/missing SAMLResponse
        # — none of those should ever surface as a raw 500 to a browser that
        # just got redirected here by (or spoofing) an IdP.
        return _login_error_redirect("SSO sign-in failed: the response from your identity provider could not be read.")
    errors = auth.get_errors()
    if errors:
        detail = auth.get_last_error_reason() or "; ".join(errors)
        return _login_error_redirect(f"SSO sign-in failed: {detail}")
    if not auth.is_authenticated():
        return _login_error_redirect("SSO sign-in failed: the identity provider did not authenticate you.")

    email = saml_service.extract_attribute(auth, cfg.email_attribute) or auth.get_nameid()
    name = saml_service.extract_attribute(auth, cfg.name_attribute)
    if not email or "@" not in email:
        return _login_error_redirect("Your identity provider did not return a usable email address.")

    try:
        user = await sso_service.find_or_provision_user(db, org, email=email, full_name=name)
    except sso_service.SsoServiceError as exc:
        return _login_error_redirect(str(exc))

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    try:
        tokens = await issue_tokens(db, user, login_method=LoginMethod.SSO, ip_address=ip_address, user_agent=user_agent)
    except SeatLimitError as exc:
        return _login_error_redirect(str(exc))

    code = _store_handoff(tokens)
    return RedirectResponse(f"{settings.frontend_base_url}/sso/callback?code={code}", status_code=status.HTTP_302_FOUND)


@router.post("/exchange", response_model=TokenResponse)
async def sso_exchange(code: str, db: AsyncSession = Depends(get_db)):
    tokens = _pop_handoff(code)
    if tokens is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in link has expired or was already used. Please try signing in again.")

    from app.services.auth_service import get_user_by_id
    from app.core.security import decode_token
    payload = decode_token(tokens["access_token"])
    user = await get_user_by_id(db, int(payload["sub"]))
    org = await get_org_by_id(db, user.organization_id) if user and user.organization_id else None
    return TokenResponse(
        **tokens,
        user=UserOut.model_validate(user),
        organization=OrgOut.model_validate(org) if org else None,
    )


@router.get("/{org_slug}/metadata")
async def sso_metadata(org_slug: str, db: AsyncSession = Depends(get_db)):
    org = await sso_service.get_org_by_slug(db, org_slug)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown organization.")
    cfg = await sso_service.get_or_create_config(db, org.id)
    try:
        xml, errors = saml_service.get_metadata(org_slug, cfg)
    except saml_service.SamlError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Could not generate SP metadata — check the configured IdP certificate is valid base64.")
    if errors:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Invalid SP metadata: {'; '.join(errors)}")
    return Response(content=xml, media_type="application/xml")
