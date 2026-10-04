"""
TallyCanonicalVoucher/Party/Ledger — TALLY-C canonical accounting model.

TallyExtractionRecord (TALLY-B) is a raw, pre-canonical snapshot of
whatever one connector call returned — it accumulates duplicate rows
across every run and carries the connector's own dict shape verbatim
(see tally_extraction_service.py's docstring, and tally_extraction.py's
module docstring). These three tables are the real canonical accounting
objects the rest of the integration (TALLY-D's accounting mapping layer,
TALLY-E's reconciliation engine) reasons about: one row per real Tally
object, kept current by GUID/ALTERID-keyed upsert
(services/tally_canonical_service.py), never duplicated across
extraction runs — raw and normalized values are both kept, side by side,
never collapsing one into the other.

Why GUID/ALTERID and not a (connection_id, voucher_number)-style natural
key: Tally's own GUID is the one truly stable, globally-unique identity
for a ledger/voucher master object within a company; ALTERID increments
on every edit inside Tally, so comparing it is exactly Tally's own "has
this object changed since I last saw it" signal — cheaper and more
reliable than diffing every field on every sync.

Not built here (deferred, disclosed limitation): BILL_ALLOCATIONS/
OUTSTANDING rows have no GUID in Tally's own TDL export (a bill
allocation is a facet of a ledger, not a first-class Tally object with
its own identity) — so there is no stable identity to upsert against.
TALLY-E's reconciliation engine reads those directly from
TallyExtractionRecord.raw_data instead of a canonical table.
"""
import enum
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class TallyVoucherDirection(str, enum.Enum):
    """Only meaningful for vouchers sourced from PAYMENTS extraction
    (get_payments() tags each voucher PAYMENT or RECEIPT) — None for
    every voucher canonicalized from any other extraction type."""
    PAYMENT = "PAYMENT"
    RECEIPT = "RECEIPT"


class TallyCanonicalVoucher(TenantBase):
    __tablename__ = "tally_canonical_vouchers"
    __table_args__ = (
        UniqueConstraint("connection_id", "guid", name="uq_tally_canonical_voucher_conn_guid"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    # Last TallyExtractionRecord this row was (re)derived from — for
    # traceability only; the row's own identity is (connection_id, guid),
    # not this FK, so re-materializing from a later run's record just
    # updates this pointer rather than creating a new voucher row.
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("tally_extraction_records.id"), nullable=True)

    guid: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    alter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    voucher_number: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    voucher_type: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    voucher_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    party_ledger_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    narration: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    direction: Mapped[TallyVoucherDirection | None] = mapped_column(Enum(TallyVoucherDirection), nullable=True)

    # Normalized total — sum of |amount| across ledger_entries where
    # is_deemed_positive == "Yes" (Tally's own debit/credit-side flag).
    # None if the raw entries don't parse cleanly — never guessed.
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Raw [{ledger_name, amount, is_deemed_positive}, ...] kept verbatim
    # alongside the normalized total above — full fidelity, never
    # collapsed into just the derived number.
    ledger_entries: Mapped[list] = mapped_column(JSON, default=list)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<TallyCanonicalVoucher {self.voucher_number or self.guid}>"


class TallyCanonicalParty(TenantBase):
    __tablename__ = "tally_canonical_parties"
    __table_args__ = (
        UniqueConstraint("connection_id", "guid", name="uq_tally_canonical_party_conn_guid"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("tally_extraction_records.id"), nullable=True)

    guid: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    alter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Same normalization convention as ApInvoiceVendor.normalized_name —
    # lets TALLY-D/E fuzzy-match this party against ApInvoice.vendor_name.
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    parent: Mapped[str | None] = mapped_column(String(120), nullable=True)  # Tally ledger group, e.g. "Sundry Creditors"
    party_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)  # SUPPLIER | CUSTOMER | None

    gstin: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    address_lines: Mapped[list] = mapped_column(JSON, default=list)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(30), nullable=True)

    opening_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    closing_balance: Mapped[float | None] = mapped_column(Float, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<TallyCanonicalParty {self.name}>"


class TallyCanonicalLedger(TenantBase):
    """Any Tally ledger master, not just Sundry Debtors/Creditors parties
    — populated from LEDGER extraction's {"kind": "ledger", ...} records.
    Distinct from TallyCanonicalParty: every party is also a ledger in
    Tally, but this table also covers non-party ledgers (bank, expense,
    tax accounts, ...) that a LEDGER extraction can target by name."""
    __tablename__ = "tally_canonical_ledgers"
    __table_args__ = (
        UniqueConstraint("connection_id", "guid", name="uq_tally_canonical_ledger_conn_guid"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("tally_extraction_records.id"), nullable=True)

    guid: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    alter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    parent: Mapped[str | None] = mapped_column(String(120), nullable=True)
    opening_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    closing_balance: Mapped[float | None] = mapped_column(Float, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<TallyCanonicalLedger {self.name}>"
