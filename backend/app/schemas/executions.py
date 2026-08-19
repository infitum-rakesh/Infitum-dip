from datetime import datetime
from typing import Any

from pydantic import BaseModel


class StartRunRequest(BaseModel):
    """node_id must point at a 'source' node in the pipeline's current
    saved version with objectType TABLE/VIEW/QUERY/FILE and a
    connectionId — see execution_service.create_run() for the exact
    validation. key_column is required for TABLE/VIEW (drives
    partitioning); ignored for QUERY/FILE. archive_dir/error_dir are
    FILE-only — if set, worker.run_file_partition() genuinely moves each
    source file there via the connector's move_file() after it completes
    or fails; left unset, files are read in place and never moved."""
    node_id: str
    key_column: str | None = None
    chunk_size: int | None = None
    worker_count: int | None = None
    partition_count: int | None = None
    archive_dir: str | None = None
    error_dir: str | None = None


class PartitionOut(BaseModel):
    partition_index: int
    range_start: str | None
    range_end: str | None
    last_key_read: str | None
    file_name: str | None = None
    file_checksum: str | None = None
    archived: bool = False
    status: str
    records_read: int
    retry_count: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class RunOut(BaseModel):
    id: int
    pipeline_id: int
    pipeline_version: int
    node_id: str
    connection_id: int
    schema_name: str | None
    object_name: str | None
    custom_sql: str | None
    sql_parameters: dict[str, Any] | None
    key_column: str | None
    key_type: str | None
    source_kind: str | None = None
    archive_dir: str | None = None
    error_dir: str | None = None
    chunk_size: int
    worker_count: int
    partition_count: int
    status: str
    records_read: int
    records_written: int
    error_message: str | None
    staging_location: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RunStatusOut(BaseModel):
    run: RunOut
    partitions: list[PartitionOut]


class RunMetricsOut(BaseModel):
    status: str
    records_read: int
    records_written: int
    throughput_rps: float | None = None
    elapsed_seconds: float | None = None
    eta_seconds: float | None = None
    partitions_complete: int
    partitions_total: int


class RunListOut(BaseModel):
    items: list[RunOut]
    total: int
