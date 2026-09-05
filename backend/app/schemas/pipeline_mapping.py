"""
Schemas for the Pipeline <-> Column Mapping attach/upgrade surface
(api/pipeline_mapping.py). Kept as its own small file rather than folded
into schemas/mapping.py, matching this build's "don't put everything in
one file" instruction and the same one-file-per-router convention the
rest of this codebase already uses.
"""
from datetime import datetime

from pydantic import BaseModel


class NodeMappingRefOut(BaseModel):
    """What a column_mapping_step node on a given pipeline currently
    references, resolved straight from that pipeline's CURRENT
    PipelineVersion.pipeline_definition — never from column_mapping_usage,
    which is history only (see models/column_mapping_usage.py)."""
    node_id: str
    mapping_id: int | None
    mapping_version: int | None
    mapping_name: str | None = None
    mapping_status: str | None = None  # the referenced version's own status (DRAFT/VALIDATED/PUBLISHED/RETIRED)
    is_current_published: bool = False  # whether mapping_version == the mapping's current_published_version


class AttachMappingRequest(BaseModel):
    node_id: str
    mapping_id: int
    mapping_version: int
    # Optional — if the caller (ColumnMappingConfig.jsx) has the pipeline's
    # resolved upstream Source Dataset / downstream Target node refs in
    # scope, passing them here backs a real source/target consistency
    # check (spec section 18) rather than skipping it.
    pipeline_source_connection_id: int | None = None
    pipeline_source_schema: str | None = None
    pipeline_source_object: str | None = None
    pipeline_target_connection_id: int | None = None
    pipeline_target_schema: str | None = None
    pipeline_target_object: str | None = None


class ConsistencyIssue(BaseModel):
    side: str  # "source" | "target"
    message: str


class AttachMappingResponse(BaseModel):
    usage_id: int
    consistent: bool
    issues: list[ConsistencyIssue] = []


class UpgradeMappingRequest(BaseModel):
    node_id: str
    to_version: int


class UpgradeMappingResponse(BaseModel):
    pipeline_id: int
    new_pipeline_version: int
    node_id: str
    mapping_id: int
    from_version: int
    to_version: int


class MappingUsageEntryOut(BaseModel):
    id: int
    mapping_id: int
    mapping_version: int
    pipeline_id: int
    pipeline_name: str | None = None
    pipeline_version: int
    node_id: str
    environment: str
    created_at: datetime

    model_config = {"from_attributes": True}
