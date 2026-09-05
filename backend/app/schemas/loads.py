from datetime import datetime

from pydantic import BaseModel


class StartLoadRequest(BaseModel):
    """write_mode: APPEND (default) | TRUNCATE_INSERT | UPSERT — see
    models/target_load_run.py for what each really does. key_columns for
    UPSERT are never taken from the request — they're auto-detected from
    the target's live primary key at run time (see load_executor.py).

    create_table: real CREATE TABLE from the source's own column metadata
    before the first batch, if the target object doesn't exist yet —
    properly sized VARCHAR2/VARCHAR, not blanket CLOB/TEXT (JJ1).
    recreate_table additionally drops an existing target table first.

    worker_count/partition_count only matter when this run actually ends
    up in KEY_RANGE mode (a plain column-copy mapping against a source
    that supports key-range reads — see load_executor.py's real,
    per-run capability probe, not a request flag); ignored otherwise.
    Both default to 4, same as extraction's own defaults."""
    write_mode: str | None = None
    chunk_size: int | None = None
    create_table: bool = False
    recreate_table: bool = False
    worker_count: int | None = None
    partition_count: int | None = None


class LoadRunOut(BaseModel):
    id: int
    mapping_id: int
    mapping_version: int | None = None  # CM4 — the exact ColumnMappingVersion this run pinned to at start time; None only for runs that predate versioning
    write_mode: str
    key_columns: list[str] | None
    chunk_size: int
    source_kind: str | None = None
    key_column: str | None = None
    worker_count: int
    partition_count: int
    create_table: bool
    recreate_table: bool
    status: str
    records_read: int
    records_written: int
    records_rejected: int = 0
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class LoadMetricsOut(BaseModel):
    status: str
    records_read: int
    records_written: int
    records_rejected: int = 0
    throughput_rps: float | None = None
    elapsed_seconds: float | None = None


class LoadListOut(BaseModel):
    items: list[LoadRunOut]
    total: int
