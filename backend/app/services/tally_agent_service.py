"""
tally_agent_service.py — enrollment & credential lifecycle for the
Multi-Client TallyPrime Integration Platform (Agent Relay). See
models/tally_agent.py's own docstring for the full design; this module
is the service layer behind api/tally_agents.py's enrollment routes and
(from Phase Group 2 onward) the WebSocket gateway's auth handshake.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.models.audit_log import AuditStatus, record_event
from app.models.tally_agent import (
    AgentCredential,
    AgentCredentialStatus,
    AgentInstallation,
    AgentInstallationStatus,
    EnrollmentToken,
)

ENROLLMENT_TOKEN_TTL_MINUTES = 30
ENROLLMENT_MAX_FAILED_ATTEMPTS = 5
CREDENTIAL_LOCKOUT_THRESHOLD = 5
CREDENTIAL_LOCKOUT_MINUTES = 15


class TallyAgentServiceError(Exception):
    """Raised for enrollment/credential validation failures — caught in
    api/tally_agents.py and turned into the appropriate HTTP status."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def create_installation(
    db: AsyncSession, organization_id: int | None, *, name: str, tally_host: str = "127.0.0.1",
    tally_port: int = 9000, created_by: int | None = None,
) -> AgentInstallation:
    installation = AgentInstallation(
        organization_id=organization_id, name=name, tally_host=tally_host, tally_port=tally_port,
        status=AgentInstallationStatus.PENDING_ENROLLMENT, created_by=created_by,
    )
    db.add(installation)
    await db.commit()
    await db.refresh(installation)
    await record_event(
        db, organization_id=organization_id, actor_id=created_by, action="Agent Installation Created",
        module="Tally Agent", resource="AgentInstallation", resource_id=str(installation.id),
        status=AuditStatus.SUCCESS, after_value={"name": name},
    )
    return installation


async def list_installations(db: AsyncSession, organization_id: int | None) -> list[AgentInstallation]:
    stmt = select(AgentInstallation)
    if organization_id is not None:
        stmt = stmt.where(AgentInstallation.organization_id == organization_id)
    rows = (await db.execute(stmt.order_by(AgentInstallation.created_at.desc()))).scalars().all()
    return list(rows)


async def get_installation(db: AsyncSession, organization_id: int | None, installation_id: int) -> AgentInstallation | None:
    installation = await db.get(AgentInstallation, installation_id)
    if installation is None:
        return None
    if organization_id is not None and installation.organization_id != organization_id:
        return None
    return installation


async def generate_enrollment_token(
    db: AsyncSession, organization_id: int | None, installation_id: int, *, created_by: int | None,
) -> tuple[AgentInstallation, str, datetime]:
    """Returns (installation, plaintext_token, expires_at) — the
    plaintext token is never stored and never retrievable again."""
    installation = await get_installation(db, organization_id, installation_id)
    if installation is None:
        raise TallyAgentServiceError("Agent installation not found.")
    if installation.status != AgentInstallationStatus.PENDING_ENROLLMENT:
        raise TallyAgentServiceError(
            f"This installation is already {installation.status.value.lower()} — generate a new installation for "
            "a fresh enrollment, or use rotate-credential to reissue credentials for an already-enrolled agent."
        )

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ENROLLMENT_TOKEN_TTL_MINUTES)
    db.add(EnrollmentToken(
        organization_id=organization_id, agent_installation_id=installation.id,
        token_hash=_sha256(token), expires_at=expires_at, created_by=created_by,
    ))
    await db.commit()
    await record_event(
        db, organization_id=organization_id, actor_id=created_by, action="Enrollment Token Generated",
        module="Tally Agent", resource="AgentInstallation", resource_id=str(installation.id),
        status=AuditStatus.SUCCESS,
    )
    return installation, token, expires_at


async def exchange_enrollment_token(
    db: AsyncSession, *, token: str, hostname: str | None, agent_version: str | None, os_version: str | None,
) -> tuple[AgentInstallation, str, str]:
    """The agent's one-shot exchange — authenticated by the token itself,
    not a web-user session (this is called before the agent has any
    credential at all). Returns (installation, credential_id, credential_secret);
    credential_secret is never stored and never retrievable again."""
    token_hash = _sha256(token)
    row = (await db.execute(
        select(EnrollmentToken).where(EnrollmentToken.token_hash == token_hash)
    )).scalar_one_or_none()

    if row is None:
        raise TallyAgentServiceError("Invalid enrollment token.")
    if row.used_at is not None:
        raise TallyAgentServiceError("This enrollment token has already been used.")
    if row.failed_attempts >= ENROLLMENT_MAX_FAILED_ATTEMPTS:
        raise TallyAgentServiceError("This enrollment token has been locked after too many failed attempts.")
    now = datetime.now(timezone.utc)
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise TallyAgentServiceError("This enrollment token has expired.")

    installation = await db.get(AgentInstallation, row.agent_installation_id)
    if installation is None or installation.status != AgentInstallationStatus.PENDING_ENROLLMENT:
        row.failed_attempts += 1
        await db.commit()
        raise TallyAgentServiceError("This installation is no longer eligible for enrollment.")

    credential_id = uuid.uuid4().hex
    credential_secret = secrets.token_urlsafe(32)
    db.add(AgentCredential(
        organization_id=installation.organization_id, agent_installation_id=installation.id,
        credential_id=credential_id, secret_hash=hash_password(credential_secret),
        status=AgentCredentialStatus.ACTIVE,
    ))
    row.used_at = now
    installation.status = AgentInstallationStatus.ENROLLED
    installation.hostname = hostname
    installation.agent_version = agent_version
    installation.os_version = os_version
    await db.commit()
    await db.refresh(installation)

    await record_event(
        db, organization_id=installation.organization_id, actor_id=None, action="Agent Enrolled",
        module="Tally Agent", resource="AgentInstallation", resource_id=str(installation.id),
        status=AuditStatus.SUCCESS, after_value={"hostname": hostname, "agent_version": agent_version},
    )
    return installation, credential_id, credential_secret


