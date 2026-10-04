from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class TallyCompanyOut(BaseModel):
    name: str
    guid: str | None = None
    is_default: bool = False


class TallyCompanyListOut(BaseModel):
    items: list[TallyCompanyOut]


# ── Extraction (TALLY-B) ────────────────────────────────────────────

class TallyExtractionStartRequest(BaseModel):
    extraction_type: str  # TallyExtractionType value
    company: str
    voucher_type: str | None = None  # also carries party_type ("SUPPLIER"/"CUSTOMER") for PARTY_MASTER
    party: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    mode: str = "FULL"  # TallyExtractionMode value


class TallyExtractionRunOut(BaseModel):
    id: int
    connection_id: int
    company_name: str
    extraction_type: str
    voucher_type: str | None
    party_filter: str | None
    from_date: date | None
    to_date: date | None
    mode: str
    status: str
    checkpoint_date: date | None
    records_fetched: int
    chunks_total: int
    chunks_failed: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TallyExtractionRunListOut(BaseModel):
    items: list[TallyExtractionRunOut]
    total: int
    page: int
    page_size: int


class TallyExtractionRecordOut(BaseModel):
    id: int
    run_id: int
    extraction_type: str
    external_ref: str | None
    record_date: date | None
    raw_data: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class TallyExtractionRecordListOut(BaseModel):
    items: list[TallyExtractionRecordOut]
    total: int
    page: int
    page_size: int


# ── Canonical accounting model (TALLY-C) ────────────────────────────

class TallyCanonicalVoucherOut(BaseModel):
    id: int
    connection_id: int
    company_name: str
    guid: str
    alter_id: int | None
    voucher_number: str | None
    voucher_type: str | None
    voucher_date: date | None
    party_ledger_name: str | None
    narration: str | None
    direction: str | None
    total_amount: float | None
    ledger_entries: list
    first_seen_at: datetime
    last_synced_at: datetime

    model_config = {"from_attributes": True}


class TallyCanonicalVoucherListOut(BaseModel):
    items: list[TallyCanonicalVoucherOut]
    total: int
    page: int
    page_size: int


class TallyCanonicalPartyOut(BaseModel):
    id: int
    connection_id: int
    company_name: str
    guid: str
    alter_id: int | None
    name: str
    normalized_name: str
    parent: str | None
    party_type: str | None
    gstin: str | None
    state: str | None
    address_lines: list
    email: str | None
    mobile: str | None
    opening_balance: float | None
    closing_balance: float | None
    first_seen_at: datetime
    last_synced_at: datetime

    model_config = {"from_attributes": True}


class TallyCanonicalPartyListOut(BaseModel):
    items: list[TallyCanonicalPartyOut]
    total: int
    page: int
    page_size: int


class TallyCanonicalLedgerOut(BaseModel):
    id: int
    connection_id: int
    company_name: str
    guid: str
    alter_id: int | None
    name: str
    parent: str | None
    opening_balance: float | None
    closing_balance: float | None
    first_seen_at: datetime
    last_synced_at: datetime

    model_config = {"from_attributes": True}


class TallyCanonicalLedgerListOut(BaseModel):
    items: list[TallyCanonicalLedgerOut]
    total: int
    page: int
    page_size: int


class TallyRematerializeResultOut(BaseModel):
    vouchers: int
    parties: int
    ledgers: int
    skipped_no_identity: int


# ── Accounting mapping + voucher preview (TALLY-D) ──────────────────

class TallyAccountMappingRowOut(BaseModel):
    id: int
    canonical_field: str
    tally_field: str
    is_line_mapping: bool
    default_value: str | None
    transform_config: dict[str, Any]
    version: int
    is_current: bool

    model_config = {"from_attributes": True}


class TallyAccountMappingListOut(BaseModel):
    items: list[TallyAccountMappingRowOut]


