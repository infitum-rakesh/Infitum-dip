"""
SellerAuditLog — the Seller's own audit trail of actions taken ON
customers (SELLER PORTAL spec section 7). Distinct from models/audit_log.py's
AuditLog, which is each *organization's own* internal audit trail of its
*own* users' actions (Document Upload, OCR, Validation, ...) — a different
consumer entirely. This table only ever gets written to by Seller Portal
services (customer_service.py now; subscription_service/module_service/
license_service/seller_user_service in later phases).

Full browse/filter UI is Phase 8 (Seller Audit Logs) — this model and
record_seller_event() are built now because Phase 2's spec explicitly
requires impersonation to be "heavily audited... never silent", which
needs a real persisted record from day one, not a stub.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase

# The exact event-type list from the spec (section 7). Kept as a plain
# string column (like AuditLog.action) rather than an enum so later phases
# can add new action strings without a migration.
SELLER_AUDIT_ACTIONS = [
    "Customer Created", "Customer Updated", "Customer Activated", "Customer Suspended",
    "Customer Admin Welcome Email",
    "Subscription Created", "Subscription Changed",
    "Module Purchased", "Module Enabled", "Module Disabled",
    "License Created", "License Activated", "License Suspended", "License Extended",
    "License Renewed", "License Changed", "License Revoked",
    "Quota Changed", "Impersonation",
    "Seller User Created", "Seller User Updated", "Seller User Disabled", "Seller User Enabled",
    "Seller Role Changed", "Seller User Password Reset", "Seller User Deleted",
    "Seller Settings Updated",
    "Tenant Database Configured", "Tenant Database Updated", "Tenant Database Tested",
    "Platform Database Migration Completed",
]


class SellerAuditLog(PlatformBase):
    __tablename__ = "seller_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    seller_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)

    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)  # one of SELLER_AUDIT_ACTIONS
    module: Mapped[str | None] = mapped_column(String(60), nullable=True)  # platform module this event concerns, if any
    resource: Mapped[str | None] = mapped_column(String(300), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:
        return f"<SellerAuditLog {self.action} customer={self.customer_id}>"


async def record_seller_event(
    db: AsyncSession, *, seller_user_id: int | None, customer_id: int | None, action: str,
    module: str | None = None, resource: str | None = None, resource_id: str | None = None,
    ip_address: str | None = None, details: str | None = None,
    before_value: dict | None = None, after_value: dict | None = None, commit: bool = True,
) -> SellerAuditLog:
    entry = SellerAuditLog(
        seller_user_id=seller_user_id, customer_id=customer_id, action=action, module=module,
        resource=resource, resource_id=resource_id, ip_address=ip_address, details=details,
        before_value=before_value, after_value=after_value,
    )
    db.add(entry)
    if commit:
        await db.commit()
        await db.refresh(entry)
    return entry
