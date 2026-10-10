"""
ConnectionService — CRUD, encryption and connectivity testing for the
Connections module.

Security boundary: this is the ONLY module that ever touches
`Connection.encrypted_secrets` or plaintext secret values. Every response
that leaves this module has secrets already masked — routers never see a
real secret value once it's been saved. decrypt_secret() exists solely to
feed test_connection() and, later, real dataset reads — its output must
never be put in an API response, log line, or exception message.

FIELD_SCHEMAS below is the single source of truth for what fields each
connection type has (which are secret, required, conditional, etc.) —
exposed to the frontend via GET /api/connections/types so the dynamic form
never duplicates this definition.

test_connection() performs a REAL connectivity attempt for every database
type registered in app.connectors.registry (Postgres/MySQL/SQL Server as
of Phase 1 of the data-integration build — see that module for what's
verified and why). For connection types with no driver-backed connector
yet (Oracle/Snowflake/SAP/Fabric, plus every non-database category),
it returns a deterministic "not yet supported" result — never a random
simulated outcome.
"""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from sqlalchemy.orm.attributes import flag_modified

from app.connectors.base import BaseConnector, ConnectorError, ConnectorUnsupportedError, ConnectorValidationError, PreviewResult
from app.connectors.database.pool_manager import evict as evict_pool
from app.connectors.registry import get_connector, is_registered_connector
from app.core import crypto
from app.models.connection import Connection, ConnectionStatus
from app.models.user import User

MASK = "••••••••"  # ••••••••


class ConnectionValidationError(Exception):
    """Raised for bad connection_type/category or missing required fields."""


# ── Field schema catalogue (single source of truth) ───────────────

