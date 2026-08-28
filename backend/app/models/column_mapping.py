"""
ColumnMapping — Object & Column Mapping (Phase 4 of the data-integration
build).

One row per saved mapping definition: a source object (one connection +
schema + object, or a custom SQL query — same shape Source Dataset nodes
already use) mapped to a target object, plus the column-level mapping
rows themselves as a JSON blob (mirrors how PipelineVersion stores
{nodes, edges} — a flexible, per-row-shaped structure like this doesn't
benefit from being fully relational, and every row already gets validated
at the Pydantic layer, not the DB layer).

Live schema is never cached here — source_columns/target_columns are
always re-fetched from the real connection via connectors/database/base.py
at read/validate/auto-map time (see mapping_service.py), so a mapping
never goes stale relative to what the source/target actually look like.

One mapping row (an element of `mappings`) has this shape:
{
  "id": "map_xxx",
  "source_columns": ["FIRST_NAME", "LAST_NAME"],  # usually one; >1 only for a
                                                    # multi-column expression (concat, etc.)
  "target_column": "CUSTOMER_FULL_NAME",
  "transformation": "TRIM(FIRST_NAME || ' ' || LAST_NAME)",  # SQL expression; empty = passthrough
  "constant_value": None,     # literal value instead of any source column
  "default_value": None,      # COALESCE(...) fallback when the source value is NULL
  "lookup": None,              # {"table": "...", "key_column": "...", "return_column": "..."} — same-connection JOIN
  "status": "MAPPED",          # MAPPED | SUGGESTED | UNMAPPED | CONFLICT
  "confidence": 100,           # 0-100, meaningful for SUGGESTED rows from the auto-mapper
  "match_method": "exact",     # exact | case_insensitive | normalized | alias | similarity | manual
}
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ColumnMapping(TenantBase):
    __tablename__ = "column_mappings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    source_connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False)
    source_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_object: Mapped[str | None] = mapped_column(String(200), nullable=True)

    target_connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False)
    target_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_object: Mapped[str | None] = mapped_column(String(200), nullable=True)

    mappings: Mapped[list] = mapped_column(JSON, default=list)  # list[dict] — see module docstring for row shape

    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT | VALIDATED | WARNING | CONFLICT — last validate() outcome

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<ColumnMapping {self.name} ({self.source_object} -> {self.target_object})>"
