from datetime import datetime
from typing import Any

from pydantic import BaseModel


class LineageNodeOut(BaseModel):
    id: str
    type: str
    label: str
    subtitle: str | None = None
    meta: dict[str, Any] = {}
    execution: dict[str, Any] | None = None


class LineageEdgeOut(BaseModel):
    id: str
    source: str
    target: str
    label: str | None = None
    edge_type: str


class PipelineLineageOut(BaseModel):
    pipeline: dict[str, Any]
    version_number: int
    nodes: list[LineageNodeOut]
    edges: list[LineageEdgeOut]


class PipelineVersionOut(BaseModel):
    version_number: int
    is_current: bool
    created_at: datetime


class PipelineVersionListOut(BaseModel):
    items: list[PipelineVersionOut]


class ExecutionLineageOut(BaseModel):
    job_id: int
    pipeline_id: int
    pipeline_name: str
    pipeline_version: int
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    records_in: int | None
    records_out: int | None
    graph: PipelineLineageOut


class ColumnLineageRowOut(BaseModel):
    source_system: str | None
    source_dataset: str | None
    source_object: str | None
    source_columns: list[str]
    transformation: str | None
    target_system: str | None
    target_dataset: str | None
    target_object: str | None
    target_column: str | None
    mapping_type: str
    status: str | None


class ColumnMappingLineageOut(BaseModel):
    mapping_id: int
    name: str
    source_connection: str | None
    source_object: str | None
    target_connection: str | None
    target_object: str | None
    columns: list[ColumnLineageRowOut]
    created_by: int | None
    updated_by: int | None
    created_at: datetime
    updated_at: datetime


class ColumnMappingSummaryOut(BaseModel):
    id: int
    name: str
    source_object: str | None
    target_object: str | None
    status: str


class ColumnMappingListOut(BaseModel):
    items: list[ColumnMappingSummaryOut]


class ImpactOut(BaseModel):
    asset: dict[str, Any]
    used_by_pipelines: list[dict[str, Any]]
    used_by_mappings: list[dict[str, Any]]
    downstream_pipeline_count: int
    upstream_pipeline_count: int
    warning_summary: str


class SearchResultOut(BaseModel):
    asset_type: str
    asset: str
    system: str | None
    owner: str | None
    used_by: int | None
    upstream_count: int | None
    downstream_count: int | None
    ref: dict[str, Any]


class SearchResultsOut(BaseModel):
    items: list[SearchResultOut]


class LineageOverviewOut(BaseModel):
    pipeline_count: int
    column_mapping_count: int
    connection_count: int
    pipelines: list[dict[str, Any]]