FIELD_SCHEMAS: dict[str, dict] = {
    "SQL_SERVER": {
        "label": "SQL Server", "category": "DATABASE",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": True, "default": 1433},
            {"key": "database", "label": "Database", "type": "text", "required": True},
            {"key": "authentication", "label": "Authentication", "type": "select",
             "options": ["SQL Server Authentication", "Windows Authentication", "Azure AD"],
             "default": "SQL Server Authentication"},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            {"key": "encryption", "label": "Encryption", "type": "select",
             "options": ["Optional", "Required", "Strict"], "default": "Optional"},
            {"key": "trust_certificate", "label": "Trust Server Certificate", "type": "checkbox", "default": False},
        ],
    },
    "POSTGRESQL": {
        "label": "PostgreSQL", "category": "DATABASE",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": True, "default": 5432},
            {"key": "database", "label": "Database", "type": "text", "required": True},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            {"key": "ssl_mode", "label": "SSL", "type": "select",
             "options": ["Disable", "Allow", "Require", "Verify-Full"], "default": "Require"},
        ],
    },
    "MYSQL": {
        "label": "MySQL", "category": "DATABASE",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": True, "default": 3306},
            {"key": "database", "label": "Database", "type": "text", "required": True},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            {"key": "ssl_mode", "label": "SSL", "type": "select", "options": ["Disable", "Require"], "default": "Disable"},
        ],
    },
    "ORACLE": {
        "label": "Oracle", "category": "DATABASE",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": True, "default": 1521},
            {"key": "service_name", "label": "Service Name", "type": "text", "required": True},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
        ],
    },
    "SNOWFLAKE": {
        "label": "Snowflake", "category": "DATABASE",
        "fields": [
            {"key": "account", "label": "Account", "type": "text", "required": True},
            {"key": "warehouse", "label": "Warehouse", "type": "text", "required": True},
            {"key": "database", "label": "Database", "type": "text", "required": True},
            {"key": "schema", "label": "Schema", "type": "text", "required": True},
            {"key": "role", "label": "Role", "type": "text"},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
        ],
    },
    "SAP": {
        "label": "SAP", "category": "DATABASE",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "system_number", "label": "System Number", "type": "text", "required": True},
            {"key": "client", "label": "Client", "type": "text", "required": True},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            {"key": "language", "label": "Language", "type": "text", "default": "EN"},
        ],
    },
    "VERTICA": {
        "label": "Vertica", "category": "DATABASE",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": True, "default": 5433},
            {"key": "database", "label": "Database", "type": "text", "required": True},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
        ],
    },
    "FABRIC": {
        "label": "Microsoft Fabric", "category": "DATABASE",
        "fields": [
            # Not required: connectors/database/fabric.py never reads this
            # field at all — the connector only needs the SQL Endpoint +
            # Database (a Fabric Warehouse/Lakehouse's SQL analytics
            # endpoint is fully addressable without naming its workspace).
            # Left as an optional label purely for the user's own
            # organization/reference. Marking it required blocked saving a
            # connection whenever that detail wasn't handy — found live
            # ("workspace name should not be a required field").
            {"key": "workspace", "label": "Workspace (optional, for reference only)", "type": "text"},
            {"key": "sql_endpoint", "label": "SQL Endpoint", "type": "text", "required": True,
             "placeholder": "xxxxxxxx.datawarehouse.fabric.microsoft.com"},
            {"key": "database", "label": "Database", "type": "text", "required": True},
            {"key": "authentication", "label": "Authentication", "type": "select",
             "options": ["Azure AD", "Service Principal"], "default": "Azure AD",
             "help": "Fabric SQL endpoints only accept Entra ID (Azure AD) tokens — there is no SQL "
                     "username/password auth. 'Azure AD' here means a non-interactive Resource Owner "
                     "Password credential (a user's own Entra ID username/password) suitable for an "
                     "unattended backend service; 'Service Principal' uses an app registration's client "
                     "secret instead and works even when the tenant requires MFA."},
            {"key": "username", "label": "Azure AD Username", "type": "text", "show_if": {"field": "authentication", "equals": "Azure AD"}},
            {"key": "password", "label": "Azure AD Password", "type": "password", "secret": True, "show_if": {"field": "authentication", "equals": "Azure AD"}},
            {"key": "tenant_id", "label": "Tenant ID", "type": "text", "show_if": {"field": "authentication", "equals": "Service Principal"}},
            {"key": "client_id", "label": "Client ID", "type": "text", "show_if": {"field": "authentication", "equals": "Service Principal"}},
            {"key": "client_secret", "label": "Client Secret", "type": "password", "secret": True, "show_if": {"field": "authentication", "equals": "Service Principal"}},
        ],
    },
    "SALESFORCE": {
        # Fields corrected to Salesforce's own real auth model (SOAP/REST
        # username-password-security-token login, optionally scoped to a
        # Connected App) — the previous shape (consumer_key/consumer_secret
        # + token_id/token_secret with no username/password at all) was a
        # NetSuite-TBA-style field set that doesn't correspond to anything
        # Salesforce's login API accepts; see connectors/application/
        # salesforce.py's docstring.
        "label": "Salesforce", "category": "APPLICATION",
        "fields": [
            {"key": "username", "label": "Username", "type": "text", "required": True, "placeholder": "user@company.com"},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            {"key": "security_token", "label": "Security Token", "type": "password", "required": True, "secret": True,
             "help": "From Salesforce: Settings -> My Personal Information -> Reset My Security Token. Required "
                     "unless this org's login IP ranges are trusted/whitelisted for API access."},
            {"key": "consumer_key", "label": "Connected App Consumer Key", "type": "text",
             "help": "Optional — scopes the login to a specific Connected App (recommended for production; lets "
                     "an admin restrict IP ranges/session policy per integration). Leave blank to log in without one."},
            {"key": "consumer_secret", "label": "Connected App Consumer Secret", "type": "password", "secret": True},
            {"key": "domain", "label": "My Domain (optional)", "type": "text",
             "help": "Custom My Domain prefix, e.g. 'mycompany' for mycompany.my.salesforce.com. Leave blank to "
                     "use the standard login host implied by Environment."},
            {"key": "environment", "label": "Environment", "type": "select", "options": ["Production", "Sandbox"], "default": "Production"},
        ],
    },
    "NETSUITE_SUITEQL": {
        "label": "NetSuite (SuiteQL)", "category": "APPLICATION",
        "fields": [
            {"key": "dsn", "label": "DSN (ODBC Data Source Name)", "type": "text", "required": True,
             "placeholder": "NetSuite2.0", "help": "Must already be configured in this server's odbc.ini / System DSN via NetSuite's SuiteAnalytics Connect ODBC driver."},
            {"key": "username", "label": "Username / Email", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            {"key": "role_id", "label": "Role ID (internal ID)", "type": "text",
             "help": "Optional — only needed if the login user has more than one assignable role."},
            {"key": "account", "label": "Account ID", "type": "text", "placeholder": "1234567 or 1234567_SB1",
             "help": "Optional — only needed if your DSN doesn't already set AccountID itself (e.g. via "
                     "ODBC Data Source Administrator on Windows, where Account/Role are typically baked "
                     "into the DSN alongside the host/port). Leave blank to use whatever the DSN already has."},
        ],
    },
    "NETSUITE_SOAP": {
        "label": "NetSuite (SOAP / Saved Search)", "category": "APPLICATION",
        "fields": [
            {"key": "account", "label": "Account ID", "type": "text", "required": True, "placeholder": "1234567 or 1234567_SB1"},
            {"key": "wsdl_version", "label": "WSDL Version", "type": "text", "default": "2024_2", "placeholder": "2024_2"},
            {"key": "consumer_key", "label": "Consumer Key", "type": "text", "required": True},
            {"key": "consumer_secret", "label": "Consumer Secret", "type": "password", "required": True, "secret": True},
            {"key": "token_id", "label": "Token ID", "type": "text", "required": True},
            {"key": "token_secret", "label": "Token Secret", "type": "password", "required": True, "secret": True},
        ],
    },
    "ORACLE_ERP_CLOUD": {
        # Oracle Fusion ERP Cloud — Payables Invoices REST resource. See
        # connectors/application/oracle_erp.py's docstring for the exact
        # documented resource/auth contract this implements. Distinct
        # from ORACLE above (a plain Oracle Database connection).
        "label": "Oracle ERP Cloud", "category": "APPLICATION",
        "fields": [
            {"key": "host", "label": "Instance URL", "type": "text", "required": True,
             "placeholder": "https://your-instance.fa.us2.oraclecloud.com",
             "help": "Base URL of your Oracle Fusion ERP Cloud instance (no trailing path)."},
            {"key": "api_version", "label": "API Version", "type": "text", "default": "11.13.18.05",
             "help": "REST API version segment used in the resource path — leave the default unless your instance documents a different one."},
            {"key": "auth_type", "label": "Auth Type", "type": "select", "options": ["Basic Auth", "OAuth2"], "default": "Basic Auth"},
            {"key": "username", "label": "Username", "type": "text", "show_if": {"field": "auth_type", "equals": "Basic Auth"}},
            {"key": "password", "label": "Password", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "Basic Auth"}},
            {"key": "client_id", "label": "Client ID", "type": "text", "show_if": {"field": "auth_type", "equals": "OAuth2"}},
            {"key": "client_secret", "label": "Client Secret", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "OAuth2"}},
            {"key": "token_url", "label": "Token URL (IDCS / OCI IAM)", "type": "text", "show_if": {"field": "auth_type", "equals": "OAuth2"}},
        ],
    },
    "TALLY_PRIME": {
        # TallyPrime — real XML-over-HTTP against Tally's own local gateway
        # (Gateway of Tally -> F1 Help -> Settings -> Connectivity, "Enable
        # ODBC/HTTP Server" — default port 9000). No SQL dialect and no
        # WSDL; see connectors/application/tallyprime.py's docstring.
        # Write is OFF by default (Part 16 — read-only until the full
        # validation/duplicate-check/approval flow is verified) and the
        # UI must show "READ ONLY" unless write_enabled is explicitly true.
        "label": "TallyPrime", "category": "APPLICATION",
        "fields": [
            {"key": "host", "label": "Tally Host / IP", "type": "text", "required": True, "default": "localhost",
             "help": "The machine running TallyPrime with its HTTP gateway enabled — usually 'localhost' if this backend runs on the same machine as Tally, otherwise Tally's LAN IP."},
            {"key": "port", "label": "Port", "type": "number", "required": True, "default": 9000},
            {"key": "company_name", "label": "Company", "type": "text",
             "help": "Leave blank until you've run Test Connection at least once — then use Discover Companies to pick from Tally's real company list. Every Tally operation explicitly targets this one company; nothing is ever inferred."},
            {"key": "timeout_seconds", "label": "Timeout (seconds)", "type": "number", "default": 30},
            {"key": "max_retries", "label": "Max Retries", "type": "number", "default": 3},
            {"key": "connection_mode", "label": "Connection Mode", "type": "select", "options": ["HTTP"], "default": "HTTP",
             "help": "TallyPrime's own local gateway only speaks plain HTTP (not HTTPS) — this is Tally's own protocol choice, not a limitation of this integration."},
            {"key": "read_mode", "label": "Read Mode", "type": "select", "options": ["Full", "Incremental"], "default": "Incremental"},
            {"key": "write_enabled", "label": "Write Enabled", "type": "checkbox", "default": False,
             "help": "OFF by default. Every write still separately requires validation, duplicate-check, a voucher preview and explicit human approval before anything is posted to Tally — enabling this alone does not let anything write automatically."},
            {"key": "auth_token", "label": "Gateway Auth Token (optional)", "type": "password", "secret": True,
             "help": "Only needed if this Tally instance's HTTP gateway sits behind a reverse proxy that requires a bearer token — Tally's own gateway has no built-in authentication."},
        ],
    },
    "LOCAL_FILE": {
        "label": "Local Folder", "category": "STORAGE",
        "fields": [
            {"key": "base_path", "label": "Folder Path", "type": "text", "required": True,
             "placeholder": "/absolute/path/to/folder",
             "help": "A path on THIS SERVER's own filesystem (not your browser's computer) — the backend "
                     "process reads directly from it. Every file operation is sandboxed to this folder and "
                     "its subfolders; nothing outside it is reachable. Only usable when the backend runs "
                     "somewhere you control (e.g. a self-hosted/on-prem deployment) — on shared or "
                     "multi-tenant hosting this exposes whatever the backend process itself can read, so "
                     "restrict it to a folder created just for this purpose."},
        ],
    },
    "SFTP": {
        "label": "SFTP", "category": "STORAGE",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": True, "default": 22},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "auth_type", "label": "Authentication Type", "type": "select", "options": ["Password", "Private Key"], "default": "Password"},
            {"key": "password", "label": "Password", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "Password"}},
            {"key": "private_key", "label": "Private Key", "type": "textarea", "secret": True, "show_if": {"field": "auth_type", "equals": "Private Key"}},
            {"key": "base_path", "label": "Base Path", "type": "text", "default": "/"},
        ],
    },
    "FTP": {
        "label": "FTP", "category": "STORAGE",
        "fields": [
            {"key": "host", "label": "Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": True, "default": 21},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            {"key": "base_path", "label": "Base Path", "type": "text", "default": "/"},
        ],
    },
    "NETWORK_FILE": {
        "label": "Network File", "category": "STORAGE",
        "fields": [
            {"key": "unc_path", "label": "UNC Path", "type": "text", "required": True, "placeholder": "\\\\server\\share"},
            {"key": "username", "label": "Username", "type": "text"},
            {"key": "password", "label": "Password", "type": "password", "secret": True},
            {"key": "port", "label": "SMB Port", "type": "number", "default": 445,
             "help": "Only change this from the standard 445 for a non-default deployment (e.g. local testing)."},
        ],
    },
    "EMAIL_IMAP": {
        "label": "Email Inbox (IMAP)", "category": "STORAGE",
        "fields": [
            {"key": "host", "label": "IMAP Host", "type": "text", "required": True},
            {"key": "port", "label": "Port", "type": "number", "required": True, "default": 993},
            {"key": "use_ssl", "label": "Use SSL/TLS", "type": "checkbox", "default": True},
            {"key": "username", "label": "Username", "type": "text", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True, "secret": True},
            {"key": "mailbox", "label": "Mailbox / Folder", "type": "text", "default": "INBOX"},
            {"key": "search_criteria", "label": "IMAP Search Criteria", "type": "text", "default": "UNSEEN",
             "help": "Standard IMAP SEARCH keys — e.g. UNSEEN (default, unread only) or ALL. "
                     "A schedule's own idempotency ledger still prevents reprocessing regardless of this filter; "
                     "this only controls which messages are worth listing at all."},
        ],
    },
    "REST": {
        "label": "REST API", "category": "API",
        "fields": [
            {"key": "base_url", "label": "Base URL", "type": "text", "required": True},
            {"key": "auth_type", "label": "Auth Type", "type": "select",
             "options": ["None", "API Key", "Bearer Token", "Basic Auth", "OAuth2"], "default": "None"},
            {"key": "api_key", "label": "API Key", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "API Key"}},
            {"key": "api_key_header", "label": "API Key Header Name", "type": "text", "default": "X-API-Key",
             "placeholder": "X-API-Key", "show_if": {"field": "auth_type", "equals": "API Key"}},
            {"key": "bearer_token", "label": "Bearer Token", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "Bearer Token"}},
            {"key": "username", "label": "Username", "type": "text", "show_if": {"field": "auth_type", "equals": "Basic Auth"}},
            {"key": "password", "label": "Password", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "Basic Auth"}},
            {"key": "client_id", "label": "Client ID", "type": "text", "show_if": {"field": "auth_type", "equals": "OAuth2"}},
            {"key": "client_secret", "label": "Client Secret", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "OAuth2"}},
            {"key": "token_url", "label": "Token URL", "type": "text", "show_if": {"field": "auth_type", "equals": "OAuth2"}},
        ],
    },
    "SOAP": {
        "label": "SOAP API", "category": "API",
        "fields": [
            {"key": "wsdl_url", "label": "WSDL URL", "type": "text", "required": True},
            {"key": "auth_type", "label": "Auth Type", "type": "select", "options": ["None", "Basic Auth", "WS-Security"], "default": "None"},
            {"key": "username", "label": "Username", "type": "text", "show_if": {"field": "auth_type", "equals": "Basic Auth"}},
            {"key": "password", "label": "Password", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "Basic Auth"}},
        ],
    },
    "AZURE": {
        "label": "Azure", "category": "CLOUD",
        "fields": [
            {"key": "storage_account", "label": "Storage Account", "type": "text", "required": True},
            {"key": "container", "label": "Container", "type": "text"},
            {"key": "auth_type", "label": "Auth Type", "type": "select",
             "options": ["Connection String", "Access Key", "Service Principal"], "default": "Access Key"},
            {"key": "connection_string", "label": "Connection String", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "Connection String"}},
            {"key": "access_key", "label": "Access Key", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "Access Key"}},
            {"key": "tenant_id", "label": "Tenant ID", "type": "text", "show_if": {"field": "auth_type", "equals": "Service Principal"}},
            {"key": "client_id", "label": "Client ID", "type": "text", "show_if": {"field": "auth_type", "equals": "Service Principal"}},
            {"key": "client_secret", "label": "Client Secret", "type": "password", "secret": True, "show_if": {"field": "auth_type", "equals": "Service Principal"}},
        ],
    },
    "AWS": {
        "label": "AWS", "category": "CLOUD",
        "fields": [
            {"key": "region", "label": "Region", "type": "text", "required": True, "placeholder": "us-east-1"},
            {"key": "service", "label": "Service", "type": "select", "options": ["S3", "Redshift", "RDS"], "default": "S3",
             "help": "Redshift/RDS are wire-compatible with Postgres/MySQL/SQL Server — use one of those connection types "
                     "pointed at the RDS/Redshift endpoint instead; this connector implements S3 directly."},
            {"key": "resource", "label": "Bucket / Resource", "type": "text"},
            {"key": "access_key_id", "label": "Access Key ID", "type": "text", "required": True},
            {"key": "secret_access_key", "label": "Secret Access Key", "type": "password", "required": True, "secret": True},
            {"key": "endpoint_url", "label": "Custom Endpoint URL (optional)", "type": "text",
             "help": "For an S3-compatible service (e.g. MinIO) or local testing — leave blank for real AWS S3."},
        ],
    },
    "OTHER": {
        "label": "Other", "category": "CLOUD",
        "fields": [
            {"key": "host", "label": "Host / Endpoint", "type": "text"},
            {"key": "notes", "label": "Notes", "type": "textarea"},
        ],
    },
    "OPENAI": {
        "label": "OpenAI", "category": "AI_PROVIDER",
        "fields": [
            {"key": "model", "label": "Model", "type": "text", "required": True, "default": "gpt-4o-mini",
             "placeholder": "gpt-4o-mini, gpt-4o, gpt-4-turbo, ..."},
            {"key": "api_key", "label": "API Key", "type": "password", "required": True, "secret": True},
            {"key": "base_url", "label": "Base URL (optional — for Azure OpenAI or a compatible gateway)", "type": "text",
             "placeholder": "https://api.openai.com/v1"},
        ],
    },
    "ANTHROPIC": {
        "label": "Anthropic (Claude)", "category": "AI_PROVIDER",
        "fields": [
            {"key": "model", "label": "Model", "type": "text", "required": True, "default": "claude-3-5-haiku-20241022",
             "placeholder": "claude-3-5-haiku-20241022, claude-3-5-sonnet-20241022, ..."},
            {"key": "api_key", "label": "API Key", "type": "password", "required": True, "secret": True},
        ],
    },
    "OLLAMA": {
        "label": "Ollama (local)", "category": "AI_PROVIDER",
        "fields": [
            {"key": "base_url", "label": "Server URL", "type": "text", "required": True, "default": "http://localhost:11434"},
            {"key": "model", "label": "Model", "type": "text", "required": True, "default": "llama3.2",
             "placeholder": "llama3.2, mistral, ... (must already be pulled — `ollama pull <model>`)"},
        ],
    },
    "AWS_TEXTRACT": {
        "label": "AWS Textract", "category": "OCR_PROVIDER",
        "fields": [
            {"key": "region", "label": "Region", "type": "text", "required": True, "default": "us-east-1",
             "placeholder": "us-east-1"},
            {"key": "access_key_id", "label": "Access Key ID", "type": "password", "required": True, "secret": True},
            {"key": "secret_access_key", "label": "Secret Access Key", "type": "password", "required": True, "secret": True},
            {"key": "session_token", "label": "Session Token (optional — for temporary STS credentials)",
             "type": "password", "secret": True},
        ],
    },
    "GOOGLE_VISION": {
        "label": "Google Cloud Vision", "category": "OCR_PROVIDER",
        "fields": [
            {"key": "service_account_json", "label": "Service Account JSON Key", "type": "textarea", "required": True,
             "secret": True, "placeholder": "Paste the full contents of a GCP service account JSON key file"},
        ],
    },
    "AZURE_FORM_RECOGNIZER": {
        "label": "Azure Document Intelligence (Form Recognizer)", "category": "OCR_PROVIDER",
        "fields": [
            {"key": "endpoint", "label": "Endpoint", "type": "text", "required": True,
             "placeholder": "https://<resource-name>.cognitiveservices.azure.com/"},
            {"key": "api_key", "label": "API Key", "type": "password", "required": True, "secret": True},
        ],
    },
}

