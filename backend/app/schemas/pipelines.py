from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


# ── Definition payload (the canvas' own node/edge shape, passed through) ──

class PipelineDefinition(BaseModel):
    nodes: list[dict] = []
    edges: list[dict] = []
    settings: dict = {}


# ── Responses ────────────────────────────────────────────────────

class PipelineOut(BaseModel):
    id: int
    name: str
    current_version: int
    created_by: int | None
    created_by_name: str | None = None
    updated_by: int | None
    updated_by_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PipelineWithDefinitionOut(PipelineOut):
    """Everything the designer needs to load a pipeline in one call."""
    nodes: list[dict]
    edges: list[dict]
    settings: dict


class PipelineListOut(BaseModel):
    items: list[PipelineOut]
    total: int


class PipelineVersionSummaryOut(BaseModel):
    version_number: int
    is_current: bool
    created_by: int | None
    created_by_name: str | None = None
    created_at: datetime
    change_summary: str | None


class PipelineVersionListOut(BaseModel):
    pipeline_id: int
    pipeline_name: str
    current_version: int
    items: list[PipelineVersionSummaryOut]  # newest first


class PipelineVersionDetailOut(PipelineVersionSummaryOut):
    nodes: list[dict]
    edges: list[dict]
    settings: dict


# ── Diff / compare ───────────────────────────────────────────────

class VersionDiffEntry(BaseModel):
    kind: Literal["node", "edge"]
    change: Literal["added", "removed", "modified"]
    id: str
    label: str
    before: dict | None = None
    after: dict | None = None
    field_changes: list[str] = []  # human-readable list, e.g. ["condition: status != 'void' → status = 'active'"]


class PipelineCompareOut(BaseModel):
    pipeline_id: int
    version_a: int
    version_b: int
    diff: list[VersionDiffEntry]


# ── Requests ─────────────────────────────────────────────────────

class PipelineCreateRequest(BaseModel):
    name: str
    nodes: list[dict] = []
    edges: list[dict] = []
    settings: dict = {}
    # Open Pipeline bug fix — set only on the first Save of a pipeline
    # opened from a document's "Open Pipeline" button (see
    # pipeline_service.get_pipeline_for_document()), so future clicks
    # reopen this same pipeline instead of creating a duplicate.
    document_id: int | None = None


class PipelineSaveRequest(BaseModel):
    """Save = always creates a new version. Never overwrites in place."""
    name: str | None = None
    nodes: list[dict]
    edges: list[dict]
    settings: dict = {}
    change_summary: str | None = None
