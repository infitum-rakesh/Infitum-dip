"""Schemas for the Multi-Client TallyPrime Integration Platform (Agent
Relay) — see models/tally_agent.py's own docstring for the design."""
from datetime import datetime

from pydantic import BaseModel


class AgentInstallationOut(BaseModel):
    id: int
    name: str
    status: str
    hostname: str | None
    agent_version: str | None
    os_version: str | None
    tally_host: str
    tally_port: int
    is_connected: bool = False  # resolved from the live in-memory registry, not a DB column — default lets model_validate(orm_obj) succeed before the caller overwrites it
    last_connected_at: datetime | None
    last_disconnected_at: datetime | None
    last_heartbeat_at: datetime | None
    last_seen_ip: str | None
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None

    model_config = {"from_attributes": True}


class AgentInstallationListOut(BaseModel):
    items: list[AgentInstallationOut]
    total: int


class CreateAgentInstallationRequest(BaseModel):
    name: str
    tally_host: str = "127.0.0.1"
    tally_port: int = 9000


class EnrollmentTokenOut(BaseModel):
    """Returned exactly once, at creation — the plaintext token is never
    retrievable again (only its bcrypt hash is stored)."""
    installation_id: int
    token: str
    expires_at: datetime


class EnrollRequest(BaseModel):
    """Body of the one-shot, unauthenticated-by-JWT HTTPS exchange the
    agent itself performs — authenticated by the enrollment token's own
    validity, not a web-user session."""
    token: str
    hostname: str | None = None
    agent_version: str | None = None
    os_version: str | None = None


class EnrollResponse(BaseModel):
    """Returned exactly once — the agent must persist credential_id +
    credential_secret immediately (Windows DPAPI in production) since
    the server never redisplays the secret again."""
    installation_id: int
    credential_id: str
    credential_secret: str
    ws_url: str


class RotateCredentialResponse(BaseModel):
    """Returned exactly once, same discipline as EnrollResponse. Rotating
    immediately disconnects the agent if it's currently connected —
    force_disconnected tells the caller whether that actually happened,
    so the UI can say so explicitly rather than leaving it ambiguous."""
    installation_id: int
    credential_id: str
    credential_secret: str
    force_disconnected: bool


class RevokeAgentRequest(BaseModel):
    reason: str | None = None


class RevokeAgentResponse(BaseModel):
    installation_id: int
    status: str
    force_disconnected: bool