async def verify_agent_credential(
    db: AsyncSession, *, credential_id: str, credential_secret: str, source_ip: str | None = None,
) -> tuple[AgentInstallation, AgentCredential] | None:
    """Used by the WebSocket auth handshake (Phase Group 2). Returns
    (installation, credential) on success, None on any failure — the
    caller is responsible for closing the connection; this function
    only handles the DB-side verify + lockout bookkeeping."""
    cred = (await db.execute(
        select(AgentCredential).where(AgentCredential.credential_id == credential_id)
    )).scalar_one_or_none()
    if cred is None:
        return None

    now = datetime.now(timezone.utc)
    locked_until = cred.locked_until
    if locked_until is not None and (locked_until if locked_until.tzinfo else locked_until.replace(tzinfo=timezone.utc)) > now:
        return None
    if cred.status != AgentCredentialStatus.ACTIVE:
        return None

    installation = await db.get(AgentInstallation, cred.agent_installation_id)
    if installation is None or installation.status in (AgentInstallationStatus.REVOKED, AgentInstallationStatus.DISABLED):
        return None

    if not verify_password(credential_secret, cred.secret_hash):
        cred.failed_auth_count += 1
        if cred.failed_auth_count >= CREDENTIAL_LOCKOUT_THRESHOLD:
            cred.locked_until = now + timedelta(minutes=CREDENTIAL_LOCKOUT_MINUTES)
        await db.commit()
        return None

    cred.failed_auth_count = 0
    cred.locked_until = None
    cred.last_used_at = now
    installation.last_connected_at = now
    installation.last_heartbeat_at = now
    installation.last_seen_ip = source_ip
    await db.commit()
    await db.refresh(installation)
    await db.refresh(cred)
    return installation, cred


async def rotate_credential(
    db: AsyncSession, organization_id: int | None, installation_id: int, *, actor_id: int | None,
) -> tuple[AgentInstallation, str, str]:
    """Marks the current ACTIVE credential ROTATED (kept for audit, never
    deleted) and issues a new ACTIVE one. Deliberately and immediately
    disruptive — the caller (api/tally_agents.py) is responsible for
    force-disconnecting the agent if it's currently connected, since its
    in-memory session is now using a credential this function has just
    made non-ACTIVE. Returns (installation, new_credential_id, new_credential_secret) —
    the plaintext secret is never stored and never retrievable again."""
    installation = await get_installation(db, organization_id, installation_id)
    if installation is None:
        raise TallyAgentServiceError("Agent installation not found.")
    if installation.status != AgentInstallationStatus.ENROLLED:
        raise TallyAgentServiceError(f"Cannot rotate credentials for an installation that is {installation.status.value.lower()}.")

    current = (await db.execute(
        select(AgentCredential).where(AgentCredential.agent_installation_id == installation.id, AgentCredential.status == AgentCredentialStatus.ACTIVE)
    )).scalars().first()
    now = datetime.now(timezone.utc)
    if current is not None:
        current.status = AgentCredentialStatus.ROTATED
        current.rotated_at = now

    credential_id = uuid.uuid4().hex
    credential_secret = secrets.token_urlsafe(32)
    db.add(AgentCredential(
        organization_id=organization_id, agent_installation_id=installation.id,
        credential_id=credential_id, secret_hash=hash_password(credential_secret),
        status=AgentCredentialStatus.ACTIVE, created_by=actor_id,
    ))
    await db.commit()
    await db.refresh(installation)

    await record_event(
        db, organization_id=organization_id, actor_id=actor_id, action="Agent Credential Rotated",
        module="Tally Agent", resource="AgentInstallation", resource_id=str(installation.id),
        status=AuditStatus.SUCCESS,
    )
    return installation, credential_id, credential_secret


async def revoke_installation(
    db: AsyncSession, organization_id: int | None, installation_id: int, *, actor_id: int | None, reason: str | None = None,
) -> AgentInstallation:
    """Permanently disables an installation — no un-revoke in v1;
    re-permitting means a fresh enrollment (see models/tally_agent.py's
    own docstring). Revokes every non-already-revoked credential, not
    just the current ACTIVE one, so a ROTATED-but-not-yet-expired
    credential (if that ever becomes a thing) can't be used either."""
    installation = await get_installation(db, organization_id, installation_id)
    if installation is None:
        raise TallyAgentServiceError("Agent installation not found.")
    if installation.status == AgentInstallationStatus.REVOKED:
        raise TallyAgentServiceError("This installation is already revoked.")

    now = datetime.now(timezone.utc)
    credentials = (await db.execute(
        select(AgentCredential).where(AgentCredential.agent_installation_id == installation.id, AgentCredential.status != AgentCredentialStatus.REVOKED)
    )).scalars().all()
    for cred in credentials:
        cred.status = AgentCredentialStatus.REVOKED
        cred.revoked_at = now

    installation.status = AgentInstallationStatus.REVOKED
    installation.revoked_at = now
    installation.revoked_reason = reason
    await db.commit()
    await db.refresh(installation)

    await record_event(
        db, organization_id=organization_id, actor_id=actor_id, action="Agent Revoked",
        module="Tally Agent", resource="AgentInstallation", resource_id=str(installation.id),
        status=AuditStatus.SUCCESS, after_value={"reason": reason},
    )
    return installation