CATEGORIES = [
    {"key": "DATABASE", "label": "Database", "types": ["SQL_SERVER", "POSTGRESQL", "MYSQL", "ORACLE", "SNOWFLAKE", "SAP", "VERTICA", "FABRIC"]},
    {"key": "APPLICATION", "label": "Application", "types": ["SALESFORCE", "NETSUITE_SUITEQL", "NETSUITE_SOAP", "ORACLE_ERP_CLOUD", "TALLY_PRIME"]},
    {"key": "STORAGE", "label": "Storage", "types": ["LOCAL_FILE", "SFTP", "FTP", "NETWORK_FILE", "EMAIL_IMAP"]},
    {"key": "API", "label": "API", "types": ["REST", "SOAP"]},
    {"key": "CLOUD", "label": "Cloud", "types": ["AZURE", "AWS", "OTHER"]},
    {"key": "AI_PROVIDER", "label": "AI Agent", "types": ["OPENAI", "ANTHROPIC", "OLLAMA"]},
    {"key": "OCR_PROVIDER", "label": "OCR Provider", "types": ["AWS_TEXTRACT", "GOOGLE_VISION", "AZURE_FORM_RECOGNIZER"]},
]

# Field used to populate the table's "Host / Endpoint" / "Database" columns
# per connection type — keeps the frontend table generic (no per-type switch).
_HOST_FIELD = {
    "SQL_SERVER": "host", "POSTGRESQL": "host", "MYSQL": "host", "ORACLE": "host", "SAP": "host", "VERTICA": "host",
    "SNOWFLAKE": "account", "FABRIC": "sql_endpoint",
    "SALESFORCE": "username", "NETSUITE_SUITEQL": "dsn", "NETSUITE_SOAP": "account",
    "ORACLE_ERP_CLOUD": "host",
    "TALLY_PRIME": "host",
    "LOCAL_FILE": "base_path", "SFTP": "host", "FTP": "host", "NETWORK_FILE": "unc_path", "EMAIL_IMAP": "host",
    "REST": "base_url", "SOAP": "wsdl_url",
    "AZURE": "storage_account", "AWS": "region", "OTHER": "host",
    "OPENAI": "base_url", "ANTHROPIC": "model", "OLLAMA": "base_url",
    # GOOGLE_VISION has no non-secret identifying field (its only config is
    # the secret service-account JSON) — deliberately unmapped so the table
    # shows a genuine blank rather than a key that's never in conn.config.
    "AWS_TEXTRACT": "region", "AZURE_FORM_RECOGNIZER": "endpoint",
}
_DATABASE_FIELD = {
    "SQL_SERVER": "database", "POSTGRESQL": "database", "MYSQL": "database", "ORACLE": "service_name",
    "SNOWFLAKE": "database", "FABRIC": "database", "VERTICA": "database", "NETSUITE_SUITEQL": "account",
    "OPENAI": "model", "OLLAMA": "model",
}

