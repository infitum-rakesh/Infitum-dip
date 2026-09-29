"""
DocumentWebhookConfig / DocumentWebhookDelivery — Phase 6 (spec item #7:
"push document data through external APIs"), genuinely distinct from
Processed Data Storage (models/document_storage.py, spec item #5), which
only writes to a DATABASE Connection or a local file. This pushes the
same kind of processed-document payload to a customer-owned external
HTTP endpoint instead — a real webhook, with its own auth, retry,
circuit-breaker, and delivery-attempt history, deliberately NOT bolted
onto ProcessedDataStorageConfig/ConnectionType (that model's push targets
are hard-coded to _DATABASE_TYPES/_FILE_TYPES and explicitly reject
anything else — see document_storage_service.py's push_document()).

Modeled after the codebase's own best precedent for "reliably deliver to
an external HTTP endpoint with retry/circuit-breaker/idempotency" —
services/invoice/invoice_erp_service.py + models/invoice_intelligence/
erp.py's ApInvoiceErpTransaction — with the same one-row-per-attempt
history shape, the same DEAD_LETTER-after-retries-exhausted status, and
the same UNIQUE(idempotency_key, attempt_number) constraint enforcing
"never double-fire a webhook for the same document" at the DB level.

Two things ERP posting didn't need that a THIRD-PARTY webhook receiver
does:
  - encrypted_secret: the auth credential this SERVICE presents to the
    customer's endpoint (API key / bearer token / "user:pass" for Basic),
    Fernet-encrypted via core/crypto.py, same convention as Connection.
    encrypted_secrets and Seller SMTP credentials.
  - encrypted_signing_secret: a SEPARATE secret used to HMAC-SHA256-sign
    the outbound payload (X-Signature header) so the customer's endpoint
    can verify a delivery genuinely came from this platform and wasn't
    forged/tampered in transit — auto-generated on first config creation
    if the caller doesn't supply one. Nothing else in this codebase signs
    an OUTBOUND payload for a third party to verify (the Custom Gateway's
    signing in payment_gateways/custom_provider.py is for payloads THIS
    platform will later verify on its own /api/payments/webhook/custom
    route, not a customer-owned endpoint) — this closes that gap.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class DocumentWebhookAuthType(str, enum.Enum):
    NONE = "NONE"
    API_KEY = "API_KEY"    # sent as a custom header (auth_header_name), value = decrypted encrypted_secret
    BEARER = "BEARER"      # Authorization: Bearer <decrypted encrypted_secret>
    BASIC = "BASIC"        # Authorization: Basic base64(decrypted encrypted_secret), secret stored as "user:pass"
    OAUTH2 = "OAUTH2"      # client_credentials grant against oauth_token_url — see service's _oauth2_access_token()


class DocumentWebhookPushStatus(str, enum.Enum):
    NEVER_RUN = "NEVER_RUN"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class DocumentWebhookDeliveryStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    DEAD_LETTER = "DEAD_LETTER"


class DocumentWebhookConfig(TenantBase):
    __tablename__ = "document_webhook_configs"
    __table_args__ = (UniqueConstraint("organization_id", name="uq_document_webhook_config_org"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Configurable HTTP method (spec item #7) — POST remains the default,
    # matching every delivery this feature has ever sent before this field
    # existed. PUT/PATCH are the other methods a real webhook receiver
    # might require; GET/DELETE make no sense for "push document data" and
    # are rejected by update_config().
    http_method: Mapped[str] = mapped_column(String(10), default="POST")

    auth_type: Mapped[DocumentWebhookAuthType] = mapped_column(Enum(DocumentWebhookAuthType), default=DocumentWebhookAuthType.NONE)
    auth_header_name: Mapped[str] = mapped_column(String(200), default="X-API-Key")
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OAUTH2 auth_type only — client_credentials grant. encrypted_secret
    # above is reused as-is for API_KEY/BEARER/BASIC; OAuth2 needs its OWN
    # separate client secret (the encrypted_secret slot would otherwise be
    # ambiguous between "the OAuth2 client secret" and "a bearer token",
    # which are different things with different lifecycles).
    oauth_token_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    oauth_client_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    encrypted_oauth_client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_scope: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Extra static headers sent on every delivery (e.g. a receiver-specific
    # "X-Tenant-Id"), merged UNDER the auth/signature/idempotency headers
    # below so a misconfigured custom header can never clobber one of
    # those — see service's _push_in_background() header-assembly order.
    custom_headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Renames outbound payload keys before serializing, e.g.
    # {"document_id": "docId"} to match a receiver's own field naming —
    # see service's _apply_field_mapping(). None/empty means pass the
    # flattened payload through with its original key names, the original
    # (and still default) behavior.
    field_mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Per-config override of the module-level REQUEST_TIMEOUT_SECONDS
    # default — a receiver that's known to be slow (e.g. it does its own
    # synchronous downstream processing before responding) can be given
    # more room without changing the timeout for every other org's webhook.
    timeout_seconds: Mapped[float] = mapped_column(Float, default=15.0)

    # Auto-generated (secrets.token_hex) the first time enabled=True is
    # set with no signing secret configured yet — see service layer.
    encrypted_signing_secret: Mapped[str | None] = mapped_column(Text, nullable=True)

    include_ocr_text: Mapped[bool] = mapped_column(Boolean, default=True)
    include_extraction_fields: Mapped[bool] = mapped_column(Boolean, default=True)
    include_dataset: Mapped[bool] = mapped_column(Boolean, default=True)
    include_ai_raw_response: Mapped[bool] = mapped_column(Boolean, default=False)

    last_pushed_document_id: Mapped[int | None] = mapped_column(nullable=True)
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[DocumentWebhookPushStatus] = mapped_column(Enum(DocumentWebhookPushStatus), default=DocumentWebhookPushStatus.NEVER_RUN)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<DocumentWebhookConfig org={self.organization_id} enabled={self.enabled}>"


class DocumentWebhookDelivery(TenantBase):
    """One row per delivery ATTEMPT (not one row per document) — a
    retried document gets additional rows sharing the same
    idempotency_key, mirroring ApInvoiceErpTransaction exactly. The
    UNIQUE constraint on (idempotency_key, attempt_number) is what
    actually enforces "never double-post the same attempt" at the DB
    level, same rationale as that model's own docstring."""
    __tablename__ = "document_webhook_deliveries"
    __table_args__ = (UniqueConstraint("idempotency_key", "attempt_number", name="uq_document_webhook_idempotency_attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    config_id: Mapped[int] = mapped_column(nullable=False, index=True)  # DocumentWebhookConfig.id, no FK (config row is never deleted, only disabled)

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[DocumentWebhookDeliveryStatus] = mapped_column(Enum(DocumentWebhookDeliveryStatus), default=DocumentWebhookDeliveryStatus.PENDING, index=True)
    http_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    request_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Scrubbed of secrets before persisting, same convention as
    # ApInvoiceErpTransaction.response_json — see connectors/base.py's
    # scrub_secrets(). Text, not JSON: a webhook receiver's response body
    # isn't guaranteed to be JSON at all.
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_retryable: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self) -> str:
        return f"<DocumentWebhookDelivery doc={self.document_id} attempt={self.attempt_number} status={self.status}>"
