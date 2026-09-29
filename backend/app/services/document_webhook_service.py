"""
document_webhook_service — Phase 6 (spec item #7: "push document data
through external APIs"). Pushes the same processed-document payload
Processed Data Storage flattens (services/document_storage_service.py's
_flatten_document(), reused here directly — same shape, same include_*
gating) to a customer-configured external HTTP endpoint instead of a
database/file, with real idempotency + retry/backoff + a per-config
circuit breaker + a signed payload, mirroring services/invoice/
invoice_erp_service.py's design (see models/document_webhook.py's module
docstring for the full rationale on why this is a separate feature from
Processed Data Storage, not a third mode bolted onto it).

Idempotency: idempotency_key is DETERMINISTIC per (document, config) —
`doc-{id}-webhook-{config_id}` — sent as the outbound X-Idempotency-Key
header so a genuinely idempotency-aware receiver can de-dupe on its own
side too, same "belt and suspenders" convention as the ERP posting
service's own Idempotency-Key header.

Signing: every delivery is HMAC-SHA256-signed over the exact JSON bytes
sent, using a per-org signing secret (auto-generated the first time
webhooks are enabled with none configured) — sent as X-Signature so the
receiving endpoint can verify a delivery genuinely came from this
platform. Nothing else in this codebase signs an OUTBOUND payload for a
genuinely third-party (customer-owned) receiver — see model docstring.

Retry classification: transient (network unreachable/timeout, or HTTP
408/429/5xx) -> exponential backoff with jitter, up to MAX_ATTEMPTS.
Permanent (any other 4xx) -> no retry, immediate terminal FAILED. A
transient failure still failing after MAX_ATTEMPTS lands as DEAD_LETTER.

Circuit breaker: small in-process per-config_id state, same shape/
constants as invoice_erp_service.py's — CLOSED -> OPEN after
CIRCUIT_FAILURE_THRESHOLD consecutive retryable failures -> HALF_OPEN
after a cooldown admits one probe. Never persisted, resets on restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import re
import secrets as secrets_module
import time
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import scrub_secrets
from app.core.crypto import decrypt_secret, encrypt_secret
from app.database import tenant_engine_registry
from app.models.document import Document
from app.models.document_webhook import (
    DocumentWebhookAuthType,
    DocumentWebhookConfig,
    DocumentWebhookDelivery,
    DocumentWebhookDeliveryStatus,
    DocumentWebhookPushStatus,
)
from app.services.document_storage_service import _flatten_document  # noqa: SLF001 - intentional shared-helper reuse, same payload shape as Processed Data Storage; see this module's own docstring

MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 60.0
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_COOLDOWN_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 15.0  # DocumentWebhookConfig.timeout_seconds' model default — the per-config value always wins once a config row exists

ALLOWED_HTTP_METHODS = {"POST", "PUT", "PATCH"}

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")

# OAuth2 client_credentials access tokens — in-process cache per config_id,
# same "never persisted, resets on restart" spirit as the circuit breaker
# state above. A token is refetched a bit early (OAUTH_TOKEN_REFRESH_SKEW_SECONDS)
# so an in-flight delivery never gets caught using a token that expires a
# moment later.
OAUTH_TOKEN_REFRESH_SKEW_SECONDS = 30.0
_oauth_token_cache: dict[int, dict] = {}


class DocumentWebhookError(Exception):
    """A genuine caller-facing precondition failure (bad URL, not
    enabled, etc) — never used for a transient delivery failure (those
    are recorded as DocumentWebhookDelivery rows, not raised)."""


# ── Circuit breaker — in-process, per config_id ─────────────────────

_circuit_state: dict[int, dict] = {}


def _breaker_allows(config_id: int) -> bool:
    st = _circuit_state.get(config_id)
    if st is None or st["state"] == "CLOSED":
        return True
    if st["state"] == "OPEN":
        if time.monotonic() - st["opened_at"] >= CIRCUIT_COOLDOWN_SECONDS:
            st["state"] = "HALF_OPEN"
            st["probing"] = True
            return True
        return False
    if st.get("probing"):
        return False
    st["probing"] = True
    return True


def _breaker_record_success(config_id: int) -> None:
    _circuit_state[config_id] = {"state": "CLOSED", "failures": 0, "opened_at": 0.0, "probing": False}


def _breaker_record_failure(config_id: int) -> None:
    st = _circuit_state.setdefault(config_id, {"state": "CLOSED", "failures": 0, "opened_at": 0.0, "probing": False})
    st["failures"] += 1
    st["probing"] = False
    if st["state"] == "HALF_OPEN" or st["failures"] >= CIRCUIT_FAILURE_THRESHOLD:
        st["state"] = "OPEN"
        st["opened_at"] = time.monotonic()


def _breaker_note_reachable(config_id: int) -> None:
    st = _circuit_state.get(config_id)
    if st and st["state"] == "HALF_OPEN":
        _breaker_record_success(config_id)


def circuit_status(config_id: int) -> dict:
    st = _circuit_state.get(config_id, {"state": "CLOSED", "failures": 0, "opened_at": 0.0})
    return dict(st)


# ── Config CRUD ──────────────────────────────────────────────────────

async def get_or_create_config(db: AsyncSession, organization_id: int | None) -> DocumentWebhookConfig:
    stmt = select(DocumentWebhookConfig)
    stmt = stmt.where(DocumentWebhookConfig.organization_id.is_(None)) if organization_id is None \
        else stmt.where(DocumentWebhookConfig.organization_id == organization_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row
    row = DocumentWebhookConfig(organization_id=organization_id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_config(
    db: AsyncSession, row: DocumentWebhookConfig, *, updated_by: int | None,
    enabled: bool | None = None, url: str | None = ..., http_method: str | None = None,
    auth_type: str | None = None, auth_header_name: str | None = None, secret: str | None = ...,
    oauth_token_url: str | None = ..., oauth_client_id: str | None = ..., oauth_client_secret: str | None = ...,
    oauth_scope: str | None = ..., custom_headers: dict | None = ..., field_mapping: dict | None = ...,
    timeout_seconds: float | None = None,
    include_ocr_text: bool | None = None, include_extraction_fields: bool | None = None,
    include_dataset: bool | None = None, include_ai_raw_response: bool | None = None,
) -> DocumentWebhookConfig:
    """url/secret/oauth_*/custom_headers/field_mapping default to Ellipsis
    so "explicitly clear" (None) is distinguishable from "caller didn't
    send this field" (unset), same convention document_storage_service.
    update_config() uses."""
    if url is not ...:
        row.url = url.strip() if url else None
    if http_method is not None:
        cleaned_method = http_method.strip().upper()
        if cleaned_method not in ALLOWED_HTTP_METHODS:
            raise DocumentWebhookError(f"HTTP method must be one of {sorted(ALLOWED_HTTP_METHODS)}, got '{http_method}'.")
        row.http_method = cleaned_method
    if auth_type is not None:
        row.auth_type = DocumentWebhookAuthType(auth_type)
    if auth_header_name is not None:
        cleaned = auth_header_name.strip()
        row.auth_header_name = cleaned if cleaned else "X-API-Key"
    if secret is not ...:
        row.encrypted_secret = encrypt_secret(secret) if secret else None
    if oauth_token_url is not ...:
        row.oauth_token_url = oauth_token_url.strip() if oauth_token_url else None
    if oauth_client_id is not ...:
        row.oauth_client_id = oauth_client_id.strip() if oauth_client_id else None
    if oauth_client_secret is not ...:
        row.encrypted_oauth_client_secret = encrypt_secret(oauth_client_secret) if oauth_client_secret else None
    if oauth_scope is not ...:
        row.oauth_scope = oauth_scope.strip() if oauth_scope else None
    if custom_headers is not ...:
        if custom_headers and not all(isinstance(k, str) and isinstance(v, str) for k, v in custom_headers.items()):
            raise DocumentWebhookError("Custom headers must be a flat map of string names to string values.")
        row.custom_headers = custom_headers or None
    if field_mapping is not ...:
        if field_mapping and not all(isinstance(k, str) and isinstance(v, str) for k, v in field_mapping.items()):
            raise DocumentWebhookError("Field mapping must be a flat map of string payload keys to string output keys.")
        row.field_mapping = field_mapping or None
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise DocumentWebhookError("Timeout must be a positive number of seconds.")
        row.timeout_seconds = timeout_seconds
    if include_ocr_text is not None:
        row.include_ocr_text = include_ocr_text
    if include_extraction_fields is not None:
        row.include_extraction_fields = include_extraction_fields
    if include_dataset is not None:
        row.include_dataset = include_dataset
    if include_ai_raw_response is not None:
        row.include_ai_raw_response = include_ai_raw_response
    if enabled is not None:
        if enabled:
            if not row.url:
                raise DocumentWebhookError("Set a webhook URL before enabling it.")
            if row.auth_type == DocumentWebhookAuthType.OAUTH2:
                if not (row.oauth_token_url and row.oauth_client_id and row.encrypted_oauth_client_secret):
                    raise DocumentWebhookError("Set an OAuth2 token URL, client ID, and client secret before enabling this auth type.")
            elif row.auth_type != DocumentWebhookAuthType.NONE and not row.encrypted_secret:
                raise DocumentWebhookError("Set an auth secret before enabling this auth type, or switch auth type to None.")
            if not row.encrypted_signing_secret:
                # Auto-generate on first enable — a receiver needs this
                # for every future delivery's X-Signature, so it must
                # exist before the first real push can happen.
                row.encrypted_signing_secret = encrypt_secret(secrets_module.token_hex(32))
        row.enabled = enabled
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


def signing_secret_preview(row: DocumentWebhookConfig) -> str | None:
    """Full plaintext signing secret — shown ONCE in the settings UI (the
    receiver's own verification code needs the real value, unlike an
    auth secret which the platform only ever sends, never displays back).
    Same "show it, don't just mask it" treatment webhook providers like
    Stripe use for their own signing secrets."""
    if not row.encrypted_signing_secret:
        return None
    return decrypt_secret(row.encrypted_signing_secret)


# ── Error classification ─────────────────────────────────────────────

def _classify_error(exc: Exception) -> tuple[bool, str, str]:
    """Returns (is_retryable, error_code, message)."""
    message = str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retryable = status in (408, 429) or status >= 500
        return retryable, str(status), f"HTTP {status}: {message}"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return True, "NETWORK", message
    if isinstance(exc, httpx.RequestError):
        return True, "NETWORK", message
    return False, "UNKNOWN", message


def _idempotency_key(document_id: int, config_id: int) -> str:
    return f"doc-{document_id}-webhook-{config_id}"


async def _next_attempt_number(db: AsyncSession, idempotency_key: str) -> int:
    result = await db.execute(select(func.max(DocumentWebhookDelivery.attempt_number)).where(DocumentWebhookDelivery.idempotency_key == idempotency_key))
    return (result.scalar_one() or 0) + 1


async def _oauth2_access_token(config: DocumentWebhookConfig) -> str | None:
    """Real client_credentials grant against config.oauth_token_url,
    cached in-process per config_id until near expiry (see
    OAUTH_TOKEN_REFRESH_SKEW_SECONDS) so every delivery attempt doesn't
    re-authenticate. A cached-but-about-to-expire token is refetched
    rather than risking a 401 mid-delivery. Returns None (never raises)
    on a failed token fetch — the caller falls through to sending the
    request with no Authorization header, which fails clearly at the
    receiver rather than masking the real problem behind a different
    error here."""
    cached = _oauth_token_cache.get(config.id)
    if cached and cached["expires_at"] - time.monotonic() > OAUTH_TOKEN_REFRESH_SKEW_SECONDS:
        return cached["access_token"]
    if not (config.oauth_token_url and config.oauth_client_id and config.encrypted_oauth_client_secret):
        return None
    client_secret = decrypt_secret(config.encrypted_oauth_client_secret)
    if not client_secret:
        return None
    data = {"grant_type": "client_credentials", "client_id": config.oauth_client_id, "client_secret": client_secret}
    if config.oauth_scope:
        data["scope"] = config.oauth_scope
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(config.oauth_token_url, data=data)
            resp.raise_for_status()
            token_response = resp.json()
    except Exception:  # noqa: BLE001 - a failed token fetch degrades to "no auth header", not a crash
        return None
    access_token = token_response.get("access_token")
    if not access_token:
        return None
    expires_in = token_response.get("expires_in", 3600)
    _oauth_token_cache[config.id] = {"access_token": access_token, "expires_at": time.monotonic() + float(expires_in)}
    return access_token


async def _auth_headers(config: DocumentWebhookConfig) -> dict:
    if config.auth_type == DocumentWebhookAuthType.OAUTH2:
        token = await _oauth2_access_token(config)
        return {"Authorization": f"Bearer {token}"} if token else {}
    if config.auth_type == DocumentWebhookAuthType.NONE or not config.encrypted_secret:
        return {}
    secret = decrypt_secret(config.encrypted_secret)
    if not secret:
        return {}
    if config.auth_type == DocumentWebhookAuthType.API_KEY:
        return {config.auth_header_name: secret}
    if config.auth_type == DocumentWebhookAuthType.BEARER:
        return {"Authorization": f"Bearer {secret}"}
    if config.auth_type == DocumentWebhookAuthType.BASIC:
        import base64
        return {"Authorization": f"Basic {base64.b64encode(secret.encode()).decode()}"}
    return {}


def _apply_field_mapping(payload: dict, field_mapping: dict | None) -> dict:
    """Renames outbound payload keys per config.field_mapping (spec item
    #7's "JSON mapping") — {"document_id": "docId"} sends "docId" instead
    of "document_id" in the wire payload. A key not present in the
    mapping passes through under its original name, so a partial mapping
    only renames what it explicitly lists."""
    if not field_mapping:
        return payload
    return {field_mapping.get(key, key): value for key, value in payload.items()}


def _sign(config: DocumentWebhookConfig, body: bytes) -> str | None:
    if not config.encrypted_signing_secret:
        return None
    signing_secret = decrypt_secret(config.encrypted_signing_secret)
    if not signing_secret:
        return None
    return hmac.new(signing_secret.encode(), body, hashlib.sha256).hexdigest()


# ── Public entry point ───────────────────────────────────────────────

async def push_document(db: AsyncSession, doc: Document, *, organization_id: int | None) -> None:
    """Called once, right after a document finishes Document Intelligence
    processing (see document_service.py — called independently of, and
    with the same COMPLETED/NEEDS_REVIEW gate as, document_storage_
    service.push_document(); a webhook failure never affects the DB/file
    push and vice versa). No-ops instantly when disabled — the common
    case. Never raises past itself: hands off to a detached background
    retry loop and returns immediately, same non-blocking convention as
    document_service.start_processing() / invoice_erp_service.post_invoice()."""
    config = await get_or_create_config(db, organization_id)
    if not config.enabled or not config.url:
        return

    idempotency_key = _idempotency_key(doc.id, config.id)
    attempt_number = await _next_attempt_number(db, idempotency_key)
    correlation_id = uuid.uuid4().hex

    delivery = DocumentWebhookDelivery(
        organization_id=organization_id, document_id=doc.id, config_id=config.id,
        idempotency_key=idempotency_key, attempt_number=attempt_number,
        status=DocumentWebhookDeliveryStatus.PENDING,
    )
    db.add(delivery)
    await db.commit()

    asyncio.create_task(_push_in_background(
        doc.id, organization_id, config.id, correlation_id, idempotency_key, attempt_number,
    ))


async def _record_delivery(
    db: AsyncSession, *, organization_id: int | None, document_id: int, config_id: int,
    idempotency_key: str, attempt_number: int, status: DocumentWebhookDeliveryStatus,
    request_json: dict | None = None, response_text: str | None = None, http_status_code: int | None = None,
    error_code: str | None = None, error_message: str | None = None, is_retryable: bool = False,
) -> DocumentWebhookDelivery:
    """Upsert by (idempotency_key, attempt_number) — push_document()
    already inserted a PENDING row at this exact key before handing off
    to the background loop, so this must update that same row rather
    than violate the UNIQUE constraint with a second insert."""
    result = await db.execute(
        select(DocumentWebhookDelivery).where(
            DocumentWebhookDelivery.idempotency_key == idempotency_key, DocumentWebhookDelivery.attempt_number == attempt_number,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = DocumentWebhookDelivery(
            organization_id=organization_id, document_id=document_id, config_id=config_id,
            idempotency_key=idempotency_key, attempt_number=attempt_number,
        )
        db.add(row)
    row.status = status
    row.http_status_code = http_status_code
    row.request_json = request_json
    row.response_text = response_text[:2000] if response_text else None
    row.error_code = error_code
    row.error_message = error_message[:2000] if error_message else None
    row.is_retryable = is_retryable
    await db.commit()
    await db.refresh(row)
    return row


async def _finalize_config(db: AsyncSession, config_id: int, document_id: int, status: DocumentWebhookPushStatus, error: str | None) -> None:
    config = await db.get(DocumentWebhookConfig, config_id)
    if config is None:
        return
    config.last_pushed_document_id = document_id
    config.last_pushed_at = datetime.now(timezone.utc)
    config.last_status = status
    config.last_error = error
    await db.commit()


async def _report_dead_letter_incident(db: AsyncSession, *, organization_id: int | None, document_id: int, reason: str) -> None:
    """Best-effort Incident + Alert on DEAD_LETTER, same convention as
    invoice_erp_service._report_dead_letter_incident() — never allowed to
    affect the real delivery outcome it's reporting on."""
    try:
        from app.models.incident import IncidentSeverity, IncidentSource
        from app.monitoring import alerts
        from app.services import incident_service

        incident = await incident_service.report_incident(
            db, organization_id=organization_id, source=IncidentSource.DOCUMENT_WEBHOOK.value,
            title=f"Document webhook delivery dead-lettered — document #{document_id}",
            description=reason, severity=IncidentSeverity.MEDIUM.value,
            dataset_name=f"document #{document_id}",
        )
        fired = await alerts.evaluate_rules(db, organization_id=organization_id, ctx={
            "event": "DOCUMENT_WEBHOOK_FAILED",
            "message": f"Document #{document_id} — webhook delivery dead-lettered: {reason}",
        })
        if fired:
            for event in fired:
                if event.incident_id is None:
                    event.incident_id = incident.id
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - monitoring/incident wiring must never fail a real delivery outcome
        import logging
        logging.getLogger(__name__).error("document_webhook_service dead-letter incident hook failed for document %s: %s", document_id, exc)


async def _push_in_background(
    document_id: int, organization_id: int | None, config_id: int,
    correlation_id: str, idempotency_key: str, start_attempt: int,
) -> None:
    """Runs detached from the request — needs its own session, same
    convention as invoice_erp_service._post_in_background()."""
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        attempt = start_attempt
        while attempt < start_attempt + MAX_ATTEMPTS:
            if not _breaker_allows(config_id):
                await _record_delivery(
                    db, organization_id=organization_id, document_id=document_id, config_id=config_id,
                    idempotency_key=idempotency_key, attempt_number=attempt,
                    status=DocumentWebhookDeliveryStatus.DEAD_LETTER, error_code="CIRCUIT_OPEN",
                    error_message="This webhook's circuit breaker is open — too many recent failures. Try again later or after fixing the endpoint.",
                    is_retryable=True,
                )
                await _finalize_config(db, config_id, document_id, DocumentWebhookPushStatus.FAILED, "Circuit breaker open — too many recent failures.")
                await _report_dead_letter_incident(
                    db, organization_id=organization_id, document_id=document_id,
                    reason="Blocked by an open circuit breaker — too many recent webhook delivery failures.",
                )
                return

            config = await db.get(DocumentWebhookConfig, config_id)
            doc = await db.get(Document, document_id)
            if config is None or doc is None or not config.enabled or not config.url:
                return

            payload = _flatten_document(doc, config)
            payload["idempotency_key"] = idempotency_key
            wire_payload = _apply_field_mapping(payload, config.field_mapping)
            body = json.dumps(wire_payload, default=str).encode()
            # Custom headers apply first so they can never clobber the
            # idempotency/content-type/auth/signature headers layered on
            # top of them — see model docstring on custom_headers.
            headers = dict(config.custom_headers or {})
            headers["Content-Type"] = "application/json"
            headers["X-Idempotency-Key"] = idempotency_key
            headers.update(await _auth_headers(config))
            signature = _sign(config, body)
            if signature:
                headers["X-Signature"] = f"sha256={signature}"

            secrets_to_scrub = {"secret": decrypt_secret(config.encrypted_secret)} if config.encrypted_secret else {}
            try:
                async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                    resp = await client.request(config.http_method, config.url, content=body, headers=headers)
                    resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - classified below; every branch is a real, recorded outcome
                retryable, error_code, message = _classify_error(exc)
                scrubbed = scrub_secrets(message, secrets_to_scrub)
                if retryable:
                    _breaker_record_failure(config_id)
                elif error_code not in ("UNKNOWN", "NETWORK"):
                    _breaker_note_reachable(config_id)
                will_retry = retryable and (attempt + 1) < start_attempt + MAX_ATTEMPTS
                response_body = None
                status_code = None
                if isinstance(exc, httpx.HTTPStatusError):
                    status_code = exc.response.status_code
                    response_body = scrub_secrets(exc.response.text, secrets_to_scrub)
                await _record_delivery(
                    db, organization_id=organization_id, document_id=document_id, config_id=config_id,
                    idempotency_key=idempotency_key, attempt_number=attempt,
                    status=DocumentWebhookDeliveryStatus.RETRYING if will_retry else (DocumentWebhookDeliveryStatus.DEAD_LETTER if retryable else DocumentWebhookDeliveryStatus.FAILED),
                    request_json=wire_payload, response_text=response_body, http_status_code=status_code,
                    error_code=error_code, error_message=scrubbed, is_retryable=retryable,
                )
                if not will_retry:
                    final = DocumentWebhookPushStatus.FAILED
                    await _finalize_config(db, config_id, document_id, final, scrubbed)
                    if retryable:
                        await _report_dead_letter_incident(
                            db, organization_id=organization_id, document_id=document_id,
                            reason=f"Retries exhausted after {attempt - start_attempt + 1} attempt(s) — last error: {scrubbed}",
                        )
                    return
                delay = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2 ** (attempt - start_attempt))) * (1.0 + random.random() * 0.25)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            # Success — real HTTP response, never synthesized.
            _breaker_record_success(config_id)
            await _record_delivery(
                db, organization_id=organization_id, document_id=document_id, config_id=config_id,
                idempotency_key=idempotency_key, attempt_number=attempt,
                status=DocumentWebhookDeliveryStatus.SUCCEEDED, request_json=wire_payload,
                response_text=scrub_secrets(resp.text, secrets_to_scrub), http_status_code=resp.status_code,
            )
            await _finalize_config(db, config_id, document_id, DocumentWebhookPushStatus.SUCCESS, None)
            return


