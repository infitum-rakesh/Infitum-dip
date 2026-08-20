from datetime import datetime

from pydantic import BaseModel


class RejectedRecordOut(BaseModel):
    id: int
    node_id: str | None
    target_object: str | None
    row_data: dict
    error_message: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RejectedRecordListOut(BaseModel):
    items: list[RejectedRecordOut]
    total: int
