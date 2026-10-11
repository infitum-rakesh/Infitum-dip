from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Header / list ────────────────────────────────────────────────

class ApInvoiceOut(BaseModel):
    id: int
    organization_id: int | None
    document_id: int
    source: str
    invoice_number: str | None
    invoice_date: datetime | None
    due_date: datetime | None
    vendor_name: str | None
    vendor_id: int | None
    purchase_order: str | None
    currency: str | None
    payment_terms: str | None
    subtotal: float | None
    tax: float | None
    discount: float | None
    freight: float | None
    total: float | None
    extractor_confidence: float | None
    evidence_quality: float | None
    field_consistency: float | None
    reference_match_score: float | None
    business_validation_score: float | None
    status: str
    final_decision: str | None
    decision_reasons: list[dict] = []
    duplicate_of_invoice_id: int | None
    duplicate_check_result: str | None
    erp_status: str
    erp_transaction_id: str | None
    reconciliation_status: str
    created_at: datetime
    updated_at: datetime
    # Convenience fields not on the ORM row directly — filled in by the
    # route/service, not part of ApInvoice itself.
    document_filename: str | None = None

    model_config = {"from_attributes": True}


class ApInvoiceListOut(BaseModel):
    items: list[ApInvoiceOut]
    total: int
    page: int
    page_size: int


class ApInvoiceAmountConvertedOut(BaseModel):
    converted_total: float | None = None
    display_currency: str
    included_currencies: list[str] = []
    excluded_currencies: list[str] = []
    error: str | None = None  # set instead of converted_total when live rates couldn't be fetched


class ApInvoiceStatsOut(BaseModel):
    total: int
    by_status: dict[str, int]
    by_decision: dict[str, int]
    needs_review: int
    straight_through: int
    rejected: int
    duplicate_blocked: int
    # Real per-currency sums — replaces the old single "total_amount" field,
    # which summed every invoice's total regardless of currency and
    # mislabeled the mixed result "$" (found live: USD + INR + AUD invoices
    # in the same org). "UNKNOWN" buckets invoices with no currency recorded.
    total_by_currency: dict[str, float]
    # Present only when the caller passed ?display_currency=... — a real,
    # live-rate conversion of total_by_currency into one number, never a
    # guessed/fabricated rate.
    total_amount_converted: ApInvoiceAmountConvertedOut | None = None


# ── Detail ───────────────────────────────────────────────────────

class ApInvoiceLineOut(BaseModel):
    id: int
    invoice_id: int
    line_number: int
    description: str | None
    quantity: float | None
    unit_price: float | None
    tax: float | None
    line_total: float | None
    purchase_order_line: str | None
    confidence: float | None
    evidence: dict | None

    model_config = {"from_attributes": True}


class ApInvoiceValidationResultOut(BaseModel):
    id: int
    invoice_id: int
    rule_id: int | None
    rule_version: int | None
    category: str
    result: str
    message: str
    details: dict = {}
    created_at: datetime

    model_config = {"from_attributes": True}


class ApInvoiceDetailOut(ApInvoiceOut):
    lines: list[ApInvoiceLineOut] = []
    validation_results: list[ApInvoiceValidationResultOut] = []
    document_status: str | None = None
    document_mime_type: str | None = None


# ── Sub-resources ────────────────────────────────────────────────

class ApInvoiceEvidenceFieldOut(BaseModel):
    name: str | None
    value: Any = None
    confidence: float | None = None
    page: int | None = None
    # Real shape as stored by evidence_service.py (INV4) / custom_engine.py
    # is [x, y, w, h] (a plain coordinate list), not a dict — Any here
    # rather than a stricter type so this never fights the real data.
    bbox: Any = None
    missing: bool = False


class ApInvoiceEvidenceOut(BaseModel):
    document_id: int
    fields: list[ApInvoiceEvidenceFieldOut]


class ApInvoiceAuditEntryOut(BaseModel):
    id: int
    action: str
    old_value: str | None
    new_value: str | None
    reason: str | None
    user_id: int | None
    user_name: str | None
    created_at: datetime


