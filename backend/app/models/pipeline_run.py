"""
PipelineRun / PipelineRunPartition — execution tracking for the real
chunked/streaming extraction engine (Phase 3 of the data-integration
build; see backend/app/execution/pipeline_executor.py).

Scope boundary: this tracks EXTRACTION runs — reading one Source Dataset
node's data from a live connection, in bounded-memory chunks, out to a
local staging file (see loading/staging.py). It does NOT yet track full
source-to-target pipeline runs — transform/validate/target-load execution
isn't real yet (later phases; BulkWriter/target loading is Phase 6).

PipelineRunPartition IS the checkpoint store: last_key_read + status per
partition is exactly what execution/checkpoint.py and pipeline_executor.py
read to resume a run without re-reading completed partitions or
restarting a failed one from its range start. For TABLE/VIEW sources with
a chosen key column, a run has N partitions (contiguous key ranges) that
extract concurrently. For custom-SQL (QUERY) sources there is no key
column to partition on, so a run always has exactly one partition
(range_start/range_end = None) streamed by a single worker — see
worker.run_query_partition(); this path has no natural resume point and
restarts from the beginning if interrupted, which is disclosed rather
than faked as resumable.

FILE sources (Phase 5) are a third partitioning strategy: `source_kind`
disambiguates which of the three a run is (older rows predating this
column are NULL and inferred by pipeline_executor from custom_sql/
object_name for backward compatibility). A FILE run's `object_name` holds
the file-matching pattern (glob, or "re:<regex>" — same convention as
connectors/files list_files()); planning discovers every matching file
via the connector, computes its checksum up front, skips any file whose
checksum already appears on a COMPLETE partition of an earlier run of the
same pipeline node (real, if partial, duplicate-file detection — not
content-diffing within a file), and creates one partition per remaining
file. Like QUERY mode, there is no natural mid-file resume point, so a
retried file-partition restarts that one file from row 0 — disclosed, not
faked. `archive_dir`/`error_dir` on PipelineRun, if configured, are where
worker.run_file_partition() moves a file via connector.move_file() after
it completes or fails; `archived` on the partition records whether that
move actually happened (never assumed).
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class RunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PartitionStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PipelineRun(TenantBase):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), nullable=False, index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(String(100), nullable=False)  # which Source Dataset node in the saved pipeline definition

    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    object_name: Mapped[str | None] = mapped_column(String(200), nullable=True)  # TABLE/VIEW: object name. FILE: match pattern.
    custom_sql: Mapped[str | None] = mapped_column(Text, nullable=True)          # set => QUERY single-stream path
    sql_parameters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    key_column: Mapped[str | None] = mapped_column(String(200), nullable=True)
    key_type: Mapped[str | None] = mapped_column(String(20), nullable=True)  # int | float | datetime | str — see execution/checkpoint.py

    # NULL on rows created before Phase 5 — pipeline_executor infers TABLE
    # vs QUERY from custom_sql/object_name for those; new rows always set
    # this explicitly. One of "TABLE" | "QUERY" | "FILE".
    source_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    archive_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)  # FILE mode only — moved here via connector.move_file() on success
    error_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)    # FILE mode only — moved here on failure

    chunk_size: Mapped[int] = mapped_column(Integer, default=20000)
    worker_count: Mapped[int] = mapped_column(Integer, default=4)
    partition_count: Mapped[int] = mapped_column(Integer, default=4)

    # name= required — see schedule.py's ScheduleRun.status for why (two
    # independent RunStatus classes in this codebase would otherwise
    # collide on one native "runstatus" DB enum type on Postgres/MySQL).
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, name="pipeline_run_status"), default=RunStatus.PENDING, index=True)
    records_read: Mapped[int] = mapped_column(Integer, default=0)
    records_written: Mapped[int] = mapped_column(Integer, default=0)  # == records_read for the staging writer (1:1, no transform yet)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    staging_location: Mapped[str | None] = mapped_column(String(500), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<PipelineRun {self.id} pipeline={self.pipeline_id} status={self.status}>"


class PipelineRunPartition(TenantBase):
    __tablename__ = "pipeline_run_partitions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False, index=True)
    partition_index: Mapped[int] = mapped_column(Integer, nullable=False)

    range_start: Mapped[str | None] = mapped_column(String(200), nullable=True)   # exclusive lower bound; None = unbounded low
    range_end: Mapped[str | None] = mapped_column(String(200), nullable=True)     # inclusive upper bound; None = unbounded high
    last_key_read: Mapped[str | None] = mapped_column(String(200), nullable=True)  # THE checkpoint — resume reads strictly greater than this

    # FILE mode only (source_kind == "FILE") — which file this partition
    # streams, its checksum (computed once at plan time — real SHA-256 via
    # connector.file_checksum(), used both for the archive record and to
    # skip a duplicate on a future run), and whether the post-completion
    # move to PipelineRun.archive_dir actually happened.
    file_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[PartitionStatus] = mapped_column(Enum(PartitionStatus), default=PartitionStatus.PENDING, index=True)
    records_read: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<PipelineRunPartition run={self.run_id} #{self.partition_index} status={self.status}>"
