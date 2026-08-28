"""
TenantDatabase — per-organization (or platform-level) "bring your own
database" configuration. One row per org that's configured BYO-DB, plus
at most one row with organization_id = NULL for the Seller/platform-level
config — the same organization_id is None -> platform-wide convention
already used by reconciliation_service/validation_service/sql_workspace_
service, rather than a second, parallel SellerDatabase model.

This is Phase 1 of the BYO-Database feature (see the plan file written to
.claude/plans/inherited-inventing-harp.md at design time): config + real
Test Connection only. Nothing reads TenantDatabase to actually ROUTE a
request yet (that's Phase 2's PlatformBase/TenantBase split + tenant
engine registry) — is_active starts False and only flips True once a real
migration/cutover (Phase 4) completes, so this model carries zero risk to
any existing request path.

Same config/encrypted_secrets split as Connection (models/connection.py),
same Fernet boundary via app.core.crypto (already shared by
Connection.encrypted_secrets and Seller.smtp_config). connection_type is
deliberately restricted (at the service layer) to POSTGRESQL/MYSQL/
SQL_SERVER — the three dialects this platform's own control-plane schema
(TenantBase.metadata, once it exists in Phase 2) can actually be created
against via SQLAlchemy — reusing models.connection.ConnectionType's string
values rather than inventing a parallel enum.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class TenantDatabaseStatus(str, enum.Enum):
    UNCONFIGURED = "UNCONFIGURED"  # no row saved yet (service-layer default; never persisted as such — get_or_create creates the row on first save)
    UNTESTED = "UNTESTED"          # config saved, Test Connection never run (or run and then config changed again)
    ACTIVE = "ACTIVE"              # last Test Connection succeeded
    ERROR = "ERROR"                # last Test Connection failed
    MIGRATING = "MIGRATING"        # Phase 4 — cutover in progress; get_tenant_db briefly gates writes for this org


class TenantDatabase(PlatformBase):
    __tablename__ = "tenant_databases"
    __table_args__ = (UniqueConstraint("organization_id", name="uq_tenant_database_org"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # NULL = the Seller/platform-level row. FK only, no relationship() —
    # additive, same convention as Connection.organization_id.
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, unique=True, index=True)

    connection_type: Mapped[str] = mapped_column(String(30), nullable=False)  # POSTGRESQL | MYSQL | SQL_SERVER

    # Non-secret configuration (host, port, database, username, ssl_mode, ...).
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # {field_name: fernet_ciphertext} — never serialized to the API as-is.
    encrypted_secrets: Mapped[dict] = mapped_column(JSON, default=dict)
    secrets_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[TenantDatabaseStatus] = mapped_column(Enum(TenantDatabaseStatus), default=TenantDatabaseStatus.UNTESTED, index=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # True only once a completed migration/cutover (Phase 4) has pointed
    # this org's real traffic at this database. Phase 1 never sets this —
    # configuring and testing a database here has no effect on where this
    # org's actual requests are served from until that later phase exists.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<TenantDatabase org={self.organization_id} {self.connection_type} {self.status}>"
