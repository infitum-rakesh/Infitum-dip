from datetime import datetime

from pydantic import BaseModel


class PipelineSourceFileOut(BaseModel):
    id: int
    pipeline_id: int | None
    node_id: str
    filename: str
    extension: str
    mime_type: str | None
    size_bytes: int
    delimiter: str | None
    sheet_names: list[str] | None
    selected_sheet: str | None
    row_count: int | None
    column_count: int | None
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class SetSheetRequest(BaseModel):
    sheet_name: str


class AttachToPipelineRequest(BaseModel):
    pipeline_id: int
