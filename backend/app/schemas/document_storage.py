from datetime import datetime

from pydantic import BaseModel


class ProcessedDataStorageConfigOut(BaseModel):
    id: int
    mode: str
    storage_type: str
    load_mode: str
    file_format: str
    target_connection_id: int | None
    target_schema: str | None
    target_table: str
    include_ocr_text: bool
    include_extraction_fields: bool
    include_dataset: bool
    include_ai_raw_response: bool
    last_pushed_document_id: int | None
    last_pushed_at: datetime | None
    last_status: str
    last_error: str | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProcessedDataStorageConfigUpdateRequest(BaseModel):
    mode: str | None = None  # "DEFAULT" | "EXTERNAL"
    storage_type: str | None = None  # "SQLITE" | "DATABASE" | "FILE"
    load_mode: str | None = None  # "CREATE_IF_NOT_EXISTS" | "APPEND" | "TRUNCATE_AND_LOAD" | "DROP_AND_RECREATE"
    file_format: str | None = None  # "JSONL" | "CSV" | "PARQUET"
    target_connection_id: int | None = None
    target_schema: str | None = None
    target_table: str | None = None
    include_ocr_text: bool | None = None
    include_extraction_fields: bool | None = None
    include_dataset: bool | None = None
    include_ai_raw_response: bool | None = None
    # Explicit presence flags — a plain Optional field can't distinguish
    # "clear this to null" from "the caller didn't send this field",
    # which matters for target_connection_id/target_schema (None is a
    # real, meaningful value: no target / no schema). The frontend always
    # sends the full form, so these default true; only a partial PATCH-
    # style caller needs to set either to false to leave the field alone.
    clear_target_connection: bool = False
    clear_target_schema: bool = False


class TestConnectionResultOut(BaseModel):
    success: bool
    message: str
    latency_ms: float | None = None


class DocumentDatasetOut(BaseModel):
    """One dataset_type's row for a document — see
    models.document_storage.DocumentProcessedDataset."""
    dataset_type: str
    payload: dict | list | None
    updated_at: datetime

    model_config = {"from_attributes": True}
