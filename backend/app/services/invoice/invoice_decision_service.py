"""
invoice_decision_service.py — the real confidence model + decision matrix
that turns invoice_validation_service.py's (INV6) rule results into one
of ApInvoiceDecision's four outcomes: STRAIGHT_THROUGH | REVIEW_REQUIRED
| REJECT | RETRY. This is the last step of the automated pipeline before
either ERP posting (INV9, on STRAIGHT_THROUGH/after review) or a human
correction cycle (Review Queue — reused unmodified, not rebuilt).

Confidence model (Technical Design §3 — kept as five DISCRETE named
scores on ApInvoice, never collapsed into one number, so "high confidence
never bypasses business validation" is enforced in code, not just
policy):

  extractor_confidence   — already set at materialization time (INV5)
                            from Document.extraction_percent — the raw
                            extractor/OCR signal, untouched here.
  evidence_quality        — % of non-missing scalar fields that carry a
                            real evidence bbox (INV4) — "how much of what
                            we extracted can we point at on the actual
                            page." None when there are no non-missing
                            fields to evaluate at all (nothing to score).
  field_consistency       — derived from the ARITHMETIC validation
                            result (does subtotal+tax-discount+freight
                            reconcile to total) — reuses that rule's own
                            PASS/WARNING/FAIL rather than recomputing
                            anything. None if no ARITHMETIC rule ran.
  reference_match_score   — derived from the REFERENCE validation result
                            (vendor recognized in reference data). None
                            if no REFERENCE rule ran.
  business_validation_score — the share of EXCEPTION-severity rules that
                            PASSed (WARNING-severity rules don't count —
                            they're advisory by design, see
                            ApInvoiceRuleSeverity). None if no
                            EXCEPTION-severity rule ran at all.

Every score is either a real, derived number or None (genuinely nothing
to compute yet) — never a fabricated placeholder. decide() below treats
a None score as "insufficient information," not as a passing or failing
value.

Decision matrix (deterministic, not a black box):
  1. Any EXCEPTION-severity DUPLICATE result is a BLOCKED duplicate ->
     REJECT outright — a known-bad business condition a re-look at the
     same evidence won't resolve; a genuinely wrong duplicate match is a
     data problem to fix at the source, not something to wave through.
  2. Any OTHER EXCEPTION-severity rule FAILed -> REVIEW_REQUIRED — a real
     business-rule violation, but one a human correction (Review Queue,
     reused unmodified) can genuinely resolve (e.g. a missing field the
     OCR actually got right but this taxonomy mismapped). This is a
     genuine VALIDATION failure — it never reaches the Payment Approval
     engine below (see PAY9 section's own "low_confidence_review" note).
  3. Every computed confidence score (ignoring None ones) is >= the
     "straight-through" threshold AND at least one score was actually
     computed -> STRAIGHT_THROUGH.
  4. No confidence score could be computed at all (every score is None —
     e.g. extraction produced literally nothing usable) -> RETRY, the
     one case a fresh extraction attempt (not a human) is the right next
     step.
  5. Otherwise (some signal exists but below threshold, or below the
     lower REVIEW_REQUIRED->REJECT floor) -> REVIEW_REQUIRED. Real
     validation already PASSED here (step 2 didn't fire) — it's only
     confidence IN THE EXTRACTED VALUES that's below the bar, a genuinely
     different concern from a business-rule violation. Real bug found
     live: this case used to ALSO skip the Payment Approval engine
     entirely (see PAY9 section below), so an org's own configured
     approval rules never even got a chance to see these invoices —
     confidence was silently acting as an approval-workflow gate that no
     admin ever configured. It no longer does: see "low_confidence_review"
     below — this case still gets its Review Queue item (a human can fix
     the data), and NOW ALSO reaches the approval engine like any other
     validated invoice; only the ERP-posting decision itself is confidence-
     gated, not whether a workflow record exists at all.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceDecision, ApInvoiceStatus
from app.models.invoice_intelligence.rules import ApInvoiceRuleCategory, ApInvoiceRuleResult, ApInvoiceRuleSeverity, ApInvoiceValidationResult

STRAIGHT_THROUGH_THRESHOLD = 90.0  # every computed score must clear this for auto-post
REJECT_FLOOR = 30.0  # kept for disclosure/tuning — this build routes low confidence to REVIEW, never an automated REJECT on score alone (see decision matrix step 5)


def _rule_result_to_score(result: ApInvoiceRuleResult | None) -> float | None:
    if result is None:
        return None
    return {ApInvoiceRuleResult.PASS: 100.0, ApInvoiceRuleResult.WARNING: 60.0, ApInvoiceRuleResult.FAIL: 20.0}[result]


def compute_confidence_scores(invoice: ApInvoice, validation_results: list[ApInvoiceValidationResult], scalar_field_count: int, evidenced_field_count: int) -> None:
    """Mutates invoice's five confidence columns in place — extractor_
    confidence is left untouched (already set by invoice_processing_
    service.materialize_invoice()). scalar_field_count/evidenced_field_
    count come from the caller (already has doc.extraction_result in
    hand) rather than re-deriving them here."""
    invoice.evidence_quality = round(100.0 * evidenced_field_count / scalar_field_count, 1) if scalar_field_count > 0 else None

    arithmetic = next((r for r in validation_results if r.category == ApInvoiceRuleCategory.ARITHMETIC), None)
    invoice.field_consistency = _rule_result_to_score(arithmetic.result if arithmetic else None)

    reference = next((r for r in validation_results if r.category == ApInvoiceRuleCategory.REFERENCE), None)
    invoice.reference_match_score = _rule_result_to_score(reference.result if reference else None)

    exception_results = [r for r in validation_results if _rule_severity(r) == ApInvoiceRuleSeverity.EXCEPTION]
    if exception_results:
        passed = sum(1 for r in exception_results if r.result == ApInvoiceRuleResult.PASS)
        invoice.business_validation_score = round(100.0 * passed / len(exception_results), 1)
    else:
        invoice.business_validation_score = None


def _rule_severity(result: ApInvoiceValidationResult) -> ApInvoiceRuleSeverity | None:
    """The rule's severity at evaluation time — invoice_validation_
    service.evaluate_invoice() embeds it into details["severity"] on
    creation (see that module) rather than this module re-fetching
    ApInvoiceValidationRule by id; ApInvoiceValidationRule stays the one
    source of truth for CURRENT severity, this is just what it was AT
    THE TIME this specific result was produced."""
    raw = (result.details or {}).get("severity")
    return ApInvoiceRuleSeverity(raw) if raw else None


def _review_reason(invoice: ApInvoice, validation_results: list[ApInvoiceValidationResult], other_exception_fail: bool) -> str:
    """Real-bug fix — builds a human-readable REVIEW_REQUIRED reason for
    the cases _pick_primary_issue() (review_queue_service.py) can't see
    at all: an invoice-level validation-rule FAIL, or a below-threshold
    confidence score computed from those rules, with every extraction
    field itself perfectly clean. Only called when create_from_document()
    already came back empty — see decide()'s call site below."""
    if other_exception_fail:
        fails = [
            r.message for r in validation_results
            if r.result == ApInvoiceRuleResult.FAIL and _rule_severity(r) == ApInvoiceRuleSeverity.EXCEPTION and r.category != ApInvoiceRuleCategory.DUPLICATE
        ]
        if fails:
            return "; ".join(fails)

    score_labels = [
        ("extractor_confidence", "Extractor confidence"),
        ("evidence_quality", "Evidence quality"),
        ("field_consistency", "Field consistency (arithmetic)"),
        ("reference_match_score", "Reference match"),
        ("business_validation_score", "Business validation"),
    ]
    low = [
        f"{label} {value:.1f}%" for attr, label in score_labels
        if (value := getattr(invoice, attr)) is not None and value < STRAIGHT_THROUGH_THRESHOLD
    ]
    if low:
        return f"Below straight-through threshold ({STRAIGHT_THROUGH_THRESHOLD:.0f}%): " + ", ".join(low)
    return "Requires manual review — confidence scores did not clear the straight-through threshold."


