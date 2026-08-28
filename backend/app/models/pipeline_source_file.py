"""
PipelineSourceFile — a file uploaded directly into a Source Dataset node's
properties panel (Transform Workspace), as an alternative to browsing an
existing SFTP connection. See services/pipeline_file_service.py.

Scoped by node_id (the pipeline canvas node's client-generated id, stable
across saves) rather than pipeline_id alone, because a file is uploaded
while configuring one specific node — a pipeline could in principle have
more than one File-typed source node, each with its own upload.
pipeline_id starts NULL for a brand-new, not-yet-saved pipeline and is
backfilled via attach_to_pipeline() the moment the pipeline is first
saved (see api/pipeline_files.py's /attach route) — this is what makes
the file "uniquely linked with its respective pipeline" once Save happens,
even though the upload itself has to be possible before a pipeline_id
exists.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class PipelineSourceFile(TenantBase):
    __tablename__ = "pipeline_source_files"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    pipeline_id: Mapped[int | None] = mapped_column(ForeignKey("pipelines.id"), nullable=True, index=True)
    node_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    uploaded_by_id: Mapped[int | None] = mapped_column(nullable=True)

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    extension: Mapped[str] = mapped_column(String(20), nullable=False)  # real, from the filename — e.g. "csv", "xlsx"
    mime_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)

    # Delimited text files only — real, sniffed from the file's own content
    # (csv.Sniffer), never assumed/hardcoded.
    delimiter: Mapped[str | None] = mapped_column(String(5), nullable=True)

    # Excel files only — real sheet names read via openpyxl.
    sheet_names: Mapped[list | None] = mapped_column(JSON, nullable=True)
    selected_sheet: Mapped[str | None] = mapped_column(String(200), nullable=True)

    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<PipelineSourceFile {self.filename} node={self.node_id}>"
