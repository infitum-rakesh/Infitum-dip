"""
ColumnMapping — Object & Column Mapping (Phase 4 of the data-integration
build; re-scoped to a lightweight header row by the CM1 versioning build,
see models/column_mapping_version.py).

One row per saved mapping *definition family*: a source object + target
object plus two pointers into ColumnMappingVersion (latest_version,
current_published_version) — mirrors models/pipeline.py's
Pipeline/PipelineVersion split exactly. The real column-level mapping
rows (transformation/lookup/constant/default/is_key, see
column_mapping_version.py's docstring for the row shape) now live on the
immutable ColumnMappingVersion snapshot rows, never here — this header
row is never itself a "current mapping definition."

`mappings`/`status` below are kept as real columns for backward read
compatibility only (old rows created before CM1's migration/backfill
script populated ColumnMappingVersion for every existing mapping) — new
code must never write to them; use services/mapping_service.py's
create_new_version()/publish_version()/get_effective_mappings() instead.
ColumnMappingOut's `mappings` field is served via get_effective_mappings()
so existing frontend consumers (ObjectColumnMapping.jsx) keep working
unmodified against the current published (or latest) version.

Live schema is never cached here — source_columns/target_columns are
always re-fetched from the real connection via connectors/database/base.py
at read/validate/auto-map time (see mapping_service.py), so a mapping
never goes stale relative to what the source/target actually look like.

Document Mapping Profiles (Document Intelligence: Dynamic JSON
Extraction spec — "do not create a third versioning system") reuse this
SAME header+version split rather than a bespoke model: source_connection_id
is nullable and, when NULL, source_document_type names a Document
Intelligence document_type (e.g. "INVOICE") instead — the live "source
columns" are then the canonical/ad-hoc taxonomy's field names
(document_taxonomies.build_seed_schema(), widened by
document_service.discover_known_field_names()), never a real connector.
Exactly one of source_connection_id/source_document_type is ever set.
Similarly target_connection_id is nullable and target_format distinguishes
what kind of target this is: "DB_TABLE" (NULL also means this, for every
pre-existing row — the original, unchanged, connection-based behavior) or
"INVOICE_SCHEMA" (ApInvoice's own real typed columns, introspected
directly — no Connection row at all, target_connection_id is then NULL
too). See services/mapping_service.py's fetch_columns() for the one place
this branches, and services/document_mapping_service.py for applying a
PUBLISHED document-sourced version to one Document's extraction result.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ColumnMapping(TenantBase):
    __tablename__ = "column_mappings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    source_connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id"), nullable=True)
    source_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_object: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_document_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    target_connection_id: Mapped[int | None] = mapped_column(ForeignKey("connections.id"), nullable=True)
    target_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_object: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_format: Mapped[str | None] = mapped_column(String(30), nullable=True)  # NULL/"DB_TABLE" | "INVOICE_SCHEMA"

    # CM1: real version pointers, mirroring Pipeline.current_version.
    # latest_version always points at the newest version row (any status);
    # current_published_version points at the newest PUBLISHED version
    # specifically (None until something has been explicitly Published) —
    # this is the version a production pipeline node is allowed to attach.
    latest_version: Mapped[int] = mapped_column(Integer, default=1)
    current_published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Legacy columns — read-only post-CM1, see module docstring. Column
    # kept (rather than dropped) so pre-CM1 SQLite rows remain loadable
    # without an in-place data rewrite beyond the one-time backfill script.
    mappings: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<ColumnMapping {self.name} ({self.source_object} -> {self.target_object}) latest=v{self.latest_version}>"
