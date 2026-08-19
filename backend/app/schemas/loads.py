from datetime import datetime

from pydantic import BaseModel


class StartLoadRequest(BaseModel):
    """write_mode: APPEND (default) | TRUNCATE_INSERT | UPSERT — see
    models/target_load_run.py for what each really does. key_columns for
    UPSERT are never taken from the request — they're auto-detected from
    the target's live primary key at run time (see load_executor.py)."""
    write_mode: str | None = None
    chunk_size: int | None = None


class LoadRunOut(BaseModel):
    id: int
    mapping_id: int
    write_mode: str
    key_columns: list[str] | None
    chunk_size: int
    status: str
    records_read: int
    records_written: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class LoadMetricsOut(BaseModel):
    status: str
    records_read: int
    records_written: int
    throughput_rps: float | None = None
    elapsed_seconds: float | None = None


class LoadListOut(BaseModel):
    items: list[LoadRunOut]
    total: int
