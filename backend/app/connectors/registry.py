"""
Connector registry — the single place that maps
Connection.connection_type -> a real connector implementation.

Only types in CONNECTOR_REGISTRY have a driver-backed connector — every
one of them makes genuine calls against the real protocol/SDK/API it
claims to (no simulated success/failure branch anywhere in this
codebase). Within that set, each entry's own comment below (and its
module's own docstring) discloses whether it was additionally exercised
against a real, LIVE instance/account during development, or is real
code that's correct against the vendor's own documented contract but
untested live because no free/local emulator or account exists in this
environment (Snowflake, SAP, Fabric, Salesforce — all cloud/enterprise-
only services). Both are "real"; only the second is "unverified" in the
live-testing sense.

UNVERIFIED (currently empty) is reserved for a connector whose interface
is defined but genuinely has no implementation behind it yet —
get_connector() raises ConnectorUnsupportedError with a specific
explanation for those, rather than silently pretending to work. See
connectors/base.py's ConnectorUnsupportedError and app/services/
connection_service.py's test_connection() for how the API surfaces this
(status "UNSUPPORTED", not "ERROR" — those mean different things).

Current coverage by category (see CATEGORIES in connection_service.py):
DATABASE (Postgres/MySQL/SQL Server/Oracle live-verified; Snowflake/SAP/
Fabric real-but-unverified), STORAGE (SFTP/FTP/Local File/Network File,
all live-verified), API (REST/SOAP, both live-verified against a real
local test server), APPLICATION (NetSuite SuiteQL+SOAP real-but-
unverified against a live NetSuite account; Salesforce real-but-
unverified against a live org), CLOUD (Azure live-verified via Azurite,
AWS live-verified via LocalStack), AI_PROVIDER (Ollama live-verified;
OpenAI/Anthropic real-but-unverified against a funded account),
OCR_PROVIDER (AWS Textract/Google Vision/Azure Document Intelligence —
all three real-but-unverified: no free/local emulator exists for any of
them, same disclosed-but-real precedent as Snowflake/SAP/Fabric above).
"""
from app.connectors.ai.anthropic_connector import AnthropicConnector
from app.connectors.ai.ollama_connector import OllamaConnector
from app.connectors.ai.openai_connector import OpenAiConnector
from app.connectors.api.rest import RestApiConnector
from app.connectors.api.soap import SoapApiConnector
from app.connectors.application.netsuite_soap import NetSuiteSoapConnector
from app.connectors.ocr.aws_textract import AwsTextractConnector
from app.connectors.ocr.azure_document_intelligence import AzureDocumentIntelligenceConnector
from app.connectors.ocr.google_vision import GoogleVisionConnector
from app.connectors.application.salesforce import SalesforceConnector
from app.connectors.base import BaseConnector, ConnectorUnsupportedError
from app.connectors.cloud.aws_s3 import AwsS3Connector
from app.connectors.cloud.azure_blob import AzureBlobConnector
from app.connectors.database.fabric import FabricConnector
from app.connectors.database.mysql import MySqlConnector
from app.connectors.database.netsuite_suiteql import NetSuiteSuiteQlConnector
from app.connectors.database.oracle import OracleConnector
from app.connectors.database.postgres import PostgresConnector
from app.connectors.database.sap import SapConnector
from app.connectors.database.snowflake import SnowflakeConnector
from app.connectors.database.sqlserver import SqlServerConnector
from app.connectors.database.vertica import VerticaConnector
from app.connectors.files.local import LocalFileConnector
from app.connectors.ftp.client import FtpConnector
from app.connectors.sftp.client import SftpConnector
from app.connectors.smb.client import NetworkFileConnector

