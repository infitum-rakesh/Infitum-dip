"""
TargetLoadRun — execution tracking for real target loads (Phase 6: Target
Loading / BulkWriter).

Scope boundary, same spirit as PipelineRun (Phase 3): this tracks LOAD
runs — taking a saved, validated ColumnMapping (source object + column
mappings/transformations/lookups, see models/column_mapping.py) and
bulk-writing it to the TARGET connection/object via loading/writers/
(execution/load_worker.py). Two real read strategies (JJ3 — see that
module's docstring for the full rationale):

  SQL_STREAM (original, kept unchanged) — the mapping's real SELECT
  (mapping/mapper.py's build_preview_sql) streamed against the SOURCE
  connection via one open cursor (open_sql_stream). Needed whenever a
  mapping uses a lookup (JOIN) or a raw SQL transformation expression —
  those can only be evaluated by a real SQL engine, so this path only
  works for SOURCE connectors that support arbitrary SQL. Single-stream,
  not partitioned, and (like extraction's QUERY-mode path) has no natural
  resume point — a failed/cancelled run's target table is left exactly as
  far as it got; the honest recovery is a fresh run, not a faked resume.

  KEY_RANGE (new) — for a straight column-copy mapping (no lookup/
  transformation) on a source that supports key_range/read_key_range_chunk
  (every SQL database connector, AND Salesforce via its Id-keyed SOQL —
  see connectors/application/salesforce.py). Partitioned into
  partition_count contiguous key ranges, run concurrently up to
  worker_count at a time, each partition read-chunk-then-write-chunk then
  advancing its own last_key_read checkpoint (TargetLoadRunPartition,
  mirroring PipelineRunPartition) only after that chunk's write actually
  lands — real resumability at 100M+ row scale, not just a fast path.

write_mode: "APPEND" (plain insert), "TRUNCATE_INSERT" (TRUNCATE TABLE
once, then insert), or "UPSERT" (per-batch insert-or-update on
key_columns — see loading/writers/*.py for the real vendor SQL each one
runs; key_columns is auto-detected from the target's live primary key,
never guessed, and UPSERT is refused with a clear error if the target has
none).

create_table: real CREATE TABLE from the source's own column metadata
(loading/writers/base.py's ensure_table_from_columns()) before the first
batch, if the target object doesn't already exist — sized VARCHAR2/
VARCHAR, not blanket CLOB/TEXT (JJ1). recreate_table additionally drops
an existing target table first (source structure has genuinely changed).
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase
from app.models.pipeline_run import PartitionStatus  # reused as-is — same PENDING/RUNNING/COMPLETE/FAILED/CANCELLED shape


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


class TargetLoadRun(TenantBase):
    __tablename__ = "target_load_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    mapping_id: Mapped[int] = mapped_column(ForeignKey("column_mappings.id"), nullable=False, index=True)
    # CM4 — pinned at run-start time to the mapping's current_published_version
    # (falling back to latest_version if nothing is published yet — Run Load
    # pre-dates the publish concept and shouldn't be blocked by it, unlike
    # the stricter Pipeline Builder path). NULL on runs created before this
    # column existed; load_executor.py falls back to the mapping's
    # then-current effective version for those, matching the exact
    # behavior this run actually got when it started.
    mapping_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    write_mode: Mapped[WriteMode] = mapped_column(Enum(WriteMode), default=WriteMode.APPEND)
    key_columns: Mapped[list | None] = mapped_column(JSON, nullable=True)  # auto-detected target PK, set at plan time for UPSERT

    chunk_size: Mapped[int] = mapped_column(Integer, default=5000)

    # NULL on rows created before JJ3 — load_executor infers SQL_STREAM for
    # those (identical behavior to before this column existed). New runs
    # always set this explicitly. One of "SQL_STREAM" | "KEY_RANGE".
    source_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    key_column: Mapped[str | None] = mapped_column(String(200), nullable=True)      # KEY_RANGE only
    key_type: Mapped[str | None] = mapped_column(String(20), nullable=True)         # KEY_RANGE only — see execution/checkpoint.py
    worker_count: Mapped[int] = mapped_column(Integer, default=4)                   # KEY_RANGE only
    partition_count: Mapped[int] = mapped_column(Integer, default=4)                # KEY_RANGE only

    create_table: Mapped[bool] = mapped_column(Boolean, default=False)
    recreate_table: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[LoadStatus] = mapped_column(Enum(LoadStatus), default=LoadStatus.PENDING, index=True)
    records_read: Mapped[int] = mapped_column(Integer, default=0)     # fetched from the source stream
    records_written: Mapped[int] = mapped_column(Integer, default=0)  # actually written to the target (== records_read unless a batch fails mid-run)
    # Individually failed rows (KK2/KK3) — real per-row failures caught by
    # loading/writers/base.py's write_batch_with_fallback() bulk-then-row-
    # by-row fallback. records_read == records_written + records_rejected
    # once a run finishes cleanly (never silently dropped, never double-
    # counted — see execution/load_worker.py's _advance_partition/
    # _advance_written). Each one's row data + real error is a
    # RejectedRecord (models/rejected_record.py), viewable via
    # GET /api/loads/{id}/rejected-records.
    records_rejected: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<TargetLoadRun {self.id} mapping={self.mapping_id} status={self.status}>"


class TargetLoadRunPartition(TenantBase):
    """KEY_RANGE mode only — one row per contiguous key range, exactly
    mirroring PipelineRunPartition's shape (see models/pipeline_run.py).
    last_key_read is the real checkpoint: on Resume, a partition that
    isn't COMPLETE picks back up strictly after this value instead of
    restarting its whole range — the piece that makes a 100M+ row load
    survive a restart instead of starting over from row 0."""
    __tablename__ = "target_load_run_partitions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("target_load_runs.id"), nullable=False, index=True)
    partition_index: Mapped[int] = mapped_column(Integer, nullable=False)

    range_start: Mapped[str | None] = mapped_column(String(200), nullable=True)    # exclusive lower bound; None = unbounded low
    range_end: Mapped[str | None] = mapped_column(String(200), nullable=True)      # inclusive upper bound; None = unbounded high
    last_key_read: Mapped[str | None] = mapped_column(String(200), nullable=True)  # THE checkpoint — resume reads strictly greater than this

    status: Mapped[PartitionStatus] = mapped_column(Enum(PartitionStatus), default=PartitionStatus.PENDING, index=True)
    records_read: Mapped[int] = mapped_column(Integer, default=0)
    records_written: Mapped[int] = mapped_column(Integer, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, default=0)  # KK3 — see TargetLoadRun.records_rejected
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<TargetLoadRunPartition run={self.run_id} #{self.partition_index} status={self.status}>"
