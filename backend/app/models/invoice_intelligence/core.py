"""
Accounts-Payable Invoice Intelligence — canonical domain models.

Prefixed `ApInvoice*` (Accounts Payable) to stay clear of the unrelated
`models/billing/invoice.py::Invoice` (this platform's own SaaS
subscription billing invoice — a completely different domain that
happens to share the word "invoice"). Table names are similarly
prefixed `ap_invoice_*`.

This is the "genuinely new" layer described in the Invoice Intelligence
plan — it sits ON TOP of the existing Document/OCR/AI-extraction/Review
Queue backbone (see services/document_service.py, extraction_service.py,
services/document_taxonomies.py's INVOICE taxonomy) rather than
duplicating it. An ApInvoice row is materialized FROM a Document row
(see services/invoice/invoice_processing_service.py) once extraction
completes or a reviewer approves corrections — Document remains the
system of record for the source file/OCR/raw extraction; ApInvoice is
the normalized, queryable, validated, ERP-postable representation built
from it.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase
from app.models.invoice_intelligence.erp import ApInvoiceErpStatus, ApInvoiceReconciliationStatus  # noqa: F401 — re-exported for callers that only import core


class ApInvoiceStatus(str, enum.Enum):
    """Mirrors the state machine from the Technical Architecture doc,
    trimmed to states this platform actually drives end to end."""
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    NORMALIZED = "NORMALIZED"
    VALIDATING = "VALIDATING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY_TO_POST = "READY_TO_POST"
    POSTING = "POSTING"
    POSTED = "POSTED"
    RECONCILED = "RECONCILED"
    REJECTED = "REJECTED"
    EXCEPTION = "EXCEPTION"


class ApInvoiceDecision(str, enum.Enum):
    STRAIGHT_THROUGH = "STRAIGHT_THROUGH"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECT = "REJECT"
    RETRY = "RETRY"


class ApInvoiceSource(str, enum.Enum):
    MANUAL_UPLOAD = "MANUAL_UPLOAD"
    SCHEDULE = "SCHEDULE"
    PIPELINE = "PIPELINE"


class ApInvoiceVendor(TenantBase):
    """Reference/master vendor data — used by Reference and PO validation
    (vendor exists/active). Not the same as a Connections `Connection`
    row; this is business reference data, seeded/edited by AP staff."""
    __tablename__ = "ap_invoice_vendors"
    __table_args__ = ({"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # The ID this vendor is known by in the target ERP (e.g. Oracle Fusion
    # supplier number) — populated manually or via a future ERP-supplier-
    # sync job; None until then, never fabricated.
    erp_supplier_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)  # alternate spellings seen on real invoices, for fuzzy matching
    is_active: Mapped[bool] = mapped_column(default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoiceVendor {self.name}>"


class ApInvoice(TenantBase):
    __tablename__ = "ap_invoices"
    # sqlite_autoincrement: this table accumulates FK children
    # (ApInvoiceLine, ApInvoiceErpTransaction, ApInvoiceValidationResult,
    # ApInvoiceAuditEvent...) — protects against the SQLite id-reuse
    # collision class already found live twice in this project
    # (organizations, pipelines) after raw-SQL test-data cleanup. Same
    # precedent as models/pipeline.py's Pipeline/PipelineVersion.
    __table_args__ = ({"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    # The Document this ApInvoice was materialized from — real FK, same
    # DB (see models/review_queue.py's identical organization_id-no-FK +
    # document_id-real-FK precedent). One Document -> at most one
    # ApInvoice; re-materialization on reprocess/correction UPDATEs the
    # same row rather than inserting a new one (see invoice_processing_service).
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, unique=True, index=True)
    source: Mapped[ApInvoiceSource] = mapped_column(Enum(ApInvoiceSource), default=ApInvoiceSource.MANUAL_UPLOAD)
    source_schedule_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    # Header — names deliberately match services/document_taxonomies.py's
    # INVOICE field names so mapping Document.extraction_result ->
    # ApInvoice is a 1:1 lookup, not a translation layer.
    invoice_number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    invoice_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("ap_invoice_vendors.id"), nullable=True, index=True)
    purchase_order: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Amounts
    subtotal: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount: Mapped[float | None] = mapped_column(Float, nullable=True)
    freight: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Confidence model (Technical Design §3) — kept as discrete named
    # scores, never collapsed into one number, so "high confidence never
    # bypasses business validation" is enforceable in code, not just policy.
    extractor_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    field_consistency: Mapped[float | None] = mapped_column(Float, nullable=True)
    reference_match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    business_validation_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[ApInvoiceStatus] = mapped_column(Enum(ApInvoiceStatus), default=ApInvoiceStatus.RECEIVED, index=True)
    final_decision: Mapped[ApInvoiceDecision | None] = mapped_column(Enum(ApInvoiceDecision), nullable=True)
    decision_reasons: Mapped[list] = mapped_column(JSON, default=list)  # [{rule_id, category, result, message}]

    # Business-duplicate detection (vendor+number+amount+date) — distinct
    # from Document.content_hash's technical byte-for-byte dedup.
    duplicate_of_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("ap_invoices.id"), nullable=True)
    duplicate_check_result: Mapped[str | None] = mapped_column(String(20), nullable=True)  # NONE|WARN|BLOCKED

    erp_connection_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # Connection.id — no FK, mirrors connection_service's own cross-module no-FK convention
    erp_status: Mapped[ApInvoiceErpStatus] = mapped_column(Enum(ApInvoiceErpStatus), default=ApInvoiceErpStatus.NOT_SUBMITTED, index=True)
    erp_transaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    reconciliation_status: Mapped[ApInvoiceReconciliationStatus] = mapped_column(
        Enum(ApInvoiceReconciliationStatus), default=ApInvoiceReconciliationStatus.NOT_STARTED
    )

    # Versions used for THIS row's processing run — reproducibility (Tech
    # Design §42): re-running the same document later with newer
    # rules/mappings never silently changes what an old ApInvoice's
    # decision meant.
    rule_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mapping_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The complete raw AI/extraction response for THIS invoice. Document
    # also gets a generic ai_raw_response column (see INV3 migration),
    # but keeping a copy here means it survives even if the Document is
    # later reprocessed and its own column overwritten.
    ai_raw_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoice {self.invoice_number or self.id} ({self.status})>"


class ApInvoiceLine(TenantBase):
    __tablename__ = "ap_invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ap_invoices.id"), nullable=False, index=True)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax: Mapped[float | None] = mapped_column(Float, nullable=True)
    line_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    purchase_order_line: Mapped[str | None] = mapped_column(String(100), nullable=True)

    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # {page, boundingPolygon, sourceText} where available — see
    # services/invoice/evidence_service.py (INV4). Never fabricated: None
    # until a real bbox is actually computed.
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<ApInvoiceLine invoice={self.invoice_id} #{self.line_number}>"
