"""
Bulk Object Migration — definition-side models (Pipeline + Objects).

Naming note: this codebase already has an unrelated "migration" concept
(TenantMigrationJob — org/platform DB migration for Bring-Your-Own-Database,
see models/tenant_migration_job.py). To avoid collision every table/class
here is prefixed BulkMigration*/bulk_migration_*; the user-facing label is
"Bulk Object Migration".

Core requirement this satisfies: migrate 5,000+ source objects to a target
system with ONE pipeline definition — never one pipeline per object. A
BulkMigrationPipeline is the reusable PROCESS (source/target connections +
default execution knobs); BulkMigrationObject rows (thousands of them) are
the OBJECTS to migrate; a run (models/bulk_migration_run.py) turns enabled
objects into execution tasks processed by a bounded worker pool. See that
module's docstring for the execution-side tables and the immutability
mechanism (tasks freeze a copy of the object config they were created from).

source_connection_id/target_connection_id are plain ints with no
ForeignKey — same loose-coupling convention as
DocumentScheduleConfig.source_connection_id (models/document_schedule.py):
Connection is a real, tenant-scoped model, but a connection being deleted
later should surface as a clean "connection not found" runtime error, not
a migration-blocking FK, and no FK can span two tenants' physical databases
once an org migrates to its own BYODB anyway (see database/connection.py's
TenantBase docstring).

`version` is a plain int bumped whenever pipeline defaults or the object
list change meaningfully — mirrors PipelineRun.pipeline_version's own
plain-int-snapshot convention (models/pipeline_run.py) rather than a joined
version table: Transform Workspace's Pipeline/PipelineVersion pair
(models/pipeline.py) needs a real versioned JSON blob because it must
restore/diff a whole node-graph; migration objects don't need that because
each task already freezes its own config copy at run-start time, so a
lightweight int counter is enough to know which "generation" of config a
run used. Audit trail for config changes reuses the existing generic
AuditLog.record_event() (models/audit_log.py) — see "Bulk Object Migration"
added to AUDIT_MODULES there — rather than a bespoke audit table.
"""
import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class BulkMigrationLoadType(str, enum.Enum):
    FULL_REPLACE = "FULL_REPLACE"   # TRUNCATE+LOAD (default) or DROP+CREATE+LOAD if drop_and_recreate
    FULL_APPEND = "FULL_APPEND"     # straight insert, no truncate — NOT idempotent on rerun (documented, not solved)
    MERGE = "MERGE"                 # UPSERT keyed by primary_key — reuses loading/writers' existing ON CONFLICT support
    SCD1 = "SCD1"                   # accepted at config layer; executor raises UNSUPPORTED_LOAD_TYPE — see bulk_migration_task_executor.py
    SCD2 = "SCD2"                   # same — genuinely deferred, disclosed, not faked
    DELTA = "DELTA"                 # same


class BulkMigrationPipeline(TenantBase):
    """The reusable PROCESS. One row here + thousands of BulkMigrationObject
    rows is the whole point — "Run Pipeline" always creates exactly one
    BulkMigrationRun, never one run per object."""
    __tablename__ = "bulk_migration_pipelines"
    # Prevents SQLite's default rowid-reuse from letting a NEW pipeline
    # silently collide with an OLD deleted pipeline's still-present
    # object/run/task rows (delete_pipeline() cascades those away too —
    # see bulk_migration_config_service.py — but this is the same
    # belt-and-suspenders precedent models/pipeline.py's Pipeline/
    # PipelineVersion already established for exactly this failure class).
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_connection_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    target_connection_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # Defaults applied to every object unless the object itself overrides
    # (see BulkMigrationObject.*_override fields below). effective_workers
    # at run time = min(max_workers, source/target connection limits,
    # GLOBAL_SYSTEM_WORKER_LIMIT) — see bulk_migration_service.py.
    max_workers: Mapped[int] = mapped_column(Integer, default=8)
    batch_size: Mapped[int] = mapped_column(Integer, default=5000)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    retry_backoff_seconds: Mapped[float] = mapped_column(Float, default=10.0)
    continue_on_error: Mapped[bool] = mapped_column(Boolean, default=True)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, default=1)

    # ACTIVE (default) | DISABLED | ARCHIVED — a DISABLED/ARCHIVED pipeline
    # can't be run but its objects/history stay readable.
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)

    # Bumped on meaningful config change — see module docstring.
    version: Mapped[int] = mapped_column(Integer, default=1)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<BulkMigrationPipeline {self.id} {self.name!r}>"