class ApInvoiceHistoryOut(BaseModel):
    document_id: int
    timeline: list[dict] = []
    decision_reasons: list[dict] = []
    final_decision: str | None
    status: str


# ── Requests ─────────────────────────────────────────────────────

class ApInvoiceCorrectionItem(BaseModel):
    field_name: str
    corrected_value: str
    reason: str | None = None


class ApInvoiceCorrectionRequest(BaseModel):
    corrections: list[ApInvoiceCorrectionItem]


class ApInvoiceUploadOut(BaseModel):
    document_id: int
    filename: str
    status: str
    duplicate_of_id: int | None = None


# ── ERP posting (INV9) ───────────────────────────────────────────

class ApInvoiceErpTransactionOut(BaseModel):
    id: int
    invoice_id: int
    erp_connection_id: int | None
    correlation_id: str
    idempotency_key: str
    attempt_number: int
    status: str
    erp_transaction_id: str | None
    request_json: dict | None
    response_json: dict | None
    error_code: str | None
    error_message: str | None
    is_retryable: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ApInvoicePostRequest(BaseModel):
    erp_connection_id: int


# ── ERP posting preview / simulation mode (INV18) ─────────────────

class ApInvoiceErpPreviewOut(BaseModel):
    erp_connection_id: int
    erp_connection_name: str
    idempotency_key: str
    endpoint_host: str | None = None
    mapping_version: int | None = None
    payload: dict


# ── ERP mapping (INV9) ───────────────────────────────────────────

class ApInvoiceErpMappingOut(BaseModel):
    id: int
    erp_connection_id: int | None
    canonical_field: str
    erp_field: str
    is_line_mapping: bool
    default_value: str | None
    transform_config: dict = {}
    version: int
    is_current: bool

    model_config = {"from_attributes": True}


class ApInvoiceErpMappingRowIn(BaseModel):
    canonical_field: str
    erp_field: str
    is_line_mapping: bool = False
    default_value: str | None = None
    transform_config: dict = {}


class ApInvoiceErpMappingPublishRequest(BaseModel):
    erp_connection_id: int
    rows: list[ApInvoiceErpMappingRowIn]


# ── Reconciliation (INV10) ───────────────────────────────────────

class ApInvoiceReconciliationOut(BaseModel):
    id: int
    invoice_id: int
    erp_transaction_id: int | None
    status: str
    field_comparisons: list[dict] = []
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Learning / evaluation (INV13) ─────────────────────────────────

class ApInvoiceAuditEventOut(BaseModel):
    id: int
    invoice_id: int
    event_type: str
    message: str | None
    details: dict = {}
    actor_id: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApInvoiceLearningRecordOut(BaseModel):
    id: int
    invoice_id: int
    review_audit_log_id: int | None
    field_name: str
    original_value: str | None
    corrected_value: str | None
    vendor_id: int | None
    document_layout_hint: str | None
    reviewer_id: int | None
    approved_for_learning: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ApInvoiceLearningRecordListOut(BaseModel):
    items: list[ApInvoiceLearningRecordOut]
    total: int
    page: int
    page_size: int


class ApInvoiceEvaluationMetricsOut(BaseModel):
    total_decided: int
    straight_through_count: int
    straight_through_rate_pct: float | None
    exception_count: int
    exception_rate_pct: float | None
    total_extracted_fields: int
    total_corrections: int
    field_accuracy_pct: float | None


class ApInvoiceRecurringCorrectionByFieldOut(BaseModel):
    field_name: str
    count: int


class ApInvoiceRecurringCorrectionByVendorOut(BaseModel):
    vendor_id: int
    vendor_name: str
    count: int


# ── Validation rules (INV17) ──────────────────────────────────────

class ApInvoiceValidationRuleOut(BaseModel):
    id: int
    organization_id: int | None
    name: str
    category: str
    severity: str
    config: dict
    message: str
    enabled: bool
    version: int
    is_current: bool
    effective_date: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class ApInvoiceValidationRulePublishRequest(BaseModel):
    name: str
    category: str
    severity: str
    config: dict = {}
    message: str
    enabled: bool = True


class ApInvoiceValidationRuleToggleRequest(BaseModel):
    enabled: bool
