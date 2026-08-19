"""
Document model — the repository/management layer for uploaded files.

Documents own their file + processing state. The actual heavy lifting
(image scanning, OCR, AI extraction) conceptually belongs to the Document
Intelligence pipeline; DocumentProcessingService (services/document_service.py)
is the seam between this repository and that pipeline — see the module
docstring there for why it currently simulates pipeline output.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class DocumentType(str, enum.Enum):
    PDF = "PDF"
    IMAGE = "IMAGE"
    EXCEL = "EXCEL"
    WORD = "WORD"
    TEXT = "TEXT"
    OTHER = "OTHER"


class DocumentStatus(str, enum.Enum):
    UPLOADED = "UPLOADED"
    SCANNING = "SCANNING"
    OCR_PROCESSING = "OCR_PROCESSING"
    AI_EXTRACTION = "AI_EXTRACTION"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # Scoping — FK only, no relationship() back-populates so this stays
    # fully additive and doesn't touch models/organization.py or models/user.py.
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    folder: Mapped[str] = mapped_column(String(500), default="/")
    doc_type: Mapped[DocumentType] = mapped_column(Enum(DocumentType), default=DocumentType.OTHER)
    mime_type: Mapped[str | None] = mapped_column(String(150), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=1)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    # SHA-256 of the raw uploaded bytes — lets the upload path detect a
    # genuine re-upload of the exact same file within an organization
    # (see document_service.save_upload()'s duplicate check) without
    # blocking it; content-identical files can legitimately need their own
    # separate Document row (e.g. reprocessing after a fix).
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    status: Mapped[DocumentStatus] = mapped_column(Enum(DocumentStatus), default=DocumentStatus.UPLOADED, index=True)
    pipeline: Mapped[str | None] = mapped_column(String(150), nullable=True)

    ocr_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    extraction_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Processing timeline: [{stage, status, started_at, ended_at, duration_seconds, confidence, error}, ...]
    timeline: Mapped[list] = mapped_column(JSON, default=list)
    # {full_text, confidence, regions: [{page, text, bbox:[x,y,w,h], confidence}]}
    ocr_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {fields: [{name, value, confidence, source_page, validation_status}]}
    extraction_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # {schema: [{field,type}], table: {columns:[...], rows:[[...]]}, json: {...}}
    dataset: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_time_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Document {self.filename} ({self.status})>"
