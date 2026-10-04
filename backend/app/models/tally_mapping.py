"""
TALLY-D — Accounting Mapping Layer + voucher preview + pre-write
validation. Nothing in this module (or the services built on top of it)
ever sends a write request to Tally — building the exact voucher an
ApInvoice WOULD become, and validating it deterministically, is
deliberately separated from actually transmitting it: that happens only
in tally_writeback_service.py (TALLY-I), gated by TALLY-H's approval
workflow, once TALLY-E's reconciliation/duplicate-detection pass has run.

TallyAccountMapping follows the exact same versioned-row convention as
Invoice Intelligence's ApInvoiceErpMapping (models/invoice_intelligence/
rules.py) — canonical ApInvoice field -> Tally voucher slot, one row per
field per version, publishing inserts a fresh batch rather than mutating
published rows in place. TallyVoucherPreview/TallyVoucherValidationResult
mirror ApInvoice/ApInvoiceValidationResult's shape for the same reason:
this is the established pattern this codebase already uses for
"versioned field mapping" and "one row per check per run" — reusing it
here rather than inventing a third variant.

Pre-write validation is intentionally NOT a user-editable rule table
like ApInvoiceValidationRule: the checks here (double-entry balance,
ledger existence, GST mutual-exclusivity) are voucher-structural
invariants, not org-configurable business policy — see
services/tally_voucher_validation_service.py for the fixed, deterministic
check set and the reasoning for keeping it code rather than config.
"""
import enum
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase
from app.models.tally_writeback import TallyWriteBackStatus


