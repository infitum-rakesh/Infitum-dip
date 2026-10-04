"""
Tally Integration — Phase A (connection + company discovery only).

Reuses the existing Connections framework end to end: a TallyPrimeConnector
is built through connection_service.build_connector() exactly like every
other connector (Postgres, Salesforce, SFTP, ...), so Test Connection,
credential encryption, secret scrubbing and org-scoping all already apply
with zero new code here. This module only adds the one operation that's
genuinely Tally-specific and not already covered by the generic connection
API: listing the real companies open on a Tally instance so a user can pick
one to target, rather than typing a company name blind.

Later Tally phases (extraction, reconciliation, voucher write-back) will
add their own service modules; this one stays scoped to Phase A on purpose.
"""
import asyncio

from app.connectors.base import ConnectorError, ConnectorUnsupportedError, ConnectorValidationError
from app.models.connection import Connection, ConnectionType
from app.services import connection_service

# Same rationale/value as connection_service.BROWSE_TIMEOUT_SECONDS — a
# background safety net for a genuinely stuck HTTP call to Tally's local
# gateway, well above any real interactive wait.
DISCOVERY_TIMEOUT_SECONDS = 60


def _require_tally(conn: Connection) -> None:
    if conn.connection_type != ConnectionType.TALLY_PRIME:
        raise ConnectorValidationError("This operation is only available for TallyPrime connections.")


async def discover_companies(conn: Connection) -> list[dict]:
    """Returns every company TallyPrime currently reports, via the
    connector's real list_companies() (TDL Collection export) — never a
    cached or previously-typed value. Raises ConnectorError/
    ConnectorValidationError/ConnectorUnsupportedError for the API layer
    to translate into the right HTTP status, per this integration's
    no-fake-success discipline (see connectors/application/tallyprime.py)."""
    _require_tally(conn)
    connector = connection_service.build_connector(conn)
    try:
        return await asyncio.wait_for(asyncio.to_thread(connector.list_companies), timeout=DISCOVERY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise ConnectorError(
            f"Timed out after {DISCOVERY_TIMEOUT_SECONDS}s waiting on TallyPrime's gateway to list companies — "
            "confirm Tally is running with the HTTP server enabled and reachable at the configured host/port."
        ) from exc