class BulkMigrationObject(TenantBase):
    """One configured source->target object mapping. A pipeline holds
    thousands of these; each enabled row becomes exactly one
    BulkMigrationTask per run (see models/bulk_migration_run.py).

    object_key is deterministic (source_schema+source_object+target_schema+
    target_object) so the UniqueConstraint below both prevents duplicate
    task creation AND is the real duplicate-mapping-detection mechanism
    (spec's "detect duplicate source/target mappings") — enforced by the
    database, not just an app-level check.
    """
    __tablename__ = "bulk_migration_objects"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "object_key", name="uq_bulk_migration_object_key"),
        {"sqlite_autoincrement": True},  # see BulkMigrationPipeline's __table_args__ comment
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("bulk_migration_pipelines.id"), nullable=False, index=True)

    object_key: Mapped[str] = mapped_column(String(600), nullable=False, index=True)

    source_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_object: Mapped[str] = mapped_column(String(300), nullable=False)
    target_schema: Mapped[str | None] = mapped_column(String(200), nullable=True)
    target_object: Mapped[str] = mapped_column(String(300), nullable=False)
    object_type: Mapped[str] = mapped_column(String(20), default="TABLE")  # TABLE | VIEW | FILE

    load_type: Mapped[BulkMigrationLoadType] = mapped_column(
        Enum(BulkMigrationLoadType, name="bulk_migration_load_type"), default=BulkMigrationLoadType.FULL_REPLACE,
    )
    primary_key: Mapped[list | None] = mapped_column(JSON, nullable=True)  # list[str] column names
    filter_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    watermark_column: Mapped[str | None] = mapped_column(String(200), nullable=True)

    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower runs first within a dependency level, ties broken by estimated_row_count desc

    # Dependency ordering — two mechanisms, either or both:
    # 1) Coarse phase grouping: all objects sharing dependency_order run in
    #    the same "wave"; waves execute in ascending order.
    # 2) Fine-grained override: depends_on lists this pipeline's own
    #    object_key values this object must wait on. When any object in the
    #    pipeline sets depends_on, the planner compiles the WHOLE pipeline
    #    into a DAG via topological sort (see
    #    bulk_migration_planner_service.py::_compute_dependency_levels(),
    #    adapted from execution/pipeline_job_executor.py::_topo_sort()) and
    #    dependency_order is ignored; a cycle is rejected pre-execution as
    #    CONFIGURATION_ERROR/DEPENDENCY_ERROR, never discovered mid-run.
    dependency_group: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dependency_order: Mapped[int] = mapped_column(Integer, default=0)
    depends_on: Mapped[list | None] = mapped_column(JSON, nullable=True)  # list[str] object_key

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    parallel_allowed: Mapped[bool] = mapped_column(Boolean, default=True)

    # Overrides of the pipeline's own defaults; None = use pipeline default.
    batch_size_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_workers_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count_override: Mapped[int | None] = mapped_column(Integer, nullable=True)

    truncate_before_load: Mapped[bool] = mapped_column(Boolean, default=True)
    create_target_if_not_exists: Mapped[bool] = mapped_column(Boolean, default=True)
    drop_and_recreate: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_policy: Mapped[str] = mapped_column(String(20), default="LOG")  # FAIL | SKIP | LOG — see loading/writers write_batch_with_fallback()

    # Populated after each run this object appears in — feeds size-aware
    # scheduling (longest-job-first within a dependency level) in
    # bulk_migration_service.py. Null before the object's first run.
    estimated_row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_run_row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<BulkMigrationObject {self.object_key!r} pipeline={self.pipeline_id}>"
