"""
Payment Invoice Approval — Notification outbox model (Phase 6).

Closes the real, disclosed gap found during this feature's own Phase B
analysis: notification_service.create_notification() sends email
synchronously inline via email_service.send_email() — no queue, no
retry, no idempotency. Fine for the existing Bell/in-app write (a plain
DB insert, nothing to retry), never safe to reuse as-is for an approval
assignment that MUST eventually reach its approver even through a
transient SMTP outage. See services/approval/notification_outbox_service.py
(the actual enqueue/send/retry logic) for the full design; this module
is schema only.

One row per (workflow, step, notification_type, channel) tuple —
enforced by the unique constraint below, which is what makes
notification_outbox_service.enqueue() idempotent: calling it twice for
the same real-world event (e.g. an already-resolved step re-resolved by
an idempotent retry elsewhere) returns the existing row instead of
double-enqueuing, so an approver is never emailed twice for one
assignment.

step_id/step_kind use the same "0 = no specific step" sentinel
convention as extraction_service's connection_id=0 (Custom Extraction
Engine) — a workflow-level notification (e.g. a future "workflow fully
approved" event) is step_id=0, step_kind=WORKFLOW, never NULL, so the
unique constraint's dedup protection covers workflow-level events too
(NULL would compare unequal to itself in a unique constraint, defeating
the dedup for exactly the rows that need it most).
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ApprovalNotificationChannel(str, enum.Enum):
    EMAIL = "EMAIL"
    IN_APP = "IN_APP"


class ApprovalNotificationType(str, enum.Enum):
    STEP_ASSIGNED = "STEP_ASSIGNED"
    PEER_REVIEW_ASSIGNED = "PEER_REVIEW_ASSIGNED"
    REMINDER = "REMINDER"
    ESCALATED = "ESCALATED"
    WORKFLOW_APPROVED = "WORKFLOW_APPROVED"
    WORKFLOW_REJECTED = "WORKFLOW_REJECTED"


class ApprovalNotificationStepKind(str, enum.Enum):
    WORKFLOW = "WORKFLOW"  # not tied to one specific step — step_id stays the 0 sentinel
    APPROVAL = "APPROVAL"
    PEER_REVIEW = "PEER_REVIEW"


class ApprovalNotificationStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"        # will retry — attempt_count < max_attempts
    DEAD_LETTER = "DEAD_LETTER"  # exhausted retries (or no email on file) — needs a human, never retried again automatically


class ApprovalNotificationOutbox(TenantBase):
    __tablename__ = "approval_notification_outbox"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "step_id", "step_kind", "notification_type", "channel",
            name="uq_approval_notification_dedup",
        ),
        {"sqlite_autoincrement": True},  # PeerReviewStep-style tables reference nothing off this one today, but a future "notification delivery log" plausibly would — same precedent as every other table in this feature that accumulates FK children
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    workflow_id: Mapped[int] = mapped_column(ForeignKey("approval_workflows.id"), nullable=False, index=True)
    # Polymorphic reference into either approval_workflow_steps or
    # peer_review_steps (disambiguated by step_kind) — no ForeignKey
    # wrapper here even though both target tables are TenantBase, because
    # a single column can't carry two different FK targets; step_kind +
    # this id together are the real key, checked in application code
    # (notification_outbox_service.py), the same "no real DB constraint,
    # honestly disclosed" tradeoff review_queue_service's polymorphic
    # refs already accept elsewhere in this codebase. 0 = no specific
    # step (see module docstring's sentinel convention).
    step_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_kind: Mapped[ApprovalNotificationStepKind] = mapped_column(Enum(ApprovalNotificationStepKind), nullable=False, default=ApprovalNotificationStepKind.WORKFLOW)

    # No ForeignKey — User is PlatformBase, this table is TenantBase; see
    # models/approval/rules.py's ApprovalRule.created_by for the full
    # cross-boundary explanation, unchanged here.
    recipient_user_id: Mapped[int] = mapped_column(nullable=False, index=True)

    notification_type: Mapped[ApprovalNotificationType] = mapped_column(Enum(ApprovalNotificationType), nullable=False, index=True)
    channel: Mapped[ApprovalNotificationChannel] = mapped_column(Enum(ApprovalNotificationChannel), nullable=False)

    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[ApprovalNotificationStatus] = mapped_column(Enum(ApprovalNotificationStatus), default=ApprovalNotificationStatus.PENDING, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Due-now query key for the worker (notification_outbox_service.
    # process_pending()) — defaults to "now" so a freshly enqueued row is
    # immediately due; a failed attempt pushes this forward by the
    # backoff schedule instead of retrying instantly in a tight loop.
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ApprovalNotificationOutbox workflow={self.workflow_id} {self.notification_type.value}/{self.channel.value} {self.status}>"
