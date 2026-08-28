"""
TenantDatabaseService — Phase 1 of Bring-Your-Own-Database: CRUD + a REAL
Test Connection for an org's (or the Seller's platform-level) chosen
backend database, exactly one implementation shared by both callers via
an `organization_id: int | None` parameter — the same org-vs-platform-
scope convention reconciliation_service/validation_service already use
(organization_id is None means the Seller/platform-wide row), rather than
a second, copy-pasted service for the Seller case.

Test Connection deliberately REUSES the existing, already-live-verified
PostgresConnector/MySqlConnector/SqlServerConnector (connectors/database/
postgres.py, mysql.py, sqlserver.py) via connectors.registry.get_connector
— the same real, driver-backed SQLAlchemy-engine-based test connection.py
already runs for Connections — rather than writing a second, parallel set
of "one-shot async connector" classes. It's the identical operation
(build an engine, SELECT a version string, report real latency, dispose),
so reusing the tested code is strictly better than duplicating it. Nothing
here reads or writes Connection rows — TenantDatabase is its own table,
deliberately not a Connection, since a Connection is soft-deletable/
cloneable ETL-source metadata and this is control-plane routing config
with a very different lifecycle (Phase 2 onward will start reading
is_active/status here from request-handling code, never true for a
Connection row).

Security boundary: this is the ONLY module that touches
TenantDatabase.encrypted_secrets or a plaintext secret value derived from
it — same rule as connection_service.py. Every dict this module returns to
a caller already has secrets masked.
"""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.connectors.base import ConnectorUnsupportedError
from app.connectors.database.pool_manager import evict as evict_pool
from app.connectors.registry import get_connector, is_registered_connector
from app.core import crypto
from app.models.audit_log import AuditStatus, record_event
from app.models.seller_audit_log import record_seller_event
from app.models.tenant_database import TenantDatabase, TenantDatabaseStatus
from app.services.connection_service import FIELD_SCHEMAS, secret_keys_for

MASK = "••••••••"

# Only the three dialects this platform's own control-plane schema can be
# created against (Phase 2's TenantBase.metadata.create_all()) — a
# deliberately narrower set than the full Connections catalogue.
ALLOWED_TYPES = {"POSTGRESQL", "MYSQL", "SQL_SERVER"}


class TenantDatabaseValidationError(Exception):
    """Raised for an unsupported connection_type or missing required fields."""


def _schema_for(connection_type: str) -> dict:
    if connection_type not in ALLOWED_TYPES:
        raise TenantDatabaseValidationError(
            f"'{connection_type}' isn't supported as a backend database — choose PostgreSQL, MySQL, or SQL Server."
        )
    return FIELD_SCHEMAS[connection_type]


def _validate_required(connection_type: str, config: dict, encrypted_secrets: dict) -> list[str]:
    missing = []
    for f in _schema_for(connection_type)["fields"]:
        if not f.get("required"):
            continue
        show_if = f.get("show_if")
        if show_if and config.get(show_if["field"]) != show_if["equals"]:
            continue
        if f.get("secret"):
            if not (encrypted_secrets or {}).get(f["key"]):
                missing.append(f["label"])
        elif not config.get(f["key"]) and config.get(f["key"]) != 0:
            missing.append(f["label"])
    return missing


def _split_config(connection_type: str, raw_config: dict) -> tuple[dict, dict]:
    secret_keys = secret_keys_for(connection_type)
    non_secret = {k: v for k, v in raw_config.items() if k not in secret_keys}
    secrets = {k: v for k, v in raw_config.items() if k in secret_keys and v not in (None, "")}
    return non_secret, secrets


def _masked_config(connection_type: str, config: dict, encrypted_secrets: dict) -> dict:
    masked = dict(config)
    for key in secret_keys_for(connection_type):
        masked[key] = MASK if (encrypted_secrets or {}).get(key) else ""
    return masked


# ── CRUD ─────────────────────────────────────────────────────────