async def decide(db: AsyncSession, invoice: ApInvoice, validation_results: list[ApInvoiceValidationResult]) -> ApInvoiceDecision:
    """Runs the confidence model + decision matrix, persists
    final_decision/decision_reasons/status onto `invoice`, and — only for
    REVIEW_REQUIRED — creates a real Review Queue item via the EXISTING,
    unmodified review_queue_service.create_from_document() (reused, not
    reimplemented, per this build's explicit reuse-infrastructure
    instruction). Returns the decision."""
    doc_result = await db.execute(select(Document).where(Document.id == invoice.document_id))
    doc = doc_result.scalar_one_or_none()
    fields = (doc.extraction_result or {}).get("fields", []) if doc else []
    scalar_fields = [f for f in fields if f.get("type") != "table"]
    non_missing = [f for f in scalar_fields if not f.get("missing")]
    evidenced = [f for f in non_missing if f.get("bbox")]
    compute_confidence_scores(invoice, validation_results, len(non_missing), len(evidenced))

    reasons: list[dict] = [
        {"rule_id": r.rule_id, "category": r.category.value, "result": r.result.value, "message": r.message}
        for r in validation_results
    ]

    duplicate_result = next((r for r in validation_results if r.category == ApInvoiceRuleCategory.DUPLICATE), None)
    duplicate_blocked = duplicate_result is not None and invoice.duplicate_check_result == "BLOCKED" and _rule_severity(duplicate_result) == ApInvoiceRuleSeverity.EXCEPTION

    other_exception_fail = any(
        r.result == ApInvoiceRuleResult.FAIL and _rule_severity(r) == ApInvoiceRuleSeverity.EXCEPTION and r.category != ApInvoiceRuleCategory.DUPLICATE
        for r in validation_results
    )

    scores = [invoice.extractor_confidence, invoice.evidence_quality, invoice.field_consistency, invoice.reference_match_score, invoice.business_validation_score]
    computed_scores = [s for s in scores if s is not None]

    if duplicate_blocked:
        decision = ApInvoiceDecision.REJECT
        invoice.status = ApInvoiceStatus.REJECTED
    elif other_exception_fail:
        decision = ApInvoiceDecision.REVIEW_REQUIRED
        invoice.status = ApInvoiceStatus.REVIEW_REQUIRED
    elif not computed_scores:
        decision = ApInvoiceDecision.RETRY
        invoice.status = ApInvoiceStatus.EXCEPTION
    elif all(s >= STRAIGHT_THROUGH_THRESHOLD for s in computed_scores):
        decision = ApInvoiceDecision.STRAIGHT_THROUGH
        invoice.status = ApInvoiceStatus.READY_TO_POST
    else:
        decision = ApInvoiceDecision.REVIEW_REQUIRED
        invoice.status = ApInvoiceStatus.REVIEW_REQUIRED

    # Real bug found live (user-reported): captured from the ORIGINAL
    # decision matrix result, before the anomaly check below can further
    # mutate `decision` — True only for decision-matrix step 5 above
    # (confidence too low, but validation itself found no EXCEPTION-
    # severity failure). Used at the PAY9 gate further down to let these
    # invoices reach the Payment Approval engine same as STRAIGHT_THROUGH
    # ones — see that section's own comment for why. Deliberately EXCLUDES
    # step 2's other_exception_fail (a genuine validation failure stays
    # excluded from approval, same as before) and anything the anomaly
    # check downgrades below (a STRAIGHT_THROUGH invoice an anomaly flags
    # keeps going to Review Queue only, not double-routed into approval
    # too — see that block's own comment).
    low_confidence_review = decision == ApInvoiceDecision.REVIEW_REQUIRED and not other_exception_fail

    invoice.final_decision = decision
    invoice.decision_reasons = reasons
    await db.commit()
    await db.refresh(invoice)

    if decision == ApInvoiceDecision.REVIEW_REQUIRED and doc is not None:
        from app.services import review_queue_service
        item = await review_queue_service.create_from_document(db, doc)
        if item is None:
            # Real-bug fix: _pick_primary_issue() found no flagged
            # extraction field (the extraction was clean) — but this
            # invoice reached REVIEW_REQUIRED via a validation-rule FAIL
            # or a below-threshold confidence score, neither of which
            # create_from_document() can see. Without this fallback the
            # invoice silently had no Review Queue entry at all.
            issue = _review_reason(invoice, validation_results, other_exception_fail)
            await review_queue_service.create_from_invoice_validation(db, doc, issue)

    # INV13 — narrow lifecycle event, never fails the real decision.
    try:
        from app.models.invoice_intelligence.learning import ApInvoiceAuditEventType
        from app.services.invoice import invoice_learning_service
        await invoice_learning_service.record_event(
            db, invoice_id=invoice.id, organization_id=invoice.organization_id,
            event_type=ApInvoiceAuditEventType.DECISION_MADE,
            message=f"Decision: {decision.value}",
            details={"decision": decision.value, "scores": scores},
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        pass

    # INV23 — anomaly check, real reuse of the Anomaly Detection engine
    # (see invoice_anomaly_service.py). Only meaningful before an
    # auto-post: a REVIEW_REQUIRED/REJECT/RETRY invoice is already headed
    # to a human or another extraction pass, so there's nothing to
    # downgrade. Never fails the real decision already committed above —
    # same "narrow, best-effort" contract as the DECISION_MADE event.
    if decision == ApInvoiceDecision.STRAIGHT_THROUGH and doc is not None:
        try:
            from app.models.invoice_intelligence.learning import ApInvoiceAuditEventType
            from app.services import review_queue_service
            from app.services.invoice import invoice_anomaly_service, invoice_learning_service

            findings = await invoice_anomaly_service.evaluate_invoice_anomalies(db, invoice, invoice.organization_id)
            critical = [f for f in findings if f["severity"] == "CRITICAL"]
            if critical:
                decision = ApInvoiceDecision.REVIEW_REQUIRED
                invoice.status = ApInvoiceStatus.REVIEW_REQUIRED
                invoice.final_decision = decision
                anomaly_reason = "; ".join(f["message"] for f in critical)
                reasons.append({"rule_id": None, "category": "ANOMALY", "result": "FAIL", "message": anomaly_reason})
                invoice.decision_reasons = reasons
                await db.commit()
                await db.refresh(invoice)
                await review_queue_service.create_from_anomaly(db, doc, f"Anomaly detected: {anomaly_reason}")
                await invoice_learning_service.record_event(
                    db, invoice_id=invoice.id, organization_id=invoice.organization_id,
                    event_type=ApInvoiceAuditEventType.ANOMALY_DETECTED,
                    message=f"{len(critical)} CRITICAL anomaly finding(s) downgraded STRAIGHT_THROUGH to REVIEW_REQUIRED.",
                    details={"findings": findings},
                )
                await db.commit()
            elif findings:
                # Real findings below CRITICAL — logged for visibility, doesn't change the decision.
                await invoice_learning_service.record_event(
                    db, invoice_id=invoice.id, organization_id=invoice.organization_id,
                    event_type=ApInvoiceAuditEventType.ANOMALY_DETECTED,
                    message=f"{len(findings)} anomaly finding(s), none CRITICAL — decision unchanged.",
                    details={"findings": findings},
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            pass

    # PAY9 — Payment Approval Workflow integration. Every prior phase's
    # docstring (most explicitly approval_workflow_engine_service.py's
    # own module docstring) deferred "automatically triggering
    # create_workflow() when a real invoice enters processing" to "a
    # later phase's job." This IS that phase, and this IS the single
    # choke point every intake path funnels through — document_service.py
    # (manual upload + scheduled + pipeline), review_queue_service.py
    # (post-correction re-decision), and invoice_inbox_service.py
    # (reprocess/revalidate) all call THIS function, so wiring it here
    # once covers all of them with no duplicated trigger logic anywhere.
    #
    # Real architecture bug found live (user-reported, matches the
    # project's own Payment Job Processing States spec): this used to be
    # gated on `decision == STRAIGHT_THROUGH` alone, which meant every
    # invoice the confidence model sent to REVIEW_REQUIRED (decision
    # matrix step 5 above) NEVER reached the Payment Approval engine at
    # all — confidence was silently acting as an undocumented approval-
    # workflow gate no admin ever configured, and "PAYMENT_ELIGIBLE ->
    # RULE_EVALUATION" never ran for the majority of real invoices in an
    # org whose extraction quality isn't consistently >=90%. Per that
    # spec, RULE_EVALUATION is the state every invoice reaches once
    # VALIDATED (validation-rule PASS — see decision matrix step 2's own
    # comment on why other_exception_fail stays excluded, a genuine
    # VALIDATION_FAILED case), and it alone decides AUTO_APPROVED vs
    # APPROVAL_REQUIRED — confidence never should have decided that by
    # skipping the engine outright. `low_confidence_review` (computed
    # above, right after the decision matrix) is exactly "validation
    # passed, only confidence in the extracted VALUES is low" — now
    # included here so it reaches the same rule evaluation a
    # STRAIGHT_THROUGH invoice does. What confidence still legitimately
    # gates is the ERP-POSTING decision, not workflow existence: see
    # invoice_erp_service.post_invoice()'s own STRAIGHT_THROUGH check —
    # an AUTO_APPROVED workflow on a low_confidence_review invoice is
    # real audit history, but the invoice's own status was already set to
    # REVIEW_REQUIRED above and is deliberately left there (not promoted
    # to READY_TO_POST) unless a matched rule actually resolves it — see
    # the status-handling comment further down.
    #
    # Still excluded: an invoice the anomaly check above downgraded FROM
    # STRAIGHT_THROUGH to REVIEW_REQUIRED — `low_confidence_review` is
    # False for that case (captured from the ORIGINAL decision, before
    # the anomaly check ran), so it stays headed to Review Queue only,
    # same "two competing needs-a-human paths" reasoning as before.
    #
    # Gated on the org actually holding an ACTIVE PAYMENT_APPROVAL
    # entitlement (services.billing.access_service, the same engine
    # every real API route in this platform is gated through) — a real
    # bug this phase found and closed before it ever shipped: an
    # org-agnostic platform-default ApprovalRule (organization_id=NULL)
    # would otherwise silently start gating ANY org's invoices on
    # approval, including orgs that never purchased the module.
    if decision == ApInvoiceDecision.STRAIGHT_THROUGH or low_confidence_review:
        try:
            from app.services.billing.access_service import access_service
            access = await access_service.has_module_access(db, invoice.organization_id, "PAYMENT_APPROVAL")
        except Exception:  # noqa: BLE001 — access-check failure must never block a real decision
            access = None

        if access is not None and access.granted:
            from app.models.approval.workflow import ApprovalWorkflowStatus
            from app.services.approval import approval_workflow_engine_service as approval_engine
            from app.services.approval import rule_engine_service as approval_rule_engine

            approval_context = approval_rule_engine.ApprovalMatchContext(
                amount=invoice.total, currency=invoice.currency,
                conditions={k: v for k, v in {
                    "SUPPLIER": invoice.vendor_name,
                    "SOURCE_SYSTEM": invoice.source.value if invoice.source else None,
                }.items() if v},
            )
            try:
                workflow = await approval_engine.create_workflow(
                    db, organization_id=invoice.organization_id, invoice_id=invoice.id, context=approval_context,
                )
            except approval_engine.ApprovalWorkflowEngineError as exc:
                # A genuine RULE_CONFLICT (two equally-specific rules tie) —
                # same "never guess" principle Phase 3/8 already established.
                # Routed to REVIEW_REQUIRED (a human resolves the rule
                # conflict via the Rules admin screen) rather than either
                # silently picking a rule or silently letting the invoice
                # through to READY_TO_POST unchecked.
                decision = ApInvoiceDecision.REVIEW_REQUIRED
                invoice.status = ApInvoiceStatus.REVIEW_REQUIRED
                invoice.final_decision = decision
                reasons.append({"rule_id": None, "category": "APPROVAL_RULE_CONFLICT", "result": "FAIL", "message": str(exc)})
                invoice.decision_reasons = reasons
                await db.commit()
                await db.refresh(invoice)
                if doc is not None:
                    from app.services import review_queue_service
                    await review_queue_service.create_from_invoice_validation(db, doc, str(exc))
            else:
                # PAY16 — create_workflow() is now idempotent (its own
                # module docstring): a second call for an invoice that
                # already has a workflow returns that EXISTING workflow
                # rather than creating a new one, and an existing workflow
                # can already be terminal (APPROVED) by the time it's
                # handed back here. Only reset the invoice to APPROVING
                # while a real decision is still outstanding — regressing
                # an already-APPROVED invoice back to APPROVING would
                # undo the READY_TO_POST/POSTED state
                # _maybe_complete_workflow() already committed, silently
                # reopening a question that's already answered.
                if workflow is not None and workflow.status in (ApprovalWorkflowStatus.PENDING, ApprovalWorkflowStatus.IN_PROGRESS):
                    # A rule matched and claimed this invoice for a real
                    # approval/peer-review workflow — the extraction/
                    # validation decision itself is still accurate
                    # (final_decision stays STRAIGHT_THROUGH), but the
                    # invoice may NOT go to READY_TO_POST until that
                    # workflow's own APPROVED/REJECTED outcome says so
                    # (see approval_workflow_engine_service._maybe_complete_
                    # workflow / act_on_approval_step, and the mirroring
                    # gate in invoice_erp_service.post_invoice()).
                    invoice.status = ApInvoiceStatus.APPROVING
                    await db.commit()
                    await db.refresh(invoice)
                # PAY22 — create_workflow() now always returns a real
                # ApprovalWorkflow (AUTO_APPROVED when no rule matched, or
                # a matched rule needed neither gate — see that function's
                # own docstring/_create_auto_approved for why), so
                # `workflow is None` no longer happens here in practice.
                # AUTO_APPROVED isn't in the PENDING/IN_PROGRESS set above,
                # so this branch is simply skipped for it — invoice stays
                # READY_TO_POST exactly as before this phase; only the
                # audit trail changed, not the posting gate.
                # workflow already APPROVED -> its own completion already
                # synced invoice.status; never regress it here.

    return decision
