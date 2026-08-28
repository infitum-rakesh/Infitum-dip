"""
Review Queue models — the human-in-the-loop layer that sits between
Document Intelligence's confidence/validation check and the structured
dataset. See services/review_queue_service.py for the workflow logic.
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ReviewStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"


class ReviewPriority(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ReviewType(str, enum.Enum):
    OCR = "OCR"
    AI_EXTRACTION = "AI_EXTRACTION"
    VALIDATION = "VALIDATION"
    BUSINESS_RULE = "BUSINESS_RULE"


class ReviewQueueItem(TenantBase):
    __tablename__ = "review_queue_items"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    # FK only, no relationship() back-populates — stays additive, doesn't
    # touch models/document.py, models/organization.py or models/user.py.
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    assigned_to_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    review_type: Mapped[ReviewType] = mapped_column(Enum(ReviewType), default=ReviewType.AI_EXTRACTION)
    issue: Mapped[str] = mapped_column(String(200), nullable=False)
    document_type: Mapped[str] = mapped_column(String(50), default="Other")  # Invoice|Contract|Purchase Order|Customer|Other

    # The primary flagged field — table/detail summary. Full field-by-field
    # review reads live from the parent Document's extraction_result so this
    # never duplicates OCR/AI Extraction data, just points at it.
    field_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    extracted_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expected_value: Mapped[str | None] = mapped_column(String(500), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # valid|warning|invalid

    status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.PENDING, index=True)

    # Staged corrections {field_name: corrected_value}, applied to the
    # document's extraction_result on Approve.
    corrections: Mapped[dict] = mapped_column(JSON, default=dict)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<ReviewQueueItem doc={self.document_id} {self.issue} ({self.status})>"


class ReviewAuditLog(TenantBase):
    """Every review action, logged. Never mutated, only appended to."""
    __tablename__ = "review_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    review_item_id: Mapped[int] = mapped_column(ForeignKey("review_queue_items.id"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True)  # null = system-generated

    action: Mapped[str] = mapped_column(String(100), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ReviewAuditLog item={self.review_item_id} {self.action}>"