class TallyAccountMapping(TenantBase):
    """Canonical ApInvoice field -> Tally voucher slot, versioned. One row
    per (organization, connection, canonical_field) per version. A
    `canonical_field` starting `__fixed__` never matches a real ApInvoice
    column (same convention as ApInvoiceErpMapping's `__fixed__business_unit`)
    — it exists purely to carry an org-configurable constant (a ledger
    name, a voucher type name) via `default_value`."""
    __tablename__ = "tally_account_mappings"
    __table_args__ = (
        UniqueConstraint("organization_id", "connection_id", "canonical_field", "version", name="uq_tally_account_mapping"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    connection_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # Connection.id, no FK — mirrors ApInvoiceErpMapping.erp_connection_id's own cross-module convention

    canonical_field: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "vendor_name", "__fixed__purchase_ledger"
    tally_field: Mapped[str] = mapped_column(String(100), nullable=False)  # a slot TallyVoucherBuilder understands, e.g. "party_ledger", "purchase_ledger_name"
    is_line_mapping: Mapped[bool] = mapped_column(Boolean, default=False)
    default_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transform_config: Mapped[dict] = mapped_column(JSON, default=dict)  # {"type": "passthrough"|"fixed"|"date_format"|"voucher_type_lookup", ...}

    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TallyAccountMapping {self.canonical_field} -> {self.tally_field} v{self.version}>"


class TallyVoucherPreviewStatus(str, enum.Enum):
    READY = "READY"       # no FAIL-severity check failed — eligible to proceed to TALLY-F/H once those exist
    BLOCKED = "BLOCKED"   # at least one FAIL-severity check failed — must not be written regardless of anything downstream


class TallyVoucherApprovalStatus(str, enum.Enum):
    """TALLY-H — the write-back approval gate's own denormalized outcome,
    synced onto this row by approval_workflow_engine_service's generic
    _sync_entity_status() the same way ApInvoice.status mirrors its own
    Payment Approval workflow outcome (see that model). Kept here, not
    only derivable by joining ApprovalWorkflow, so TALLY-I's write-back
    gate is a single-column check: `preview.approval_status == APPROVED`,
    regardless of whether that came from a real human decision or from
    AUTO_APPROVED (no TALLY_VOUCHER-scoped ApprovalRule configured for
    this org — see tally_approval_service.submit_for_approval()'s own
    docstring on why that is a disclosed, not silent, configuration gap,
    identical in spirit to how Payment Approval already treats an
    unconfigured org)."""
    NOT_SUBMITTED = "NOT_SUBMITTED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class TallyVoucherPreview(TenantBase):
    """The voucher an ApInvoice WOULD become in Tally, and nothing more —
    rebuilt (upserted in place, never duplicated) every time
    tally_voucher_preview_service.build_preview() runs for this
    (connection, invoice) pair, since it's a pure deterministic function
    of current ApInvoice + mapping + Tally canonical data, not an
    independent object with its own lifecycle yet. TALLY-H added
    approval_status/approval_workflow_id below — the write-back approval
    gate's own state, synced from a real ApprovalWorkflow (entity_type=
    TALLY_VOUCHER) rather than a second, forked state machine; TALLY-I
    still owns actually transmitting anything."""
    __tablename__ = "tally_voucher_previews"
    __table_args__ = (
        UniqueConstraint("connection_id", "invoice_id", name="uq_tally_voucher_preview_conn_invoice"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ap_invoices.id"), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)

    mapping_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    voucher_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    voucher_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    party_ledger_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    narration: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    ledger_entries: Mapped[list] = mapped_column(JSON, default=list)  # [{ledger_name, amount, is_deemed_positive}, ...] — the exact structure build_voucher() produced

    # The literal Tally "Import Data" XML this voucher would be transmitted
    # as — serialized for audit/preview only (see module docstring: no
    # connector method exists yet that can actually send it).
    voucher_xml: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Precomputed now so TALLY-I's real post call can use the exact same
    # key without recomputing it — same "ap-invoice-{id}-erp-{connection}"
    # convention as invoice_erp_service's idempotency key, Tally-scoped.
    idempotency_key: Mapped[str | None] = mapped_column(String(150), nullable=True, index=True)

    status: Mapped[TallyVoucherPreviewStatus] = mapped_column(Enum(TallyVoucherPreviewStatus), default=TallyVoucherPreviewStatus.BLOCKED, index=True)

    # TALLY-H — write-back approval gate outcome, see TallyVoucherApprovalStatus.
    approval_status: Mapped[TallyVoucherApprovalStatus] = mapped_column(Enum(TallyVoucherApprovalStatus), default=TallyVoucherApprovalStatus.NOT_SUBMITTED, index=True)
    approval_workflow_id: Mapped[int | None] = mapped_column(ForeignKey("approval_workflows.id"), nullable=True, index=True)

    # TALLY-I — the actual write-back outcome. Denormalized here (not only
    # derivable by joining the latest TallyVoucherWriteBackTransaction) for
    # the exact same reason approval_status is: a single-column check
    # (`preview.write_status == SUCCEEDED`) is what the app-level
    # idempotency short-circuit in tally_writeback_service.py reads before
    # ever building a new request, mirroring ApInvoice.erp_status.
    write_status: Mapped["TallyWriteBackStatus"] = mapped_column(Enum(TallyWriteBackStatus), default=TallyWriteBackStatus.NOT_WRITTEN, index=True)
    tally_voucher_guid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tally_master_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    tally_voucher_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<TallyVoucherPreview invoice={self.invoice_id} conn={self.connection_id} {self.status}>"


class TallyVoucherCheckCategory(str, enum.Enum):
    REQUIRED_FIELDS = "REQUIRED_FIELDS"
    BALANCE = "BALANCE"
    LEDGER_EXISTS = "LEDGER_EXISTS"
    GST = "GST"
    DUPLICATE = "DUPLICATE"


class TallyVoucherCheckResult(str, enum.Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"  # blocks — see TallyVoucherPreviewStatus.BLOCKED


class TallyVoucherValidationResult(TenantBase):
    """One row per deterministic check per preview build — the evidence
    behind a READY/BLOCKED status, not just the status itself. Replaced
    wholesale on every rebuild (same "recompute and replace" convention
    as ApInvoiceLine's replace-on-reprocess), since there's no per-check
    identity worth diffing across rebuilds."""
    __tablename__ = "tally_voucher_validation_results"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    preview_id: Mapped[int] = mapped_column(ForeignKey("tally_voucher_previews.id"), nullable=False, index=True)

    category: Mapped[TallyVoucherCheckCategory] = mapped_column(Enum(TallyVoucherCheckCategory), nullable=False)
    result: Mapped[TallyVoucherCheckResult] = mapped_column(Enum(TallyVoucherCheckResult), nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TallyVoucherValidationResult preview={self.preview_id} {self.category}:{self.result}>"
