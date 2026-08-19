"""
connectors — the integration layer for every external data source/target
this platform talks to (databases today; files/SFTP/API land in later
phases of the data-integration build).

Every connector implements connectors.base.BaseConnector. Nothing outside
this package should import a driver library (psycopg2, pymysql, ...)
directly — connection_service and, later, the ingestion/loading layers go
through connectors.registry.get_connector() so the set of supported
connectors stays a single, swappable seam instead of scattered imports.
"""
