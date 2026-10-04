"""
tally_approval_service.py — TALLY-H: the write-back approval gate.

Deliberately NOT auto-triggered the way invoice_decision_service.py
auto-triggers Payment Approval for every STRAIGHT_THROUGH invoice: a
TallyVoucherPreview can be rebuilt many times (TALLY-D's
build_preview() upserts in place) before a user is ready to actually
seek write-back authorization, and this feature's own master directive
requires "explicit human authorization" for every Tally write, not a
silent background trigger. submit_for_approval() is therefore only ever
called from a real user action (the "Submit for Approval" button on the
Voucher Preview page, api/tally.py's own route).

Reuses approval_workflow_engine_service.create_workflow() unforked, with
entity_type=TALLY_VOUCHER — see ApprovalEntityType's own docstring
(models/approval/rules.py) for why resolve_candidate_rules() scopes
rules by entity_type so an org's existing invoice-approval rules can
never accidentally also gate Tally vouchers.

Disclosed, not silent, gap: if this org has never published a
TALLY_VOUCHER-scoped ApprovalRule, create_workflow() returns a terminal
AUTO_APPROVED workflow (rule_id=NULL) — identical in spirit to how an
org that never configures a PAYMENT_APPROVAL rule gets the same
behavior for invoices (see approval_workflow_engine_service.
_create_auto_approved()'s own docstring). TALLY-I's write-back gate
still only checks `preview.approval_status == APPROVED`, so the actual
protection against unauthorized writes is this: an org that cares about
gating Tally write-back MUST configure a rule, exactly like Payment
Approval already requires.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval.rules import ApprovalEntityType
from app.models.approval.workflow import ApprovalWorkflowStatus
from app.models.invoice_intelligence.core import ApInvoice
from app.models.tally_mapping import TallyVoucherApprovalStatus, TallyVoucherPreview, TallyVoucherPreviewStatus
from app.services.approval import approval_workflow_engine_service as approval_engine
from app.services.approval import rule_engine_service as approval_rule_engine


class TallyApprovalError(Exception):
    pass


async def submit_for_approval(
    db: AsyncSession, *, preview_id: int, organization_id: int, actor_user_id: int | None,
) -> tuple[TallyVoucherPreview, object]:
    """Submits a READY voucher preview for write-back authorization.
    Returns (preview, workflow). Raises TallyApprovalError for a missing
    preview, a cross-org preview, or a BLOCKED preview (TALLY-D's own
    deterministic pre-write checks failed — no amount of human approval
    should override a real structural problem like an unbalanced
    voucher; the preview must be rebuilt clean first)."""
    preview = await db.get(TallyVoucherPreview, preview_id)
    if preview is None or (preview.organization_id is not None and preview.organization_id != organization_id):
        raise TallyApprovalError(f"Voucher preview {preview_id} was not found in this organization.")
    if preview.status != TallyVoucherPreviewStatus.READY:
        raise TallyApprovalError("Only a READY voucher preview (all deterministic checks passed) can be submitted for approval.")

    invoice = await db.get(ApInvoice, preview.invoice_id)
    context = approval_rule_engine.ApprovalMatchContext(
        amount=invoice.total if invoice else None,
        currency=invoice.currency if invoice else None,
        conditions={k: v for k, v in {
            "SUPPLIER": preview.party_ledger_name,
            "COMPANY": preview.company_name,
            "SOURCE_SYSTEM": "TALLY",
        }.items() if v},
    )

    preview.approval_status = TallyVoucherApprovalStatus.PENDING
    await db.commit()

    try:
        workflow = await approval_engine.create_workflow(
            db, organization_id=organization_id, entity_type=ApprovalEntityType.TALLY_VOUCHER,
            entity_id=preview.id, context=context, created_by=actor_user_id,
        )
    except approval_engine.ApprovalWorkflowEngineError as exc:
        preview.approval_status = TallyVoucherApprovalStatus.NOT_SUBMITTED
        await db.commit()
        raise TallyApprovalError(str(exc)) from exc

    preview.approval_workflow_id = workflow.id
    # AUTO_APPROVED is just as terminal-and-cleared as a real human
    # APPROVED — see this module's own docstring on why an unconfigured
    # org's Tally vouchers land here, same as unconfigured Payment
    # Approval does for invoices.
    #
    # Real bug found live (reasoned through before ever exercising it):
    # create_workflow()'s duplicate-suppression treats APPROVED the same
    # as PENDING/IN_PROGRESS/AUTO_APPROVED — a caller resubmitting an
    # ALREADY-APPROVED preview gets that same pre-existing APPROVED
    # workflow back, not a new one. But this function unconditionally
    # sets approval_status=PENDING a few lines above, BEFORE calling
    # create_workflow() — so without this dispatch, an already-approved
    # preview would regress to PENDING on a stray resubmission, contradicting
    # the workflow it just got back. Only PENDING/IN_PROGRESS genuinely
    # means "still PENDING" here; every terminal status must be reflected
    # immediately, not left for a future action that will never come
    # (there is nothing left to act on for an already-terminal workflow).
    if workflow.status in (ApprovalWorkflowStatus.APPROVED, ApprovalWorkflowStatus.AUTO_APPROVED):
        preview.approval_status = TallyVoucherApprovalStatus.APPROVED
    elif workflow.status == ApprovalWorkflowStatus.REJECTED:
        preview.approval_status = TallyVoucherApprovalStatus.REJECTED
    await db.commit()
    await db.refresh(preview)
    return preview, workflow