class TallyAccountMappingRowIn(BaseModel):
    canonical_field: str
    tally_field: str
    is_line_mapping: bool = False
    default_value: str | None = None
    transform_config: dict[str, Any] = {}


class TallyAccountMappingPublishRequest(BaseModel):
    rows: list[TallyAccountMappingRowIn]


class TallyVoucherPreviewRequest(BaseModel):
    invoice_id: int
    company: str


class TallyVoucherValidationResultOut(BaseModel):
    id: int
    category: str
    result: str
    message: str
    details: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class TallyVoucherPreviewOut(BaseModel):
    id: int
    connection_id: int
    invoice_id: int
    company_name: str
    mapping_version: int | None
    voucher_type: str | None
    voucher_date: date | None
    party_ledger_name: str | None
    narration: str | None
    ledger_entries: list
    voucher_xml: str | None
    idempotency_key: str | None
    status: str
    approval_status: str = "NOT_SUBMITTED"  # TALLY-H
    approval_workflow_id: int | None = None
    write_status: str = "NOT_WRITTEN"  # TALLY-I
    tally_voucher_guid: str | None = None
    tally_master_id: str | None = None
    tally_voucher_number: str | None = None
    written_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    validation_results: list[TallyVoucherValidationResultOut] = []

    model_config = {"from_attributes": True}


class TallyVoucherPreviewListOut(BaseModel):
    items: list[TallyVoucherPreviewOut]
    total: int
    page: int
    page_size: int


class TallyVoucherSubmitForApprovalOut(BaseModel):
    """TALLY-H — result of submitting a READY voucher preview for
    write-back approval. `workflow_id` is always set (create_workflow()
    never returns a bare None — see its own docstring); approval_status
    already reflects the terminal AUTO_APPROVED outcome when no
    TALLY_VOUCHER-scoped ApprovalRule is configured for this org."""
    workflow_id: int
    workflow_status: str
    approval_status: str


# ── Controlled write-back (TALLY-I) ─────────────────────────────────

class TallyWriteBackTransactionOut(BaseModel):
    id: int
    preview_id: int
    connection_id: int | None
    correlation_id: str
    idempotency_key: str
    attempt_number: int
    status: str
    tally_voucher_guid: str | None
    tally_master_id: str | None
    tally_voucher_number: str | None
    error_code: str | None
    error_message: str | None
    is_retryable: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TallyWriteBackTransactionListOut(BaseModel):
    items: list[TallyWriteBackTransactionOut]


class TallyWriteBackVerificationOut(BaseModel):
    id: int
    preview_id: int
    transaction_id: int | None
    status: str
    field_comparisons: list
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TallyWriteBackVerificationListOut(BaseModel):
    items: list[TallyWriteBackVerificationOut]


class TallyWriteBackStartOut(BaseModel):
    transaction_id: int
    write_status: str


class TallyReconciliationStartRequest(BaseModel):
    company: str
    tolerance: float | None = None


class TallyReconciliationRunOut(BaseModel):
    id: int
    connection_id: int
    company_name: str
    tolerance: float
    status: str
    error_message: str | None
    source_count: int | None
    target_count: int | None
    matched_count: int | None
    missing_count: int | None
    extra_count: int | None
    mismatched_count: int | None
    ambiguous_count: int | None
    duplicate_target_count: int | None
    ledger_balance_mismatch_count: int
    payment_voucher_missing_count: int
    schedule_id: int | None = None
    trigger: str = "Manual"
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TallyReconciliationRunListOut(BaseModel):
    items: list[TallyReconciliationRunOut]
    total: int


class TallyReconciliationExceptionOut(BaseModel):
    id: int
    run_id: int
    exception_type: str
    invoice_id: int | None
    voucher_id: int | None
    party_name: str | None
    column: str | None
    source_value: str | None
    target_value: str | None
    message: str
    details: dict[str, Any]
    review_item_id: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TallyReconciliationExceptionListOut(BaseModel):
    items: list[TallyReconciliationExceptionOut]
    total: int