# ── Test / history ───────────────────────────────────────────────────

async def send_test_delivery(config: DocumentWebhookConfig) -> dict:
    """Real, synchronous test POST — used by the "Test Webhook" button.
    Sends a small synthetic payload (never a real document's data) so an
    admin can verify the URL/auth/signature setup without waiting on a
    real document to finish processing. Never touches DocumentWebhookDelivery
    history or last_status — this is a dry connectivity check, same
    spirit as connection_service.test_connection()."""
    if not config.url:
        raise DocumentWebhookError("No webhook URL is configured.")
    test_payload = {
        "event": "test",
        "document_id": 0,
        "message": "This is a test delivery from the Data Intelligence Platform.",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    wire_test_payload = _apply_field_mapping(test_payload, config.field_mapping)
    body = json.dumps(wire_test_payload, default=str).encode()
    headers = dict(config.custom_headers or {})
    headers["Content-Type"] = "application/json"
    headers["X-Idempotency-Key"] = f"test-{uuid.uuid4().hex}"
    headers.update(await _auth_headers(config))
    signature = _sign(config, body)
    if signature:
        headers["X-Signature"] = f"sha256={signature}"
    secrets_to_scrub = {"secret": decrypt_secret(config.encrypted_secret)} if config.encrypted_secret else {}
    try:
        async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
            resp = await client.request(config.http_method, config.url, content=body, headers=headers)
        return {
            "success": 200 <= resp.status_code < 300,
            "status_code": resp.status_code,
            "response_body": scrub_secrets(resp.text[:1000], secrets_to_scrub),
        }
    except Exception as exc:  # noqa: BLE001 - a failed test call is a normal, informative result, not a server error
        return {"success": False, "status_code": None, "response_body": scrub_secrets(str(exc), secrets_to_scrub)}


async def list_deliveries(db: AsyncSession, organization_id: int | None, *, limit: int = 50, offset: int = 0) -> tuple[list[DocumentWebhookDelivery], int]:
    base = select(DocumentWebhookDelivery)
    base = base.where(DocumentWebhookDelivery.organization_id.is_(None)) if organization_id is None \
        else base.where(DocumentWebhookDelivery.organization_id == organization_id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(DocumentWebhookDelivery.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    return list(rows), total
