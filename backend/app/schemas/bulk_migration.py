"""Bulk Object Migration — request/response schemas (BM6). Response shapes
match what bulk_migration_config_service.py/bulk_migration_planner_service.py/
bulk_migration_service.py's `to_out()`-style converters (defined at the
bottom of this module) actually build from the ORM rows — never bound
directly via from_attributes, matching this codebase's own established
convention of a service-layer to_out() rather than ORM auto-serialization
(see e.g. connection_service.to_out(), schedule_service.to_out())."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Pipelines ────────────────────────────────────────────────────────────

class PipelineCreateRequest(BaseModel):
    name: str
    description: str | None = None
    source_connection_id: int
    target_connection_id: int
    max_workers: int = 8
    batch_size: int = 5000
    max_retries: int = 3
    retry_backoff_seconds: float = 10.0
    continue_on_error: bool = True
    max_concurrent_runs: int = 1


class PipelineUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    source_connection_id: int | None = None
    target_connection_id: int | None = None
    max_workers: int | None = None
    batch_size: int | None = None
    max_retries: int | None = None
    retry_backoff_seconds: float | None = None
    continue_on_error: bool | None = None
    max_concurrent_runs: int | None = None


class PipelineOut(BaseModel):
    id: int
    name: str
    description: str | None
    source_connection_id: int
    target_connection_id: int
    max_workers: int
    batch_size: int
    max_retries: int
    retry_backoff_seconds: float
    continue_on_error: bool
    max_concurrent_runs: int
    status: str
    version: int
    object_count: int = 0
    created_at: datetime
    updated_at: datetime


class PipelineListOut(BaseModel):
    items: list[PipelineOut]
    total: int
    page: int
    page_size: int
    total_pages: int


# ── Objects ──────────────────────────────────────────────────────────────

class ObjectCreateRequest(BaseModel):
    source_schema: str | None = None
    source_object: str
    target_schema: str | None = None
    target_object: str
    object_type: str = "TABLE"
    load_type: str = "FULL_REPLACE"
    primary_key: list[str] | None = None
    filter_condition: str | None = None
    watermark_column: str | None = None
    priority: int = 100
    dependency_group: str | None = None
    dependency_order: int = 0
    depends_on: list[str] | None = None
    enabled: bool = True
    parallel_allowed: bool = True
    batch_size_override: int | None = None
    timeout_seconds: int | None = None
    max_workers_override: int | None = None
    retry_count_override: int | None = None
    truncate_before_load: bool = True
    create_target_if_not_exists: bool = True
    drop_and_recreate: bool = False
    reject_policy: str = "LOG"


class ObjectUpdateRequest(BaseModel):
    source_schema: str | None = None
    source_object: str | None = None
    target_schema: str | None = None
    target_object: str | None = None
    object_type: str | None = None
    load_type: str | None = None
    primary_key: list[str] | None = None
    filter_condition: str | None = None
    watermark_column: str | None = None
    priority: int | None = None
    dependency_group: str | None = None
    dependency_order: int | None = None
    depends_on: list[str] | None = None
    enabled: bool | None = None
    parallel_allowed: bool | None = None
    batch_size_override: int | None = None
    timeout_seconds: int | None = None
    max_workers_override: int | None = None
    retry_count_override: int | None = None
    truncate_before_load: bool | None = None
    create_target_if_not_exists: bool | None = None
    drop_and_recreate: bool | None = None
    reject_policy: str | None = None


class ObjectOut(BaseModel):
    id: int
    pipeline_id: int
    object_key: str
    source_schema: str | None
    source_object: str
    target_schema: str | None
    target_object: str
    object_type: str
    load_type: str
    primary_key: list[str] | None
    filter_condition: str | None
    watermark_column: str | None
    priority: int
    dependency_group: str | None
    dependency_order: int
    depends_on: list[str] | None
    enabled: bool
    parallel_allowed: bool
    batch_size_override: int | None
    timeout_seconds: int | None
    max_workers_override: int | None
    retry_count_override: int | None
    truncate_before_load: bool
    create_target_if_not_exists: bool
    drop_and_recreate: bool
    reject_policy: str
    estimated_row_count: int | None
    last_run_duration_seconds: float | None
    last_run_row_count: int | None


class ObjectListOut(BaseModel):
    items: list[ObjectOut]
    total: int
    page: int
    page_size: int
    total_pages: int


class BulkObjectActionRequest(BaseModel):
    object_ids: list[int]
    patch: dict[str, Any] = {}


class BulkObjectActionResult(BaseModel):
    affected: int


# ── Validate / dry-run ───────────────────────────────────────────────────

class ValidationDetailOut(BaseModel):
    level: str
    object_key: str | None = None
    code: str
    message: str


class ValidationResultOut(BaseModel):
    total: int
    valid: int
    warnings: int
    errors: int
    dependency_levels: int
    details: list[ValidationDetailOut]


class DryRunObjectActionOut(BaseModel):
    object_key: str
    load_type: str
    target_action: str
    estimated_rows: int | None


class DryRunResultOut(BaseModel):
    object_count: int
    dependency_levels: int
    levels_preview: list[list[str]]
    estimated_total_rows: int
    effective_workers: int
    actions: list[DryRunObjectActionOut]
    error: dict[str, str] | None = None


# ── Runs / Tasks / Events ────────────────────────────────────────────────

class RunOut(BaseModel):
    id: int
    pipeline_id: int
    pipeline_version: int
    run_key: str
    status: str
    trigger_type: str
    cancel_requested: bool
    pause_requested: bool
    effective_workers: int
    effective_batch_size: int
    continue_on_error: bool
    total_objects: int
    pending_objects: int
    queued_objects: int
    running_objects: int
    success_objects: int
    failed_objects: int
    skipped_objects: int
    cancelled_objects: int
    blocked_objects: int
    total_rows_source: int
    rows_processed: int
    rows_rejected: int
    error_count: int
    dependency_levels: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class RunListOut(BaseModel):
    items: list[RunOut]
    total: int
    page: int
    page_size: int
    total_pages: int


class TaskOut(BaseModel):
    id: int
    run_id: int
    object_key: str
    source_schema: str | None
    source_object: str
    target_schema: str | None
    target_object: str
    object_type: str
    load_type: str
    dependency_level: int
    status: str
    attempt_number: int
    worker_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    last_heartbeat: datetime | None
    source_row_count: int | None
    target_row_count: int | None
    rows_processed: int
    rows_rejected: int
    throughput_rows_per_sec: float | None
    error_code: str | None
    error_message: str | None


class TaskListOut(BaseModel):
    items: list[TaskOut]
    total: int
    page: int
    page_size: int
    total_pages: int


class TaskEventOut(BaseModel):
    id: int
    run_id: int
    task_id: int | None
    event_type: str
    event_timestamp: datetime
    worker_id: str | None
    message: str | None


class TaskEventListOut(BaseModel):
    items: list[TaskEventOut]
    since_id: int  # pass back as `since_id` on the next poll


class RetryFailedRequest(BaseModel):
    include_successful: bool = False


class RetryFailedResult(BaseModel):
    requeued: int


# ── Import / export ──────────────────────────────────────────────────────

class ImportResultOut(BaseModel):
    total: int
    valid: int
    invalid: int
    imported: int
    updated: int
    errors: list[str]
    unmapped_columns: list[str] = []