def get_type_catalogue() -> dict:
    return {"categories": CATEGORIES, "schemas": FIELD_SCHEMAS}


def _schema_for(connection_type: str) -> dict:
    schema = FIELD_SCHEMAS.get(connection_type)
    if schema is None:
        raise ConnectionValidationError(f"Unknown connection type '{connection_type}'.")
    return schema


def secret_keys_for(connection_type: str) -> set[str]:
    return {f["key"] for f in _schema_for(connection_type)["fields"] if f.get("secret")}


# ── Encryption ─────────────────────────────────────────────────────
# Delegates to app.core.crypto (shared Fernet-over-secret_key impl — see
# that module's docstring; extracted from here so Seller SMTP credentials
# can reuse the same encryption instead of a second copy).

_encrypt = crypto.encrypt_secret


def decrypt_secret(conn: Connection, field_key: str) -> str | None:
    """The ONLY way to recover a plaintext secret — used solely by
    test_connection(); never exposed through an API response."""
    token = (conn.encrypted_secrets or {}).get(field_key)
    return crypto.decrypt_secret(token) if token else None


def _split_config(connection_type: str, raw_config: dict) -> tuple[dict, dict]:
    """Splits a submitted config dict into (non_secret_fields, secret_plaintext_values)."""
    secret_keys = secret_keys_for(connection_type)
    non_secret = {k: v for k, v in raw_config.items() if k not in secret_keys}
    secrets = {k: v for k, v in raw_config.items() if k in secret_keys and v not in (None, "")}
    return non_secret, secrets


