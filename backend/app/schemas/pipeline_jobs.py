from datetime import datetime

from pydantic import BaseModel


class PipelineJobCreateRequest(BaseModel):
    pass  # nothing beyond the pipeline_id in the URL — trigger is always "Manual" for now


class PipelineJobOut(BaseModel):
    id: int
    pipeline_id: int
    pipeline_version: int
    pipeline_name: str
    status: str
    trigger: str
    node_stats: dict
    records_in: int | None
    records_out: int | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_by: int | None
    created_by_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PipelineJobListOut(BaseModel):
    items: list[PipelineJobOut]
    total: int


class PipelineJobLogEntryOut(BaseModel):
    id: int
    ts: datetime
    level: str
    node_id: str | None
    message: str

    model_config = {"from_attributes": True}


class PipelineJobLogsOut(BaseModel):
    items: list[PipelineJobLogEntryOut]
    job_status: str  # lets the frontend know when to stop polling
