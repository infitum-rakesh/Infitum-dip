"""
TallyExtractionRun/TallyExtractionRecord — TALLY-B read-only extraction
snapshot storage.

A run captures ONE extraction call against a TallyPrime connection (one
extraction_type, one date range, one company) and its real child records
verbatim, as TallyPrimeConnector returned them — this is intentionally a
raw snapshot layer, not yet the canonical accounting model. TALLY-C builds
TallyCanonicalVoucher/Party/Ledger FROM these raw records, preserving raw
alongside normalized per the spec's own requirement; this phase's job is
only to get real data out of Tally durably and inspectably, with
incremental/checkpointed re-extraction.

No cross-run dedup/uniqueness constraint on TallyExtractionRecord: each
run's rows are an immutable snapshot of what Tally returned for that call.
Matching/versioning/deduplication across runs is deliberately deferred to
the canonical-model layer (TALLY-C+), which is where Tally's own natural
keys (GUID/ALTERID) get used as the real identity — inventing a second,
competing dedup mechanism here would just be the parallel-implementation
mistake this integration's own design review flagged and ruled out
elsewhere (see reconciliation engine reuse rationale).
"""
import enum
from datetime import date, datetime

from sqlalchemy import DateTime, Date, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class TallyExtractionType(str, enum.Enum):
    VOUCHERS = "VOUCHERS"
    PURCHASE_VOUCHERS = "PURCHASE_VOUCHERS"
    SALES_VOUCHERS = "SALES_VOUCHERS"
    PARTY_MASTER = "PARTY_MASTER"
    LEDGER = "LEDGER"
    PAYMENTS = "PAYMENTS"
    OUTSTANDING = "OUTSTANDING"
    BILL_ALLOCATIONS = "BILL_ALLOCATIONS"


class TallyExtractionMode(str, enum.Enum):
    FULL = "FULL"
    INCREMENTAL = "INCREMENTAL"


class TallyExtractionStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TallyExtractionRun(TenantBase):
    __tablename__ = "tally_extraction_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)

    # TALLY-J — same additive schedule_id/trigger convention as
    # TallyReconciliationRun (see that model's own docstring on this pair):
    # set only for the incremental PURCHASE_VOUCHERS/PAYMENTS extractions
    # tally_scheduled_reconciliation_service.py runs immediately ahead of a
    # scheduled reconciliation. Every manually-triggered extraction
    # (POST /connections/{id}/extractions) leaves both null/"Manual".
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedules.id"), nullable=True, index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="Manual")

    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    extraction_type: Mapped[TallyExtractionType] = mapped_column(Enum(TallyExtractionType), nullable=False, index=True)
    # Only meaningful for VOUCHERS/PAYMENTS-shaped extractions — a specific
    # Tally voucher type name (e.g. "Purchase", a renamed custom type) to
    # filter to, on top of extraction_type's own default filter.
    voucher_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    party_filter: Mapped[str | None] = mapped_column(String(200), nullable=True)

    from_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    to_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    mode: Mapped[TallyExtractionMode] = mapped_column(Enum(TallyExtractionMode), default=TallyExtractionMode.FULL)

    status: Mapped[TallyExtractionStatus] = mapped_column(Enum(TallyExtractionStatus), default=TallyExtractionStatus.PENDING, index=True)
    # High-water mark this run advanced to on success — the next
    # INCREMENTAL run for this (connection, company, extraction_type,
    # voucher_type) combination starts from here. Only advanced past
    # to_date once every date-range chunk for this run has succeeded.
    checkpoint_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    records_fetched: Mapped[int] = mapped_column(Integer, default=0)
    chunks_total: Mapped[int] = mapped_column(Integer, default=0)
    chunks_failed: Mapped[int] = mapped_column(Integer, default=0)

    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TallyExtractionRun {self.id} {self.extraction_type} {self.status}>"


class TallyExtractionRecord(TenantBase):
    __tablename__ = "tally_extraction_records"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("tally_extraction_runs.id"), nullable=False, index=True)

    extraction_type: Mapped[TallyExtractionType] = mapped_column(Enum(TallyExtractionType), nullable=False)
    # Best-effort human identifier for this record (voucher number, party
    # name, bill name, ...) — for display/search only, never a uniqueness
    # key (Tally's real identity for a voucher/ledger is GUID/ALTERID,
    # inside raw_data, per the connector's own normalized shape).
    external_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # The connector's own normalized dict (see tallyprime.py's
    # _normalize_voucher/get_party_master/etc.) — already reshaped out of
    # Tally's raw XML/xmltodict form, but still pre-canonical: field names
    # here are this integration's own extraction-layer shape, not yet
    # TALLY-C's canonical accounting model.
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TallyExtractionRecord run={self.run_id} {self.extraction_type} {self.external_ref}>"
