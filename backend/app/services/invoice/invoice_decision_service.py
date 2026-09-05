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
     OCR actually got right but this taxonomy mismapped).
  3. Every computed confidence score (ignoring None ones) is >= the
     "straight-through" threshold AND at least one score was actually
     computed -> STRAIGHT_THROUGH.
  4. No confidence score could be computed at all (every score is None —
     e.g. extraction produced literally nothing usable) -> RETRY, the
     one case a fresh extraction attempt (not a human) is the right next
     step.
  5. Otherwise (some signal exists but below threshold, or below the
     lower REVIEW_REQUIRED->REJECT floor) -> REVIEW_REQUIRED, the safe
     default whenever this build isn't confident enough to either
     auto-post or auto-reject.
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

    invoice.final_decision = decision
    invoice.decision_reasons = reasons
    await db.commit()
    await db.refresh(invoice)

    if decision == ApInvoiceDecision.REVIEW_REQUIRED and doc is not None:
        from app.services import review_queue_service
        await review_queue_service.create_from_document(db, doc)

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

    return decision