async def get_row(db: AsyncSession, organization_id: int | None) -> TenantDatabase | None:
    stmt = select(TenantDatabase).where(
        TenantDatabase.organization_id.is_(None) if organization_id is None
        else TenantDatabase.organization_id == organization_id
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def update_config(
    db: AsyncSession, *, organization_id: int | None, connection_type: str, raw_config: dict, actor_id: int,
    ip_address: str | None = None,
) -> TenantDatabase:
    _schema_for(connection_type)  # raises if unsupported
    non_secret, secrets = _split_config(connection_type, raw_config)
    encrypted_new = {k: crypto.encrypt_secret(v) for k, v in secrets.items()}

    row = await get_row(db, organization_id)
    is_new = row is None
    if is_new:
        row = TenantDatabase(
            organization_id=organization_id, connection_type=connection_type,
            config=non_secret, encrypted_secrets=encrypted_new,
            secrets_rotated_at=datetime.now(timezone.utc) if encrypted_new else None,
            status=TenantDatabaseStatus.UNTESTED,
        )
        db.add(row)
    else:
        type_changed = row.connection_type != connection_type
        row.connection_type = connection_type
        # A type change starts config fresh rather than merging fields from
        # a different dialect's schema (e.g. a leftover MySQL ssl_mode value
        # under a now-Postgres row) — same "changing shape invalidates the
        # old shape" reasoning as Connection.update_connection, applied one
        # step further since here it's the whole row, not just secrets.
        row.config = non_secret if type_changed else {**row.config, **non_secret}
        flag_modified(row, "config")
        if encrypted_new:
            row.encrypted_secrets = {} if type_changed else dict(row.encrypted_secrets or {})
            row.encrypted_secrets = {**row.encrypted_secrets, **encrypted_new}
            flag_modified(row, "encrypted_secrets")
            row.secrets_rotated_at = datetime.now(timezone.utc)
        elif type_changed:
            row.encrypted_secrets = {}
            flag_modified(row, "encrypted_secrets")
        # Any config/type/secret change invalidates whatever the last Test
        # Connection proved — never let a stale ACTIVE/ERROR status imply
        # something about credentials that just changed.
        row.status = TenantDatabaseStatus.UNTESTED
        row.last_tested_at = None
        row.last_test_success = None
        row.last_test_message = None

    await db.commit()
    await db.refresh(row)

    # Any config/secret/type change invalidates a pooled engine opened
    # under the old values — never let a stale pool keep answering (or
    # keep failing) for what's now different credentials. Same rule
    # connection_service.update_connection() applies to Connection rows.
    if not is_new:
        evict_pool(f"tenantdb:{row.id}")
        # Local import — tenant_engine_registry imports this module at
        # top level (to look up TenantDatabase rows), so importing it back
        # up here at module scope would be circular.
        from app.database import tenant_engine_registry
        tenant_engine_registry.evict(organization_id)

    await _audit(
        db, organization_id=organization_id, actor_id=actor_id, ip_address=ip_address,
        action="Tenant Database Configured" if is_new else "Tenant Database Updated",
        details=f"connection_type={connection_type}",
    )
    return row


async def test_connection(db: AsyncSession, row: TenantDatabase, *, actor_id: int, ip_address: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    connection_type = row.connection_type
    missing = _validate_required(connection_type, row.config, row.encrypted_secrets)

    latency_ms: float | None = None
    server_version: str | None = None

    if missing:
        success = False
        message = f"Missing required field(s): {', '.join(missing)}."
    elif not is_registered_connector(connection_type):
        success = False
        message = f"'{connection_type}' has no connector registered."
    else:
        secret_values = {
            key: crypto.decrypt_secret((row.encrypted_secrets or {}).get(key))
            for key in secret_keys_for(connection_type)
        }
        secret_values = {k: v for k, v in secret_values.items() if v is not None}
        try:
            connector = get_connector(connection_type, row.config, secret_values, cache_key=f"tenantdb:{row.id}")
            result = await asyncio.to_thread(connector.test_connection)
        except ConnectorUnsupportedError as exc:
            success, message = False, str(exc)
        else:
            success, message = result.success, result.message
            latency_ms, server_version = result.latency_ms, result.server_version

    row.last_tested_at = now
    row.last_test_success = success
    row.last_test_message = message
    row.status = TenantDatabaseStatus.ACTIVE if success else TenantDatabaseStatus.ERROR
    await db.commit()
    await db.refresh(row)

    await _audit(
        db, organization_id=row.organization_id, actor_id=actor_id, ip_address=ip_address,
        action="Tenant Database Tested", details=message,
        status=AuditStatus.SUCCESS if success else AuditStatus.FAILURE,
    )
    return {
        "success": success, "message": message, "tested_at": now, "status": row.status.value,
        "latency_ms": latency_ms, "server_version": server_version,
    }


async def _audit(
    db: AsyncSession, *, organization_id: int | None, actor_id: int, action: str, details: str | None,
    ip_address: str | None = None, status: AuditStatus = AuditStatus.SUCCESS,
) -> None:
    """Org-initiated changes go through the existing AuditLog (module=
    "Settings", same as settings_service.py); the Seller/platform-level row
    (organization_id is None) goes through SellerAuditLog instead — both
    already-real, already-displayed audit tables, no new one needed."""
    if organization_id is not None:
        await record_event(
            db, organization_id=organization_id, actor_id=actor_id, action=action, module="Settings",
            resource="Backend Database", status=status, ip_address=ip_address, details=details,
        )
    else:
        await record_seller_event(
            db, seller_user_id=actor_id, customer_id=None, action=action,
            resource="Platform Backend Database", resource_id=None, ip_address=ip_address, details=details,
        )


def to_out(row: TenantDatabase | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id, "organization_id": row.organization_id, "connection_type": row.connection_type,
        "config": _masked_config(row.connection_type, row.config, row.encrypted_secrets),
        "status": row.status.value,
        "last_tested_at": row.last_tested_at, "last_test_success": row.last_test_success,
        "last_test_message": row.last_test_message, "secrets_rotated_at": row.secrets_rotated_at,
        "is_active": row.is_active, "activated_at": row.activated_at,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def type_catalogue() -> dict:
    """Same shape as connection_service.get_type_catalogue(), narrowed to
    the three dialects this feature supports — drives the frontend's
    connection-type dropdown from one source of truth rather than a second
    hardcoded field list."""
    return {t: FIELD_SCHEMAS[t] for t in ALLOWED_TYPES}
