"""
CustomerModule — Seller Portal -> Purchased Modules (Phase 4). The
granular per-customer, per-module license record: SELLER PORTAL spec
section 3's "MODULE LICENSE CONFIGURATION" (Enabled / License Type /
Maximum Users / Monthly Credits / Maximum Documents / Expiry) lives here.

Design note (see Seller Portal memory): this table doubles as the real
row Phase 5 (License Management) will operate on — the spec's own
"License Table" columns (License ID, Customer, Module, License Type,
Start, Expiry, Users, Usage Limit, Status, Actions) are the same shape as
this model, so Phase 5 adds lifecycle *actions* on these rows rather than
a second, redundant "License" table.

CustomerModule.enabled is the granular per-module toggle; it is kept in
sync with (a subset of) Organization.licensed_modules — see
services/seller/module_service.py's _sync_org_licensed_modules(). That
sync is what makes this real: Organization.licensed_modules is what
core/deps.require_action() and the frontend's evaluateAccess() actually
gate on, so a CustomerModule row that isn't reflected there would be
cosmetic, not real licensing.

Phase 5 (License Management) additions: `status` (ACTIVE/SUSPENDED/
EXPIRED/REVOKED — a real lifecycle distinct from the plain `enabled`
bool Phase 4 already uses; REVOKED is terminal, SUSPENDED can be
resumed via Activate) plus `max_records`/`storage_limit_gb` to round out
the spec's "CREATE LICENSE" field list (Record Limit, Storage Limit —
Maximum Documents/Monthly Credits already covered Document Limit/AI
Credit Limit from Phase 4).
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class CustomerModule(Base):
    __tablename__ = "customer_modules"
    __table_args__ = (UniqueConstraint("organization_id", "module", name="uq_customer_module"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(60), nullable=False, index=True)  # one of module_service.MODULE_CATALOG keys

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # ACTIVE|SUSPENDED|EXPIRED|REVOKED — EXPIRED is also computed on read from
    # expiry_date (see license_service._effective_status) so an unattended
    # license reads as expired even before anyone explicitly flips this.
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    # TRIAL|SUBSCRIPTION|PER_USER|PER_DOCUMENT|PER_RECORD|USAGE_BASED|UNLIMITED|CUSTOM
    # (same license-type vocabulary Phase 5 License Management uses)
    license_type: Mapped[str] = mapped_column(String(30), default="SUBSCRIPTION")

    max_users: Mapped[int | None] = mapped_column(Integer, nullable=True)         # None = unlimited
    monthly_credits: Mapped[int | None] = mapped_column(Integer, nullable=True)   # None = unlimited / not applicable — "AI Credit Limit"
    max_documents: Mapped[int | None] = mapped_column(Integer, nullable=True)     # None = unlimited / not applicable — "Document Limit"
    max_records: Mapped[int | None] = mapped_column(Integer, nullable=True)       # None = unlimited / not applicable — "Record Limit"
    storage_limit_gb: Mapped[float | None] = mapped_column(Float, nullable=True)  # None = unlimited / not applicable — "Storage Limit"

    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<CustomerModule org={self.organization_id} {self.module} enabled={self.enabled}>"
