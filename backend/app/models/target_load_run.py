"""
TargetLoadRun — execution tracking for real target loads (Phase 6: Target
Loading / BulkWriter).

Scope boundary, same spirit as PipelineRun (Phase 3): this tracks LOAD
runs — taking a saved, validated ColumnMapping (source object + column
mappings/transformations/lookups, see models/column_mapping.py), building
its real SELECT (mapping/mapper.py's build_preview_sql — the exact same
SQL the Mapping page's own Preview button already runs, just without a
row cap here), streaming it against the SOURCE connection in bounded
chunks, and bulk-writing each chunk to the TARGET connection/object via
loading/writers/ (execution/load_worker.py).

Single-stream, not partitioned: like extraction's QUERY-mode path (there
is no key column here either — the mapping's SELECT can span an
arbitrary join/expression, not one key-ordered table), a run has no
natural resume point. A failed or cancelled run's target table is left
exactly as far as it got; the honest recovery is a fresh run (with
write_mode TRUNCATE_INSERT if the destination needs to be reset first),
not silently "resuming" a stream that has no checkpoint to resume from.

write_mode: "APPEND" (plain insert), "TRUNCATE_INSERT" (TRUNCATE TABLE
once, then insert), or "UPSERT" (per-batch insert-or-update on
key_columns — see loading/writers/*.py for the real vendor SQL each one
runs; key_columns is auto-detected from the target's live primary key,
never guessed, and UPSERT is refused with a clear error if the target has
none).
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class LoadStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WriteMode(str, enum.Enum):
    APPEND = "APPEND"
    TRUNCATE_INSERT = "TRUNCATE_INSERT"
    UPSERT = "UPSERT"


class TargetLoadRun(Base):
    __tablename__ = "target_load_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)

    mapping_id: Mapped[int] = mapped_column(ForeignKey("column_mappings.id"), nullable=False, index=True)

    write_mode: Mapped[WriteMode] = mapped_column(Enum(WriteMode), default=WriteMode.APPEND)
    key_columns: Mapped[list | None] = mapped_column(JSON, nullable=True)  # auto-detected target PK, set at plan time for UPSERT

    chunk_size: Mapped[int] = mapped_column(Integer, default=5000)

    status: Mapped[LoadStatus] = mapped_column(Enum(LoadStatus), default=LoadStatus.PENDING, index=True)
    records_read: Mapped[int] = mapped_column(Integer, default=0)     # fetched from the source stream
    records_written: Mapped[int] = mapped_column(Integer, default=0)  # actually written to the target (== records_read unless a batch fails mid-run)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TargetLoadRun {self.id} mapping={self.mapping_id} status={self.status}>"
