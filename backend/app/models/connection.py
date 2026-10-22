"""
Connection model — centralized connection management for databases,
applications, file/storage endpoints, APIs and cloud providers.

Credentials are never stored in plaintext: secret fields (passwords, keys,
tokens) live in `encrypted_secrets` as Fernet ciphertext, keyed by field
name; everything else lives in `config`. See services/connection_service.py
for the encryption boundary — no other module should read/write
`encrypted_secrets` directly.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ConnectionCategory(str, enum.Enum):
    DATABASE = "DATABASE"
    APPLICATION = "APPLICATION"
    STORAGE = "STORAGE"
    API = "API"
    CLOUD = "CLOUD"
    AI_PROVIDER = "AI_PROVIDER"
    OCR_PROVIDER = "OCR_PROVIDER"
    # India Business/Tax Identity/Corporate Document Verification module —
    # see connectors/verification/*. Pluggable like OCR_PROVIDER: this
    # category holds whichever verification vendor(s) are configured, the
    # built-in default being a local mock gateway (no real verification)
    # for development/testing — never implied as a live government/KYC
    # integration on its own.
    VERIFICATION_PROVIDER = "VERIFICATION_PROVIDER"


class ConnectionType(str, enum.Enum):
    # Database
    SQL_SERVER = "SQL_SERVER"
    POSTGRESQL = "POSTGRESQL"
    MYSQL = "MYSQL"
    ORACLE = "ORACLE"
    SNOWFLAKE = "SNOWFLAKE"
    SAP = "SAP"
    FABRIC = "FABRIC"
    VERTICA = "VERTICA"
    # Application
    SALESFORCE = "SALESFORCE"
    # NetSuite splits into two genuinely different integrations rather than
    # one generic type: SuiteQL over SuiteAnalytics Connect's ODBC driver
    # (tabular, DSN-based — real SQL browsing/preview/chunked reads) vs.
    # SuiteTalk SOAP web services (running an existing saved search by ID,
    # TBA-signed, XML/SOAP response). See connectors/database/
    # netsuite_suiteql.py and connectors/application/netsuite_soap.py.
    NETSUITE_SUITEQL = "NETSUITE_SUITEQL"
    NETSUITE_SOAP = "NETSUITE_SOAP"
    # Oracle Fusion ERP Cloud (Financials — Payables Invoices REST resource)
    # — see connectors/application/oracle_erp.py. Deliberately distinct
    # from ORACLE above, which is a plain Oracle Database driver
    # connection (SQL*Net/cx_Oracle), unrelated to ERP Cloud's REST APIs.
    ORACLE_ERP_CLOUD = "ORACLE_ERP_CLOUD"
    # TallyPrime — Indian SMB accounting software, integrated via its own
    # local XML-over-HTTP gateway (typically port 9000), not a SQL dialect
    # or a WSDL/SOAP service. See connectors/application/tallyprime.py.
    TALLY_PRIME = "TALLY_PRIME"
    # TallyPrime via the Agent Relay (Multi-Client TallyPrime Integration
    # Platform) — for a customer whose TallyPrime machine is behind NAT/a
    # firewall and not directly reachable from this backend. Routes
    # through a connected Windows agent over an authenticated WebSocket
    # instead of a direct HTTP call; same operation surface, same return
    # shapes as TALLY_PRIME — see connectors/application/tallyprime_agent.py
    # and models/tally_agent.py. A genuinely separate transport, not a
    # replacement: TALLY_PRIME keeps working unchanged for customers whose
    # Tally machine IS directly reachable.
    TALLY_PRIME_AGENT = "TALLY_PRIME_AGENT"
    # Storage
    SFTP = "SFTP"
    FTP = "FTP"
    LOCAL_FILE = "LOCAL_FILE"
    NETWORK_FILE = "NETWORK_FILE"
    # Email inbox intake (PAY19) — real IMAP client (stdlib imaplib/email,
    # no new dependency). Implements the same FileConnectorBase contract
    # as SFTP/FTP/NETWORK_FILE: each matching attachment across the
    # mailbox's messages is one "file" (list_files/open_read_stream/
    # move_file/file_checksum) — see connectors/email/imap_client.py.
    EMAIL_IMAP = "EMAIL_IMAP"
    # API
    REST = "REST"
    SOAP = "SOAP"
    # Cloud
    AZURE = "AZURE"
    AWS = "AWS"
    OTHER = "OTHER"
    # AI Provider
    OPENAI = "OPENAI"
    ANTHROPIC = "ANTHROPIC"
    OLLAMA = "OLLAMA"
    # OCR Provider (Documents module — Organization Settings' "OCR Provider"
    # dropdown selects between these and the built-in default, Tesseract,
    # which needs no connection at all). See connectors/ocr/*.
    AWS_TEXTRACT = "AWS_TEXTRACT"
    GOOGLE_VISION = "GOOGLE_VISION"
    AZURE_FORM_RECOGNIZER = "AZURE_FORM_RECOGNIZER"
    # Verification Provider (Compliance & Verification module) — a local
    # mock gateway today (scripts/mock_verification_gateway.py); real
    # vendors (a UIDAI-licensed AUA/KYC aggregator, GSTN/MCA-authorized
    # data providers, ...) get their own ConnectionType value here once
    # credentials for one are actually supplied — see
    # connectors/verification/base_verification.py's docstring.
    MOCK_VERIFICATION_PROVIDER = "MOCK_VERIFICATION_PROVIDER"


class ConnectionEnvironment(str, enum.Enum):
    PRODUCTION = "PRODUCTION"
    STAGING = "STAGING"
    DEVELOPMENT = "DEVELOPMENT"
    TEST = "TEST"


class ConnectionStatus(str, enum.Enum):
    UNTESTED = "UNTESTED"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
    UNSUPPORTED = "UNSUPPORTED"  # a real test was attempted but this connector isn't implemented yet — see connectors/registry.py
    INACTIVE = "INACTIVE"  # manually disabled


class Connection(TenantBase):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # FK only, no relationship() back-populates — additive, doesn't touch
    # models/organization.py or models/user.py.
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    owner_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[ConnectionCategory] = mapped_column(Enum(ConnectionCategory), nullable=False)
    connection_type: Mapped[ConnectionType] = mapped_column(Enum(ConnectionType), nullable=False)
    environment: Mapped[ConnectionEnvironment] = mapped_column(Enum(ConnectionEnvironment), default=ConnectionEnvironment.DEVELOPMENT)

    # Non-secret configuration (host, port, database, auth type, etc.) —
    # shape depends on connection_type; see connection_service.FIELD_SCHEMAS.
    config: Mapped[dict] = mapped_column(JSON, default=dict)

    # {field_name: fernet_ciphertext} — never serialized to the API as-is.
    encrypted_secrets: Mapped[dict] = mapped_column(JSON, default=dict)
    # Bumped every time any secret field is rotated — lets the UI show
    # "credentials last rotated ..." without ever exposing the value.
    secrets_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[ConnectionStatus] = mapped_column(Enum(ConnectionStatus), default=ConnectionStatus.UNTESTED, index=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # soft-delete flag

    # AI Connection Subscription & Organisation-Level Access Management —
    # organisation-wide visibility for an AI_PROVIDER connection, same
    # precedent as SqlWorkspaceQuery.is_shared: False by default (private
    # until the owner/admin explicitly makes it public), discoverable by
    # every eligible user in the SAME organization only when True (never
    # across tenants). Meaningful for every connection category, not just
    # AI_PROVIDER, but AI Connection access is the first feature to read
    # it — see models/ai_connection.py's AIConnectionPermission for the
    # per-user/per-role grant table this complements.
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Connection {self.name} ({self.connection_type})>"
