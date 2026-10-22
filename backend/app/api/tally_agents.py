"""Tally Agents router — /api/tally-agents/... (Multi-Client TallyPrime
Integration Platform, Phase Group 1).

Every route except POST /tally-agents/enroll is an authenticated admin
route, gated on require_action(..., module="TALLY_INTEGRATION") — same
module string app/api/tally.py already uses, since this is a second
transport for the same feature area, not a new licensable module.

POST /tally-agents/enroll is deliberately NOT gated by a web-user
session — it's the one-shot exchange the agent itself performs, using
only its enrollment token as proof of authorization (see
tally_agent_service.exchange_enrollment_token's docstring). It uses the
plain (non-tenant-routed) DB session, same as /api/auth/login and the
other pre-auth endpoints in api/auth.py — correct today since this app
has one shared database until an org's own Bring-Your-Own-Database
cuts over (see database/tenant_engine_registry.py's own docstring).
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.core.rate_limit import check_rate_limit
from app.database.connection import get_db
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.ws import tally_agent_registry
from app.schemas.tally_agent import (
    AgentInstallationListOut,
    AgentInstallationOut,
    CreateAgentInstallationRequest,
    EnrollmentTokenOut,
    EnrollRequest,
    EnrollResponse,
    RevokeAgentRequest,
    RevokeAgentResponse,
    RotateCredentialResponse,
)
from app.services import tally_agent_service as svc
from app.services.tally_agent_service import TallyAgentServiceError

router = APIRouter(prefix="/tally-agents", tags=["tally-agents"])

_view = require_action("VIEW", module="TALLY_INTEGRATION")
_execute = require_action("EXECUTE", module="TALLY_INTEGRATION")


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _is_connected(installation_id: int) -> bool:
    return tally_agent_registry.is_connected(installation_id)


def _installation_out(installation) -> AgentInstallationOut:
    out = AgentInstallationOut.model_validate(installation)
    out.is_connected = _is_connected(installation.id)
    return out


@router.post("", response_model=AgentInstallationOut, status_code=status.HTTP_201_CREATED)
async def create_installation(body: CreateAgentInstallationRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An agent installation must be created for a specific organization.")
    installation = await svc.create_installation(
        db, org_id, name=body.name, tally_host=body.tally_host, tally_port=body.tally_port, created_by=user.id,
    )
    return _installation_out(installation)


@router.get("", response_model=AgentInstallationListOut)
async def list_installations(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    installations = await svc.list_installations(db, _effective_org_id(user))
    items = [_installation_out(i) for i in installations]
    return AgentInstallationListOut(items=items, total=len(items))


@router.get("/{installation_id}", response_model=AgentInstallationOut)
async def get_installation(installation_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    installation = await svc.get_installation(db, _effective_org_id(user), installation_id)
    if installation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent installation not found")
    return _installation_out(installation)


@router.post("/{installation_id}/enrollment-tokens", response_model=EnrollmentTokenOut, status_code=status.HTTP_201_CREATED)
async def create_enrollment_token(installation_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    try:
        installation, token, expires_at = await svc.generate_enrollment_token(db, org_id, installation_id, created_by=user.id)
    except TallyAgentServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return EnrollmentTokenOut(installation_id=installation.id, token=token, expires_at=expires_at)


@router.post("/enroll", response_model=EnrollResponse)
async def enroll(body: EnrollRequest, request: Request, db: AsyncSession = Depends(get_db)):
    # In-process per-IP sliding window — complements the token's own
    # persistent failed_attempts lockout (survives restarts, not IP-
    # dependent); see tally_agent_service.exchange_enrollment_token.
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"tally-agent-enroll:{client_ip}", max_attempts=10, window_seconds=600):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many enrollment attempts from this address — try again later.")
    try:
        installation, credential_id, credential_secret = await svc.exchange_enrollment_token(
            db, token=body.token, hostname=body.hostname, agent_version=body.agent_version, os_version=body.os_version,
        )
    except TallyAgentServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return EnrollResponse(
        installation_id=installation.id, credential_id=credential_id, credential_secret=credential_secret,
        ws_url="/api/tally-agent/ws",  # relative — the agent resolves this against the --server base URL it was given at enroll time
    )


@router.post("/{installation_id}/rotate-credential", response_model=RotateCredentialResponse)
async def rotate_credential(installation_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    try:
        installation, credential_id, credential_secret = await svc.rotate_credential(
            db, org_id, installation_id, actor_id=user.id,
        )
    except TallyAgentServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    force_disconnected = await tally_agent_registry.force_disconnect(
        installation.id, code=4403, reason="Credential rotated",
    )
    return RotateCredentialResponse(
        installation_id=installation.id, credential_id=credential_id, credential_secret=credential_secret,
        force_disconnected=force_disconnected,
    )


@router.post("/{installation_id}/revoke", response_model=RevokeAgentResponse)
async def revoke_installation(installation_id: int, body: RevokeAgentRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    org_id = _effective_org_id(user)
    try:
        installation = await svc.revoke_installation(
            db, org_id, installation_id, actor_id=user.id, reason=body.reason,
        )
    except TallyAgentServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    force_disconnected = await tally_agent_registry.force_disconnect(
        installation.id, code=4403, reason="Agent revoked",
    )
    return RevokeAgentResponse(
        installation_id=installation.id, status=installation.status.value, force_disconnected=force_disconnected,
    )
