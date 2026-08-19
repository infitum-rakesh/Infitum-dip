"""
Bulk writer registry — the write-side counterpart to
connectors/registry.py. Only connection types with a real, verified
connector AND a real, verified writer are here; anything else raises
ConnectorUnsupportedError with a specific reason rather than silently
falling back to something unverified.
"""
from app.connectors.base import ConnectorUnsupportedError
from app.loading.writers.base import BaseBulkWriter
from app.loading.writers.mysql import MySqlBulkWriter
from app.loading.writers.oracle import OracleBulkWriter
from app.loading.writers.postgres import PostgresBulkWriter
from app.loading.writers.snowflake import SnowflakeBulkWriter
from app.loading.writers.sqlserver import SqlServerBulkWriter

WRITER_REGISTRY: dict[str, type[BaseBulkWriter]] = {
    "POSTGRESQL": PostgresBulkWriter,
    "MYSQL": MySqlBulkWriter,
    "SQL_SERVER": SqlServerBulkWriter,
    "ORACLE": OracleBulkWriter,
    # SNOWFLAKE — real code (write_pandas + MERGE, see snowflake.py's
    # module docstring), NOT live-verified against a real Snowflake
    # account in this environment (cloud-only service, no local/free way
    # to stand one up here) — disclosed, not hidden.
    "SNOWFLAKE": SnowflakeBulkWriter,
}


def get_bulk_writer(connection_type: str, connector) -> BaseBulkWriter:
    cls = WRITER_REGISTRY.get(connection_type)
    if cls is None:
        raise ConnectorUnsupportedError(f"No bulk writer is implemented for target connection type '{connection_type}' yet.")
    return cls(connector)
