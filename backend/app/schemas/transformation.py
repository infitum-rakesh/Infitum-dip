from datetime import datetime

from pydantic import BaseModel


class TransformationJobCreateRequest(BaseModel):
    name: str = "Untitled Conversion"

    source_kind: str  # "UPLOADED_FILE" | "CONNECTION"
    source_format: str  # CSV | JSON | EXCEL | XML | PARQUET | TABLE
    source_uploaded_file_id: int | None = None
    source_connection_id: int | None = None
    source_schema: str | None = None
    source_object: str | None = None

    target_kind: str  # "DOWNLOAD_FILE" | "CONNECTION"
    target_format: str
    target_connection_id: int | None = None
    target_schema: str | None = None
    target_object: str | None = None
    # Target-format writer options (only apply for target_kind=DOWNLOAD_FILE
    # — a TABLE target has no equivalent concept). Recognized keys depend
    # on target_format; unrecognized/inapplicable keys are ignored, see
    # sql_export_writers.make_writer()'s own docstring:
    #   CSV: delimiter, quotechar, encoding
    #   EXCEL: sheet_name
    #   XML: root_element, row_element
    format_options: dict | None = None


class TransformationJobOut(BaseModel):
    id: int
    name: str
    source_kind: str
    source_format: str
    source_uploaded_file_id: int | None
    source_connection_id: int | None
    source_schema: str | None
    source_object: str | None
    target_kind: str
    target_format: str
    target_connection_id: int | None
    target_schema: str | None
    target_object: str | None
    format_options: dict | None
    status: str
    row_count: int
    file_size_bytes: int | None
    error_message: str | None
    created_by: int | None
    created_by_name: str | None = None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TransformationJobListOut(BaseModel):
    items: list[TransformationJobOut]
    total: int


class TransformationCancelOut(BaseModel):
    cancelled: bool
    message: str
