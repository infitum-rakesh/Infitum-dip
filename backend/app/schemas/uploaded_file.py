from datetime import datetime

from pydantic import BaseModel


class UploadedFileOut(BaseModel):
    id: int
    filename: str
    extension: str
    mime_type: str | None
    size_bytes: int
    row_count: int | None
    column_count: int | None
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class UploadedFileColumnsOut(BaseModel):
    columns: list[str]
