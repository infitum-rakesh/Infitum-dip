"""
tally_agent.py — Multi-Client TallyPrime Integration Platform (Agent
Relay). A customer whose TallyPrime machine is behind NAT/a firewall
installs a lightweight Windows agent there; the agent opens an outbound,
authenticated WebSocket to this server (see app/ws/tally_agent_registry.py
and app/api/ws_tally_agent.py) so the server can route Tally operations
to it without any inbound port on the customer's network. This is a
SECOND transport alongside the existing direct-HTTP TallyPrime
integration (connectors/application/tallyprime.py) — see
connectors/application/tallyprime_agent.py, which gives
AgentAwareTallyConnector the identical method signatures/return shapes
as TallyPrimeConnector so the existing extraction/reconciliation/
write-back pipeline needs no changes to use either.

Design notes:
 - AgentInstallation is the device record — one row per customer Windows
   machine running the agent. "Is this agent connected right now" is
   answered by the in-memory registry (app/ws/tally_agent_registry.py)
   while this process is up, cross-checked against last_heartbeat_at
   staleness for the crash-recovery edge case — deliberately NOT a
   second, DB-backed "live session" table (would just be a slower,
   harder-to-keep-consistent copy of what the registry already knows).
 - AgentCredential is separate from AgentInstallation so rotation
   leaves an audit trail (ROTATED rows kept) instead of overwriting
   state in place. The credential secret is bcrypt-hashed (same
   primitive as User.hashed_password, see app/core/security.py) —
   never Fernet/reversible, since the server only ever verifies it,
   never needs to redisplay it.
 - EnrollmentToken is a one-time, short-lived credential exchange
   token — the plaintext token is returned exactly once at creation
   and never stored or retrievable again, only its bcrypt hash.
 - AgentCommand is the audit/diagnostics trail for one dispatched
   command (what did we ask the agent, what did it say back, how
   long did it take) — NOT a data store; the actual voucher/ledger
   payload flows straight back to the calling service, which persists
   it in its own tables (TallyExtractionRecord etc.) exactly as the
   direct-HTTP path already does today. Status is written by exactly
   one coroutine per row (the dispatcher that owns that request
   end-to-end), never contended by concurrent workers the way
   BulkMigrationTask is — so plain sequential updates are used here,
   not the claim-task CAS pattern bulk_migration_service.py needs.
 - No heartbeat-history table — heartbeats only ever update
   AgentInstallation's own rolling last_heartbeat_at column. Discrete
   lifecycle events (enrolled/connected/disconnected/revoked/rotated)
   go to the existing AuditLog table instead (module="Tally Agent"),
   same as every other feature's audit trail.
 - No TallyCompany cache table — list_companies() stays a live call
   through the agent every time, matching TallyPrimeConnector's own
   uncached behavior exactly, so the two transports have identical
   semantics (no transport-specific staleness/invalidation rules to
   get wrong).
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class AgentInstallationStatus(str, enum.Enum):
    PENDING_ENROLLMENT = "PENDING_ENROLLMENT"
    ENROLLED = "ENROLLED"
    REVOKED = "REVOKED"
    DISABLED = "DISABLED"


class AgentCredentialStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ROTATED = "ROTATED"
    REVOKED = "REVOKED"


class AgentCommandStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class AgentCommandSource(str, enum.Enum):
    INTERACTIVE = "INTERACTIVE"
    EXTRACTION = "EXTRACTION"
    RECONCILIATION = "RECONCILIATION"
    WRITEBACK = "WRITEBACK"
    HEALTH_CHECK = "HEALTH_CHECK"


class AgentInstallation(TenantBase):
    """One row per customer Windows machine running the agent — the
    device record administrators create, enroll, monitor, and revoke."""
    __tablename__ = "agent_installations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)  # admin-given label, e.g. "Mumbai Office Tally"
    status: Mapped[AgentInstallationStatus] = mapped_column(Enum(AgentInstallationStatus), default=AgentInstallationStatus.PENDING_ENROLLMENT, index=True)

    # Self-reported by the agent at connect time — display/diagnostics only, never trusted for authorization.
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Where TallyPrime's own HTTP gateway listens ON THE AGENT'S OWN
    # MACHINE — lives here (not per-Connection) because several
    # TALLY_PRIME_AGENT Connections can route through the same physical
    # agent/Tally instance for different companies; duplicating host/port
    # per-Connection would let them drift inconsistently.
    tally_host: Mapped[str] = mapped_column(String(255), default="127.0.0.1")
    tally_port: Mapped[int] = mapped_column(Integer, default=9000)

    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_seen_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)  # no FK — User is PlatformBase, this table is TenantBase
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:
        return f"<AgentInstallation {self.name} ({self.status})>"


class AgentCredential(TenantBase):
    """Separate from AgentInstallation so rotation leaves history
    (ROTATED rows kept) instead of overwriting state in place. Only
    secret_hash is ever stored — the plaintext credential_secret is
    returned exactly once, at enrollment or rotation time, never again."""
    __tablename__ = "agent_credentials"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    agent_installation_id: Mapped[int] = mapped_column(ForeignKey("agent_installations.id"), nullable=False, index=True)
    credential_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)  # uuid4 hex — the public identifier the agent presents
    secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # bcrypt — app.core.security.hash_password/verify_password

    status: Mapped[AgentCredentialStatus] = mapped_column(Enum(AgentCredentialStatus), default=AgentCredentialStatus.ACTIVE, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    failed_auth_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # persistent brute-force lockout, survives process restarts

    created_by: Mapped[int | None] = mapped_column(nullable=True)

    def __repr__(self) -> str:
        return f"<AgentCredential {self.credential_id} ({self.status})>"


class EnrollmentToken(TenantBase):
    """A one-time, short-lived token an admin generates and hands to
    whoever is installing the agent — exchanged exactly once for a real
    AgentCredential (see api/tally_agents.py's enroll route). The
    plaintext token is returned once at creation and never stored."""
    __tablename__ = "agent_enrollment_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    agent_installation_id: Mapped[int] = mapped_column(ForeignKey("agent_installations.id"), nullable=False, index=True)
    # SHA-256 hex digest, not bcrypt — deliberately different from
    # AgentCredential.secret_hash. bcrypt's slow-hash defense matters for
    # LOW-entropy secrets (passwords); this token is secrets.token_urlsafe(32)
    # (256 bits of randomness), already infeasible to brute-force, so a
    # fast deterministic hash is used instead specifically so the token
    # can be looked up directly (WHERE token_hash = sha256(presented)) —
    # same reasoning most bearer-token systems use. Indexed for that lookup.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)  # token permanently dead past a threshold, independent of source IP

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<EnrollmentToken installation={self.agent_installation_id}>"


class AgentCommand(TenantBase):
    """One row per command dispatched through the agent — the audit/
    diagnostics trail for AgentAwareTallyConnector, not a data store
    (see module docstring). request_id is the correlation id shared
    with the WebSocket protocol's command/command_result messages."""
    __tablename__ = "agent_commands"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    agent_installation_id: Mapped[int] = mapped_column(ForeignKey("agent_installations.id"), nullable=False, index=True)
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id"), nullable=True)

    request_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[AgentCommandStatus] = mapped_column(Enum(AgentCommandStatus), default=AgentCommandStatus.PENDING, index=True)
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(nullable=True)

    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # small, e.g. {"voucher_count": 42} — never the full payload

    initiated_by: Mapped[int | None] = mapped_column(nullable=True)  # user id; null for system-initiated (e.g. a scheduled extraction)
    source: Mapped[AgentCommandSource] = mapped_column(Enum(AgentCommandSource), default=AgentCommandSource.INTERACTIVE)

    def __repr__(self) -> str:
        return f"<AgentCommand {self.operation} ({self.status})>"
