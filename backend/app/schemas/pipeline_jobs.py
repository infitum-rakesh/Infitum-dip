from datetime import datetime

from pydantic import BaseModel


class PipelineJobCreateRequest(BaseModel):
    pass  # nothing beyond the pipeline_id in the URL — trigger is always "Manual" for now


class PipelineJobMetricsProgressOut(BaseModel):
    percentage: float | None = None
    processed: int = 0
    total: int | None = None


class PipelineJobMetricsThroughputOut(BaseModel):
    rows_per_second: float = 0.0
    extraction_rate: float = 0.0
    load_rate: float = 0.0
    processing_rate: float = 0.0
    rows_per_minute: float = 0.0


class PipelineJobMetricsTimingOut(BaseModel):
    started_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed_seconds: float = 0.0
    eta_seconds: float | None = None


class PipelineJobMetricsBulkApiOut(BaseModel):
    current_chunk: int = 0
    total_chunks: int | None = None
    current_chunk_rows: int = 0
    chunks_processed: int = 0


class PipelineJobMetricsRecordsOut(BaseModel):
    extracted: int = 0
    processed: int = 0
    loaded: int = 0
    rejected: int = 0


class PipelineJobOut(BaseModel):
    id: int
    pipeline_id: int
    pipeline_version: int
    pipeline_name: str
    status: str
    stage: str | None = None
    trigger: str
    node_stats: dict
    records_in: int | None = None
    records_out: int | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_by: int | None = None
    created_by_name: str | None = None
    created_at: datetime
    progress: PipelineJobMetricsProgressOut | None = None
    throughput: PipelineJobMetricsThroughputOut | None = None
    timing: PipelineJobMetricsTimingOut | None = None
    bulk_api: PipelineJobMetricsBulkApiOut | None = None
    records: PipelineJobMetricsRecordsOut | None = None

    model_config = {"from_attributes": True}


class PipelineJobListOut(BaseModel):
    items: list[PipelineJobOut]
    total: int


class PipelineJobLogEntryOut(BaseModel):
    id: int
    ts: datetime
    level: str
    node_id: str | None = None
    message: str

    model_config = {"from_attributes": True}


class PipelineJobLogsOut(BaseModel):
    items: list[PipelineJobLogEntryOut]
    job_status: str  # lets the frontend know when to stop polling
