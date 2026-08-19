from typing import Any

from pydantic import BaseModel


class ExtractionFieldSpec(BaseModel):
    name: str
    type: str = "string"
    required: bool = False
    instruction: str = ""


class DetectSchemaRequest(BaseModel):
    document_id: int
    connection_id: int
    # e.g. "INVOICE" — see document_taxonomies.py's available_document_types().
    # None/omitted keeps the original ad-hoc "let the LLM guess 5-12 fields"
    # behavior; when set, returns a comprehensive canonical schema instead
    # (merged with field names mined from this org's own past documents).
    document_type: str | None = None


class DetectSchemaOut(BaseModel):
    fields: list[ExtractionFieldSpec]


class ExtractRequest(BaseModel):
    document_id: int
    connection_id: int
    fields: list[ExtractionFieldSpec]


class ExtractedFieldOut(BaseModel):
    name: str
    value: Any
    confidence: float
    page: int


class ExtractOut(BaseModel):
    fields: list[ExtractedFieldOut]
    overallConfidence: float
    model: str
    rawJson: str
