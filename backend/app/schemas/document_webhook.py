from datetime import datetime

from pydantic import BaseModel


class DocumentWebhookConfigOut(BaseModel):
    id: int
    enabled: bool
    url: str | None
    http_method: str
    auth_type: str
    auth_header_name: str
    has_secret: bool
    oauth_token_url: str | None
    oauth_client_id: str | None
    has_oauth_client_secret: bool
    oauth_scope: str | None
    custom_headers: dict | None
    field_mapping: dict | None
    timeout_seconds: float
    include_ocr_text: bool
    include_extraction_fields: bool
    include_dataset: bool
    include_ai_raw_response: bool
    last_pushed_document_id: int | None
    last_pushed_at: datetime | None
    last_status: str
    last_error: str | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentWebhookConfigUpdateRequest(BaseModel):
    enabled: bool | None = None
    url: str | None = None
    http_method: str | None = None  # "POST" | "PUT" | "PATCH"
    auth_type: str | None = None  # "NONE" | "API_KEY" | "BEARER" | "BASIC" | "OAUTH2"
    auth_header_name: str | None = None
    secret: str | None = None
    clear_secret: bool = False
    oauth_token_url: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    clear_oauth_client_secret: bool = False
    oauth_scope: str | None = None
    custom_headers: dict | None = None
    field_mapping: dict | None = None
    timeout_seconds: float | None = None
    include_ocr_text: bool | None = None
    include_extraction_fields: bool | None = None
    include_dataset: bool | None = None
    include_ai_raw_response: bool | None = None


class SigningSecretOut(BaseModel):
    signing_secret: str | None


class WebhookTestResultOut(BaseModel):
    success: bool
    status_code: int | None
    response_body: str | None


class DocumentWebhookDeliveryOut(BaseModel):
    id: int
    document_id: int
    idempotency_key: str
    attempt_number: int
    status: str
    http_status_code: int | None
    error_code: str | None
    error_message: str | None
    is_retryable: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentWebhookDeliveryListOut(BaseModel):
    items: list[DocumentWebhookDeliveryOut]
    total: int
