from typing import Any

from pydantic import BaseModel


class ExtractionFieldSpec(BaseModel):
    name: str
    type: str = "string"
    required: bool = False
    instruction: str = ""
    # Only meaningful when type == "table" — the row shape (see
    # document_taxonomies.py's table entries / extraction_service.py's
    # detect_schema()). Optional so scalar fields stay unaffected; without
    # this, a table field's columns were silently dropped the moment
    # detect-schema's response passed through this model, which then made
    # /extract impossible to populate meaningfully for that field (a real,
    # previously-existing bug — not introduced by the Custom Engine, just
    # reliably triggered by it since its schemas always include a table).
    columns: list[dict] | None = None


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
    type: str = "scalar"
    # Scalar shape: value + page. Table shape: columns + rows instead —
    # a table field's real value IS its rows, not a single scalar (see
    # extraction_service.extract_fields()'s docstring). Both optional so
    # one model covers either shape without a 500 on whichever one isn't
    # in play (the actual bug this fixes: every table-shaped field made
    # the whole /extract response fail validation outright).
    value: Any = None
    confidence: float
    page: int | None = None
    # Plain column NAME strings, matching what extraction_service.
    # extract_fields()/custom_engine.extract_fields() actually put here
    # ([c["name"] for c in ...]) — NOT the {name,type} dicts the REQUEST
    # side (ExtractionFieldSpec.columns) uses. Getting this wrong is what
    # caused the 500 here even after fixing the request side.
    columns: list[str] | None = None
    rows: list[dict] | None = None


class ExtractOut(BaseModel):
    fields: list[ExtractedFieldOut]
    overallConfidence: float
    model: str
    rawJson: str