def _masked_config(connection_type: str, config: dict, encrypted_secrets: dict) -> dict:
    masked = dict(config)
    for key in secret_keys_for(connection_type):
        masked[key] = MASK if (encrypted_secrets or {}).get(key) else ""
    return masked


def _validate_required(connection_type: str, config: dict, encrypted_secrets: dict) -> list[str]:
    """Returns labels of required fields still missing, honoring show_if
    (a conditionally-hidden field isn't required)."""
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


# ── CRUD ─────────────────────────────────────────────────────────

async def create_connection(
    db: AsyncSession, *, organization_id: int | None, owner_id: int,
    name: str, category: str, connection_type: str, environment: str, raw_config: dict,
) -> Connection:
    _schema_for(connection_type)  # raises if unknown
    non_secret, secrets = _split_config(connection_type, raw_config)
    encrypted = {k: _encrypt(v) for k, v in secrets.items()}

    conn = Connection(
        organization_id=organization_id, owner_id=owner_id, name=name,
        category=category, connection_type=connection_type, environment=environment,
        config=non_secret, encrypted_secrets=encrypted,
        secrets_rotated_at=datetime.now(timezone.utc) if encrypted else None,
        status=ConnectionStatus.UNTESTED,
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


async def update_connection(
    db: AsyncSession, conn: Connection, *, name: str | None, environment: str | None, raw_config: dict | None,
) -> Connection:
    if name:
        conn.name = name
    if environment:
        conn.environment = environment
    if raw_config is not None:
        non_secret, secrets = _split_config(conn.connection_type.value, raw_config)
        conn.config = {**conn.config, **non_secret}
        flag_modified(conn, "config")
        if secrets:
            conn.encrypted_secrets = {**(conn.encrypted_secrets or {}), **{k: _encrypt(v) for k, v in secrets.items()}}
            flag_modified(conn, "encrypted_secrets")
            conn.secrets_rotated_at = datetime.now(timezone.utc)
        # Any config or secret change invalidates a pooled connection
        # opened under the old values — never let a stale pool keep
        # answering for what's now a different target/credential.
        evict_pool(f"conn:{conn.id}")
    await db.commit()
    await db.refresh(conn)
    return conn


async def clone_connection(db: AsyncSession, conn: Connection, owner_id: int) -> Connection:
    clone = Connection(
        organization_id=conn.organization_id, owner_id=owner_id, name=f"{conn.name} (Copy)",
        category=conn.category, connection_type=conn.connection_type, environment=conn.environment,
        config=dict(conn.config), encrypted_secrets=dict(conn.encrypted_secrets or {}),
        secrets_rotated_at=conn.secrets_rotated_at,
        status=ConnectionStatus.UNTESTED,  # a clone hasn't been tested itself yet
    )
    db.add(clone)
    await db.commit()
    await db.refresh(clone)
    return clone


async def delete_connection(db: AsyncSession, conn: Connection) -> None:
    evict_pool(f"conn:{conn.id}")
    await db.delete(conn)
    await db.commit()


async def toggle_disabled(db: AsyncSession, conn: Connection) -> Connection:
    """Disable <-> re-enable. Re-enabling restores whatever status the last
    test implied, rather than blindly marking it ACTIVE again."""
    if conn.status == ConnectionStatus.INACTIVE:
        if conn.last_tested_at is None:
            conn.status = ConnectionStatus.UNTESTED
        else:
            conn.status = ConnectionStatus.ACTIVE if conn.last_test_success else ConnectionStatus.ERROR
    else:
        conn.status = ConnectionStatus.INACTIVE
    await db.commit()
    await db.refresh(conn)
    return conn


def build_connector(conn: Connection) -> BaseConnector:
    """
    Builds a real, driver-backed connector for a saved Connection —
    decrypting exactly the secret fields its type needs, never more.
    Raises ConnectorUnsupportedError for anything without a working
    connector wired up yet (see connectors/registry.py). Public — shared
    by test_connection(), every schema/object/column/preview function
    below, AND execution/pipeline_executor.py (Phase 3), so "can this
    connection actually be used for X" is answered the same way
    everywhere a connector gets built from a saved Connection row.
    """
    connection_type = conn.connection_type.value
    if not is_registered_connector(connection_type):
        raise ConnectorUnsupportedError(
            f"{connection_type.replace('_', ' ').title()} connections aren't supported yet — only "
            f"database (Postgres, MySQL, SQL Server), file (SFTP, Local File) and AI provider "
            f"(OpenAI, Anthropic, Ollama) connectors are wired up in this build."
        )
    secret_values = {key: decrypt_secret(conn, key) for key in secret_keys_for(connection_type)}
    secret_values = {k: v for k, v in secret_values.items() if v is not None}
    return get_connector(connection_type, conn.config, secret_values, cache_key=f"conn:{conn.id}")


async def test_connection(db: AsyncSession, conn: Connection) -> dict:
    now = datetime.now(timezone.utc)
    connection_type = conn.connection_type.value
    missing = _validate_required(connection_type, conn.config, conn.encrypted_secrets)

    latency_ms: float | None = None
    server_version: str | None = None
    unsupported = False

    if missing:
        success = False
        message = f"Missing required field(s): {', '.join(missing)}."
    else:
        try:
            connector = build_connector(conn)
        except ConnectorUnsupportedError as exc:
            success, unsupported, message = False, True, str(exc)
        else:
            result = await asyncio.to_thread(connector.test_connection)
            success, message = result.success, result.message
            latency_ms, server_version = result.latency_ms, result.server_version

    conn.last_tested_at = now
    conn.last_test_success = success
    conn.last_test_message = message
    if conn.status != ConnectionStatus.INACTIVE:  # a disabled connection stays disabled even if tested
        if unsupported:
            conn.status = ConnectionStatus.UNSUPPORTED
        else:
            conn.status = ConnectionStatus.ACTIVE if success else ConnectionStatus.ERROR
    await db.commit()
    await db.refresh(conn)
    return {
        "success": success, "message": message, "tested_at": now, "status": conn.status.value,
        "latency_ms": latency_ms, "server_version": server_version,
    }


# ── Schema/object browsing — Source Dataset node (Phase 2) ────────
# All of these raise ConnectorUnsupportedError (-> 501 at the API layer)
# or ConnectorValidationError (-> 400) rather than returning empty/fake
# data for a connection type or SQL statement they can't really handle.
# See connectors/database/base.py for the actual SQLAlchemy-reflection
# implementation shared by every registered database connector.

PREVIEW_LIMIT_MAX = 1000  # spec: preview 10/50/100/1000 rows, never the full dataset

# Schema/object/column browsing hits the live source. This bound is a
# background SAFETY NET only — not a user-facing constraint — and must
# stay well above services/api/connectionApi.js's own BROWSE_TIMEOUT
# (120s, deliberately generous and already live-verified against a real
# NetSuite account — see that file's own comment for the history). That
# frontend timeout is what a user actually experiences; this one exists
# purely so a truly wedged native driver call (pyodbc/ODBC connections in
# particular can hang the underlying OS thread with no way to interrupt
# it) doesn't leak a thread-pool slot forever after the browser has long
# since given up and moved on. It must never be the thing that cuts off a
# real, in-progress, slow-but-working catalog enumeration — NetSuite's
# SuiteAnalytics Connect ODBC driver scanning every standard + custom
# record type on a large account is exactly that case, and a first
# attempt at this safety net (45s) was tuned far too aggressively,
# cutting off real NetSuite schema fetches that other tools complete
# successfully given enough time — found live. 10 minutes is long enough
# to never be reached by any legitimate interactive browse (the frontend
# itself gives up 8x sooner), while still bounding a genuinely stuck call.
BROWSE_TIMEOUT_SECONDS = 600


async def _browse_call(fn, *args):
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=BROWSE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise ConnectorError(
            f"Timed out after {BROWSE_TIMEOUT_SECONDS}s waiting on the source — it may be slow to enumerate "
            "objects, or unreachable. You can enter the object name manually instead of browsing."
        ) from exc


async def list_schemas(conn: Connection) -> list[str]:
    connector = build_connector(conn)
    return await _browse_call(connector.list_schemas)


async def list_objects(conn: Connection, schema: str | None, search: str | None):
    connector = build_connector(conn)
    return await _browse_call(connector.list_objects, schema, search)


async def get_columns(conn: Connection, object_name: str, schema: str | None, include_row_count: bool):
    connector = build_connector(conn)
    columns = await _browse_call(connector.get_columns, object_name, schema)
    row_count = await _browse_call(connector.row_count, object_name, schema) if include_row_count else None
    return columns, row_count


async def preview(
    conn: Connection, *, schema: str | None, object_name: str | None,
    sql: str | None, parameters: dict, limit: int,
):
    connector = build_connector(conn)
    bounded_limit = max(1, min(limit or 100, PREVIEW_LIMIT_MAX))
    if sql:
        return await asyncio.to_thread(connector.preview_sql, sql, parameters or {}, bounded_limit)
    if object_name:
        return await asyncio.to_thread(connector.preview_object, object_name, schema, bounded_limit)
    raise ConnectorValidationError("Provide either object_name (with optional schema) or sql.")


async def preview_api(
    conn: Connection, *, endpoint: str, method: str, query_params: dict, headers: dict,
    body: dict | None, json_path: str | None, pagination: dict, limit: int,
) -> PreviewResult:
    """Real preview for an API-category (REST) Source Dataset node — used
    both by the Source Dataset config panel's Preview button and, at
    save-time, to validate the endpoint/JSON path actually resolve before
    a pipeline run depends on them. Genuinely walks pagination the same
    way a real pipeline run would (see connectors/api/rest.py), just
    bounded to PREVIEW_LIMIT_MAX rows instead of the full extraction cap."""
    connector = build_connector(conn)
    bounded_limit = max(1, min(limit or 100, PREVIEW_LIMIT_MAX))
    return await asyncio.to_thread(
        connector.fetch_api,
        endpoint=endpoint, method=method or "GET", query_params=query_params or {}, headers=headers or {},
        body=body, json_path=json_path or None, pagination=pagination or {"style": "none"}, limit=bounded_limit,
    )


# ── Query / list ─────────────────────────────────────────────────

async def get_connection(db: AsyncSession, connection_id: int, organization_id: int | None) -> Connection | None:
    stmt = select(Connection).where(Connection.id == connection_id, Connection.is_active.is_(True))
    if organization_id is not None:
        stmt = stmt.where(Connection.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_ai_connection(db: AsyncSession, connection_id: int, organization_id: int | None) -> Connection | None:
    """Like get_connection(), but also returns a seller-managed connection
    (Connection.organization_id IS NULL) when the caller has an org context.
    get_connection()'s plain `organization_id == organization_id` filter
    was written for org-owned database/file connections, where a NULL-org
    row should never match a specific org's lookup — but AI Provider
    connections are the one category where a NULL-org row is a deliberate,
    intended object (a Seller-managed connection a customer org can be
    entitled to use — see ai_connection_access_service.py). Without this,
    extraction_service._get_connector()/sql_ai_service._get_ai_connector()
    would 404 on exactly the connection ids ai_connection_permission_service
    just granted a customer access to, since the standard lookup silently
    excludes every NULL-org row the moment an organization_id is given.
    The real authorization (entitlement/is_public/grant) still happens
    afterward via check_access() — this only widens what gets FETCHED,
    never what's allowed to be USED."""
    stmt = select(Connection).where(Connection.id == connection_id, Connection.is_active.is_(True))
    if organization_id is not None:
        stmt = stmt.where(or_(Connection.organization_id == organization_id, Connection.organization_id.is_(None)))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def owner_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


async def list_connections(
    db: AsyncSession, *, organization_id: int | None, search: str = "",
    category: str | None = None, connection_type: str | None = None,
    status_filter: str | None = None, environment: str | None = None,
    page: int = 1, page_size: int = 20, sort_by: str = "created_at", sort_dir: str = "desc",
) -> tuple[list[Connection], int, dict[int, str]]:
    stmt = select(Connection).where(Connection.is_active.is_(True))
    if organization_id is not None:
        stmt = stmt.where(Connection.organization_id == organization_id)
    if category:
        stmt = stmt.where(Connection.category == category)
    if connection_type:
        stmt = stmt.where(Connection.connection_type == connection_type)
    if status_filter:
        stmt = stmt.where(Connection.status == status_filter)
    if environment:
        stmt = stmt.where(Connection.environment == environment)

    result = await db.execute(stmt)
    conns = list(result.scalars().all())

    owner_ids = {c.owner_id for c in conns if c.owner_id}
    name_map = await owner_name_map(db, owner_ids)

    if search:
        needle = search.lower()
        def matches(c: Connection) -> bool:
            haystacks = [
                c.name, c.connection_type.value, c.category.value, c.environment.value,
                c.status.value, name_map.get(c.owner_id, ""),
                str(c.config.get(_HOST_FIELD.get(c.connection_type.value, ""), "")),
            ]
            return any(needle in h.lower() for h in haystacks if h)
        conns = [c for c in conns if matches(c)]

    reverse = sort_dir == "desc"
    conns.sort(key=lambda c: getattr(c, sort_by, c.created_at) or c.created_at, reverse=reverse)

    total = len(conns)
    start = (page - 1) * page_size
    return conns[start:start + page_size], total, name_map


async def get_stats(db: AsyncSession, organization_id: int | None) -> dict:
    stmt = select(Connection).where(Connection.is_active.is_(True))
    if organization_id is not None:
        stmt = stmt.where(Connection.organization_id == organization_id)
    result = await db.execute(stmt)
    conns = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    recently_tested = sum(
        1 for c in conns
        if c.last_tested_at and (now - (c.last_tested_at if c.last_tested_at.tzinfo else c.last_tested_at.replace(tzinfo=timezone.utc))).total_seconds() < 86_400
    )
    return {
        "total": len(conns),
        "active": sum(1 for c in conns if c.status == ConnectionStatus.ACTIVE),
        "inactive": sum(1 for c in conns if c.status == ConnectionStatus.INACTIVE),
        "errors": sum(1 for c in conns if c.status == ConnectionStatus.ERROR),
        "recently_tested": recently_tested,
    }


def to_out(conn: Connection, owner_name: str | None) -> dict:
    return {
        "id": conn.id, "name": conn.name, "category": conn.category.value,
        "connection_type": conn.connection_type.value, "environment": conn.environment.value,
        "config": _masked_config(conn.connection_type.value, conn.config, conn.encrypted_secrets),
        "status": conn.status.value,
        "host_display": conn.config.get(_HOST_FIELD.get(conn.connection_type.value, "")),
        "database_display": conn.config.get(_DATABASE_FIELD.get(conn.connection_type.value, "")),
        "owner_id": conn.owner_id, "owner_name": owner_name,
        "last_tested_at": conn.last_tested_at, "last_test_success": conn.last_test_success,
        "last_test_message": conn.last_test_message, "secrets_rotated_at": conn.secrets_rotated_at,
        "created_at": conn.created_at, "updated_at": conn.updated_at,
    }
