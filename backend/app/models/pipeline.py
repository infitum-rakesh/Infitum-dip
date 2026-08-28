"""
Pipeline models — backs the Transform Workspace / Pipeline Builder canvas.

Two tables, matching the versioning model the Transform Workspace needs:

  pipeline          — one row per pipeline. Tracks only *current* pointer
                       metadata (name, current_version) plus who/when.
  pipeline_versions — one row per saved snapshot of {nodes, edges, settings}.
                       Every Save creates a NEW version row rather than
                       overwriting anything (see services/pipeline_service.py
                       for the "keep last 5" retention + restore-creates-a-
                       new-version rules). This is what "Auto-save Safety"
                       (never silently overwrite) and "Version History" are
                       built on.

Additive module: no FK from here into schedules/connections/etc. Schedules
already reference pipelines by id/name only (see models/schedule.py), so
this doesn't need to change to interoperate with it.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class Pipeline(TenantBase):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=1)

    created_by: Mapped[int | None] = mapped_column(nullable=True, index=True)
    updated_by: Mapped[int | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Pipeline {self.name} v{self.current_version}>"


class PipelineVersion(TenantBase):
    """
    One immutable snapshot of a pipeline's full node/edge definition.

    pipeline_definition shape: {"nodes": [...], "edges": [...], "settings": {}}
    — the exact node/edge objects the canvas already works with, so save/
    load never needs a translation layer.
    """
    __tablename__ = "pipeline_versions"
    __table_args__ = (UniqueConstraint("pipeline_id", "version_number", name="uq_pipeline_version"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    pipeline_definition: Mapped[dict] = mapped_column(JSON, nullable=False)

    created_by: Mapped[int | None] = mapped_column(nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # e.g. "Restored from v7" / free-text note — surfaced in Version History
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<PipelineVersion pipeline={self.pipeline_id} v{self.version_number}>"
