"""
ColumnMappingUsage — CM1: real usage-tracking rows for "this mapping is
used by N pipeline(s)" (spec section 37) and the Upgrade Mapping flow
(spec section 38).

One row per (pipeline_id, node_id) *attach* event -- written by
api/pipeline_mapping.py's /attach route the moment a column_mapping_step
node on a pipeline canvas is pointed at a specific (mapping_id,
mapping_version), and again by /upgrade whenever that pin moves to a
newer version. Superseded rows are never deleted or overwritten -- this
is deliberately an append-only history, not a single current-pointer
row, so "what did this pipeline use over time" stays answerable and
editing/upgrading a mapping can never quietly erase what a pipeline used
to reference.

"Currently attached" (what a running pipeline actually uses right now)
is always read from the *pipeline's own* PipelineVersion.pipeline_definition
node config (`{"mappingId": ..., "mappingVersion": ...}`) -- never from
this table, which exists purely as a history/lookup index. See
api/pipeline_mapping.py and services/mapping_service.py::list_usage().
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ColumnMappingUsage(TenantBase):
    __tablename__ = "column_mapping_usage"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    mapping_id: Mapped[int] = mapped_column(ForeignKey("column_mappings.id"), nullable=False, index=True)
    mapping_version: Mapped[int] = mapped_column(Integer, nullable=False)

    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), nullable=False, index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(String(100), nullable=False)

    environment: Mapped[str] = mapped_column(String(20), default="production")

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ColumnMappingUsage mapping={self.mapping_id}v{self.mapping_version} pipeline={self.pipeline_id}v{self.pipeline_version} node={self.node_id}>"