CONNECTOR_REGISTRY: dict[str, type[BaseConnector]] = {
    "POSTGRESQL": PostgresConnector,
    "MYSQL": MySqlConnector,
    "SQL_SERVER": SqlServerConnector,
    "ORACLE": OracleConnector,
    # SNOWFLAKE — real code, not live-verified (no live account available
    # here) — see this module's docstring above and connectors/database/
    # snowflake.py's own docstring.
    "SNOWFLAKE": SnowflakeConnector,
    "SFTP": SftpConnector,
    # FTP — real, ftplib-based (stdlib, no extra dependency), live-verified
    # against a Docker vsftpd instance including its LIST-based listing
    # fallback (see connectors/ftp/client.py's docstring — that server
    # genuinely doesn't support MLSD).
    "FTP": FtpConnector,
    # NETWORK_FILE (SMB/UNC share) — real, smbprotocol-based, live-
    # verified against a Docker Samba instance (see connectors/smb/
    # client.py's docstring for a real, non-obvious port-kwarg gotcha
    # found and fixed during that verification).
    "NETWORK_FILE": NetworkFileConnector,
    "LOCAL_FILE": LocalFileConnector,
    "REST": RestApiConnector,
    # SOAP — real, zeep-based generic WSDL client, live-verified against a
    # hand-written but protocol-valid local WSDL 1.1/SOAP 1.1 mock service
    # (real WSDL fetch, real SOAP request/response, real fault handling —
    # see connectors/api/soap.py's docstring).
    "SOAP": SoapApiConnector,
    # NetSuite — real code (pyodbc DSN for SuiteQL; TBA-signed SOAP for
    # SuiteTalk saved searches, verified against NetSuite's own real WSDL
    # during development — see each module's docstring), not live-
    # verified against an actual NetSuite account (none available here).
    "NETSUITE_SUITEQL": NetSuiteSuiteQlConnector,
    "NETSUITE_SOAP": NetSuiteSoapConnector,
    # SALESFORCE — real simple-salesforce REST/SOQL client. Field schema
    # was corrected this session (see connection_service.py's SALESFORCE
    # entry) from a broken NetSuite-TBA-shaped set to Salesforce's actual
    # username/password/security_token(+ optional Connected App) auth
    # model. NOT live-verified — no Salesforce org/account exists in this
    # environment; real code against simple-salesforce's own installed
    # source (exact constructor kwargs, exact exception classes — see
    # connectors/application/salesforce.py's docstring), same disclosed-
    # but-real precedent as SNOWFLAKE/SAP/FABRIC above.
    "SALESFORCE": SalesforceConnector,
    # AI Provider connections (Document Intelligence AI Extraction) — real
    # HTTP-based connectors; OLLAMA is what's live-verified in this build
    # (no external API key needed). OPENAI/ANTHROPIC follow the same real
    # API contract but weren't exercised against a funded account here.
    "OPENAI": OpenAiConnector,
    "ANTHROPIC": AnthropicConnector,
    "OLLAMA": OllamaConnector,
    # AZURE (Blob Storage) — real azure-storage-blob SDK calls, live-
    # verified against a local Azurite emulator (Connection String auth
    # path only — Access Key/Service Principal are real SDK-contract
    # code, unverified against an actual Azure subscription; see
    # connectors/cloud/azure_blob.py's docstring).
    "AZURE": AzureBlobConnector,
    # AWS (S3 only — see field help text on why Redshift/RDS reuse the
    # existing DB connectors instead) — real boto3 calls, live-verified
    # against a local LocalStack 3.x emulator via endpoint_url.
    "AWS": AwsS3Connector,
    # SAP — real hana+hdbcli SQLAlchemy dialect (sqlalchemy-hana wrapping
    # SAP's own official hdbcli driver), same SqlAlchemyDatabaseConnector
    # base Oracle/Snowflake use. NOT live-verified — no free/local SAP
    # HANA emulator exists (HANA Express needs a large license-gated
    # download unavailable here); real code against HANA's own documented
    # connection contract, same disclosed-but-real precedent as SNOWFLAKE.
    # Scoped to HANA only — an ABAP/RFC-only SAP system needs the
    # proprietary NetWeaver RFC SDK, which isn't pip-installable at all;
    # see connectors/database/sap.py's docstring.
    "SAP": SapConnector,
    # FABRIC — real mssql+pyodbc dialect + azure-identity AAD token auth
    # (ROPC or Service Principal, injected via a do_connect listener for
    # per-connection token refresh — see connectors/database/fabric.py's
    # docstring). NOT live-verified — Fabric is a cloud-only, license-
    # gated service with no free/local emulator; real code against
    # Microsoft's own documented Azure SQL/Fabric AAD-token contract,
    # same disclosed-but-real precedent as SNOWFLAKE/SAP above.
    "FABRIC": FabricConnector,
    # VERTICA — real sqlalchemy-vertica-python dialect (wraps vertica-
    # python). NOT live-verified — no free/local Vertica emulator exists
    # (the official Community Edition image is a large, license-accepting
    # download); real code against Vertica's own documented SQL reference,
    # same disclosed-but-real precedent as SNOWFLAKE/SAP/FABRIC above.
    "VERTICA": VerticaConnector,
    # OCR Provider connections (Documents module — organization's OCR
    # Provider setting; see app/ocr/engine.py + services/document_service.py).
    # Tesseract remains the zero-config default and isn't a Connection at
    # all. None of these three have a free/local emulator available in
    # this environment (Textract's LocalStack support is Pro-only; Vision
    # and Document Intelligence have none) — real code against each
    # vendor's own documented SDK contract, same disclosed-but-real
    # precedent as SNOWFLAKE/SAP/FABRIC/SALESFORCE above.
    "AWS_TEXTRACT": AwsTextractConnector,
    "GOOGLE_VISION": GoogleVisionConnector,
    "AZURE_FORM_RECOGNIZER": AzureDocumentIntelligenceConnector,
}

UNVERIFIED: dict[str, str] = {}


def is_registered_connector(connection_type: str) -> bool:
    """True for any type this registry is responsible for, whether
    verified or not — used by connection_service to decide whether to
    attempt a real connector at all vs. leave unregistered categories
    (API/Cloud) on their existing behavior."""
    return connection_type in CONNECTOR_REGISTRY or connection_type in UNVERIFIED


def get_connector(connection_type: str, config: dict, secrets: dict, cache_key: str | None = None) -> BaseConnector:
    """
    cache_key should be the caller's stable identifier for this specific
    Connection (connection_service passes f"conn:{conn.id}") — it becomes
    the pooling key in connectors/database/pool_manager.py. Passing None
    is only safe for connectors that don't pool (none currently do
    anything else); every database connector requires one in practice.
    """
    if connection_type in UNVERIFIED:
        raise ConnectorUnsupportedError(UNVERIFIED[connection_type])
    cls = CONNECTOR_REGISTRY.get(connection_type)
    if cls is None:
        raise ConnectorUnsupportedError(f"No connector is registered for connection type '{connection_type}'.")
    return cls(config, secrets, cache_key=cache_key)
