"""
UploadedDatasetFile — a file uploaded directly as a data source/target for
Validation or Reconciliation, as an alternative to picking a real DB
connection+object. Deliberately NOT pipeline-scoped (unlike
PipelineSourceFile) — a Validation rule set or Reconciliation job isn't a
pipeline node, so this is its own generic table, reusable by both.

Each row gets its OWN storage directory (storage_dir), and the connector
layer (services/uploaded_file_service.py) reads it back via a real
LocalFileConnector pointed at that directory with object_name=filename —
the exact same sandboxed, streaming file-reading code SFTP/Local pipeline
sources already use (connectors/files/), not a bespoke reader. One file
per directory (rather than sharing a directory across uploads) keeps the
LocalFileConnector's sandbox trivially correct — no risk of one upload's
object_name accidentally resolving into a sibling upload's file.
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class UploadedDatasetFile(TenantBase):
    __tablename__ = "uploaded_dataset_files"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    uploaded_by_id: Mapped[int | None] = mapped_column(nullable=True)

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    extension: Mapped[str] = mapped_column(String(20), nullable=False)  # real, from the filename — e.g. "csv", "xlsx"
    mime_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # This upload's OWN directory — LocalFileConnector's base_path.
    # object_name passed to the connector is always `filename` above.
    storage_dir: Mapped[str] = mapped_column(String(1000), nullable=False)

    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<UploadedDatasetFile {self.filename} id={self.id}>"
