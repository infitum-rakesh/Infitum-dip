"""
DocumentMappingDefault — the organisation's default mapping profile for a
given Document Intelligence document_type (Document Intelligence: Dynamic
JSON Extraction spec, section 7.3 "Default profile administration").

One row per (organization_id, document_type) — the UNIQUE constraint is
the actual "prevent ambiguous default configurations" guarantee section
7.3 asks for, not just an application-level check (same discipline as
DocumentSourceRegistry's own identity constraint). Setting a new default
for a type that already has one is an UPDATE of the existing row, not a
second row — see document_mapping_resolution_service.set_default().

This is consulted ONLY when a schedule has no explicit
DocumentScheduleConfig.mapping_id for that run — see section 7.2's
precedence (explicit schedule mapping -> compatible document-type
default -> no mapping) and
document_mapping_resolution_service.resolve_scheduled_mapping().
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class DocumentMappingDefault(TenantBase):
    __tablename__ = "document_mapping_defaults"
    __table_args__ = (
        UniqueConstraint("organization_id", "document_type", name="uq_document_mapping_default_org_doctype"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    document_type: Mapped[str] = mapped_column(String(60), nullable=False)

    # No FK — same loose-coupling convention as ColumnMapping's own
    # source/target connection fields; a deleted/retired mapping is a
    # clear "default is no longer usable" runtime condition surfaced at
    # resolution time, never a migration-blocking FK.
    mapping_id: Mapped[int] = mapped_column(Integer, nullable=False)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<DocumentMappingDefault org={self.organization_id} type={self.document_type} mapping={self.mapping_id}>"
