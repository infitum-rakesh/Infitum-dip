"""Document Webhook router — /api/document-webhook

Administration-level configuration for pushing Document Intelligence's
processed output to a customer-owned external HTTP endpoint after each
document finishes — see services/document_webhook_service.py's module
docstring for how this differs from Processed Data Storage
(api/document_storage.py). Same single ADMIN-tier gating convention.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User
from app.schemas.document_webhook import (
    DocumentWebhookConfigOut,
    DocumentWebhookConfigUpdateRequest,
    DocumentWebhookDeliveryListOut,
    DocumentWebhookDeliveryOut,
    SigningSecretOut,
    WebhookTestResultOut,
)
from app.services import document_webhook_service as svc

router = APIRouter(prefix="/document-webhook", tags=["document-webhook"])

_admin = require_action("ADMIN", module=None)


def _to_out(row) -> DocumentWebhookConfigOut:
    return DocumentWebhookConfigOut(
        id=row.id, enabled=row.enabled, url=row.url, http_method=row.http_method, auth_type=row.auth_type.value,
        auth_header_name=row.auth_header_name, has_secret=bool(row.encrypted_secret),
        oauth_token_url=row.oauth_token_url, oauth_client_id=row.oauth_client_id,
        has_oauth_client_secret=bool(row.encrypted_oauth_client_secret), oauth_scope=row.oauth_scope,
        custom_headers=row.custom_headers, field_mapping=row.field_mapping, timeout_seconds=row.timeout_seconds,
        include_ocr_text=row.include_ocr_text, include_extraction_fields=row.include_extraction_fields,
        include_dataset=row.include_dataset, include_ai_raw_response=row.include_ai_raw_response,
        last_pushed_document_id=row.last_pushed_document_id, last_pushed_at=row.last_pushed_at,
        last_status=row.last_status.value, last_error=row.last_error, updated_at=row.updated_at,
    )


@router.get("/config", response_model=DocumentWebhookConfigOut)
async def get_config(user: User = Depends(_admin), db: AsyncSession = Depends(get_tenant_db)):
    row = await svc.get_or_create_config(db, user.organization_id)
    return _to_out(row)


@router.put("/config", response_model=DocumentWebhookConfigOut)
async def update_config(
    body: DocumentWebhookConfigUpdateRequest, user: User = Depends(_admin), db: AsyncSession = Depends(get_tenant_db),
):
    row = await svc.get_or_create_config(db, user.organization_id)
    secret = body.secret if body.secret is not None else (None if body.clear_secret else ...)
    oauth_client_secret = body.oauth_client_secret if body.oauth_client_secret is not None else (None if body.clear_oauth_client_secret else ...)
    try:
        row = await svc.update_config(
            db, row, updated_by=user.id, enabled=body.enabled, url=body.url, http_method=body.http_method,
            auth_type=body.auth_type, auth_header_name=body.auth_header_name, secret=secret,
            oauth_token_url=body.oauth_token_url, oauth_client_id=body.oauth_client_id,
            oauth_client_secret=oauth_client_secret, oauth_scope=body.oauth_scope,
            custom_headers=body.custom_headers, field_mapping=body.field_mapping, timeout_seconds=body.timeout_seconds,
            include_ocr_text=body.include_ocr_text, include_extraction_fields=body.include_extraction_fields,
            include_dataset=body.include_dataset, include_ai_raw_response=body.include_ai_raw_response,
        )
    except svc.DocumentWebhookError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return _to_out(row)


@router.get("/config/signing-secret", response_model=SigningSecretOut)
async def get_signing_secret(user: User = Depends(_admin), db: AsyncSession = Depends(get_tenant_db)):
    """Full plaintext signing secret, shown once so the receiver's own
    HMAC verification code can be configured — see service docstring."""
    row = await svc.get_or_create_config(db, user.organization_id)
    return SigningSecretOut(signing_secret=svc.signing_secret_preview(row))


@router.post("/config/test", response_model=WebhookTestResultOut)
async def test_webhook(user: User = Depends(_admin), db: AsyncSession = Depends(get_tenant_db)):
    row = await svc.get_or_create_config(db, user.organization_id)
    try:
        result = await svc.send_test_delivery(row)
    except svc.DocumentWebhookError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return result


@router.get("/deliveries", response_model=DocumentWebhookDeliveryListOut)
async def list_deliveries(
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    user: User = Depends(_admin), db: AsyncSession = Depends(get_tenant_db),
):
    rows, total = await svc.list_deliveries(db, user.organization_id, limit=limit, offset=offset)
    return DocumentWebhookDeliveryListOut(items=[DocumentWebhookDeliveryOut.model_validate(r) for r in rows], total=total)
