"""
invoice_reconciliation_service.py — field-by-field platform-vs-ERP
compare, run after a successful post (INV10). Auto-triggered by
invoice_erp_service.py right after an ERP posting attempt SUCCEEDS
(reused call site, not a second separate reconciliation-scheduling
mechanism) — also callable on demand via POST /invoices/{id}/reconciliation
for a manual re-check.

Real GET against the ERP (invoice_erp_service.get_live_status(), INV9)
compared against what this platform would send RIGHT NOW — recomputed
from the CURRENT mapping + CURRENT invoice state via the same
build_erp_payload() posting itself uses, not the possibly-stale
request_json captured at post time. This deliberately catches drift from
a correction made *after* posting (see invoice_inbox_service.
apply_corrections, INV8) — comparing against the original request would
miss exactly that case.

Scope: header fields only (the mapped canonical -> ERP fields this
platform actually controls). Oracle's own workflow-state fields
(ValidationStatus/ApprovalStatus/PaymentStatus) have no platform-side
value to compare against — they're surfaced separately as informational
ERP state, never forced into a MATCH/MISMATCH verdict they can't
honestly produce. Line-item reconciliation is out of scope for this
phase — the model's own ApInvoiceReconciliation row is header-scoped
(one row per reconciliation run, not per line), disclosed here rather
than silently assumed to cover lines too.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceLine
from app.models.invoice_intelligence.erp import ApInvoiceErpStatus, ApInvoiceErpTransaction, ApInvoiceReconciliation, ApInvoiceReconciliationStatus
from app.services.invoice import invoice_erp_mapping_service, invoice_erp_service

_COMPARE_TOLERANCE = 0.01
# Oracle's own workflow-state fields — the ERP's response includes them,
# but this platform never posted them (they're not in our payload), so
# they can never be part of a MATCH/MISMATCH field comparison. Recorded
# separately as informational ERP state instead.
_STATUS_ONLY_FIELDS = {"ValidationStatus", "ApprovalStatus", "PaymentStatus", "InvoiceId"}


class InvoiceReconciliationError(Exception):
    pass


def _normalize(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower()
    return value


def _values_match(platform_value, erp_value) -> bool:
    if platform_value is None and erp_value is None:
        return True
    if isinstance(platform_value, (int, float)) and not isinstance(platform_value, bool):
        try:
            return abs(float(platform_value) - float(erp_value)) < _COMPARE_TOLERANCE
        except (TypeError, ValueError):
            return False
    # Dates are compared as the same plain string this platform actually
    # sent (build_erp_payload's date_format transform already turned the
    # real datetime into "YYYY-MM-DD" before it ever reached the ERP) —
    # never re-parsed into a datetime here, which would risk inventing a
    # format assumption the ERP's own response doesn't document.
    return _normalize(platform_value) == _normalize(erp_value)


async def _latest_succeeded_transaction_id(db: AsyncSession, invoice_id: int) -> int | None:
    result = await db.execute(
        select(ApInvoiceErpTransaction.id)
        .where(ApInvoiceErpTransaction.invoice_id == invoice_id, ApInvoiceErpTransaction.status == ApInvoiceErpStatus.SUCCEEDED)
        .order_by(ApInvoiceErpTransaction.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def reconcile_invoice(db: AsyncSession, invoice: ApInvoice) -> ApInvoiceReconciliation:
    """Raises InvoiceReconciliationError only for a genuine caller
    precondition failure (never posted yet). A real GET failure or a
    genuine field mismatch is NOT an exception — both are legitimate,
    recorded outcomes (FAILED / MISMATCH rows), same "every branch is a
    real result" convention as invoice_erp_service."""
    if not invoice.erp_transaction_id:
        raise InvoiceReconciliationError("This invoice has no ERP transaction id yet — it hasn't been successfully posted.")

    erp_transaction_id = await _latest_succeeded_transaction_id(db, invoice.id)

    try:
        erp_response = await invoice_erp_service.get_live_status(db, invoice)
    except invoice_erp_service.InvoiceErpError as exc:
        row = ApInvoiceReconciliation(
            organization_id=invoice.organization_id, invoice_id=invoice.id, erp_transaction_id=erp_transaction_id,
            status=ApInvoiceReconciliationStatus.FAILED,
            field_comparisons=[{"field": "__erp_lookup__", "platform_value": None, "erp_value": str(exc)[:500], "result": "FAILED"}],
        )
        db.add(row)
        invoice.reconciliation_status = ApInvoiceReconciliationStatus.FAILED
        await db.commit()
        await db.refresh(row)
        return row

    lines_result = await db.execute(select(ApInvoiceLine).where(ApInvoiceLine.invoice_id == invoice.id).order_by(ApInvoiceLine.line_number.asc()))
    lines = list(lines_result.scalars().all())
    mapping_rows = await invoice_erp_mapping_service.get_current_mapping(db, invoice.organization_id, invoice.erp_connection_id)
    try:
        expected_payload = invoice_erp_mapping_service.build_erp_payload(invoice, lines, mapping_rows)
    except invoice_erp_mapping_service.InvoiceErpValidationError as exc:
        # The invoice was posted successfully once, but its data has
        # since changed (a correction removed a previously-required
        # field) — real, disclosed as a comparison failure rather than a
        # silently-skipped field.
        row = ApInvoiceReconciliation(
            organization_id=invoice.organization_id, invoice_id=invoice.id, erp_transaction_id=erp_transaction_id,
            status=ApInvoiceReconciliationStatus.FAILED,
            field_comparisons=[{"field": "__mapping__", "platform_value": None, "erp_value": str(exc)[:500], "result": "FAILED"}],
        )
        db.add(row)
        invoice.reconciliation_status = ApInvoiceReconciliationStatus.FAILED
        await db.commit()
        await db.refresh(row)
        return row

    comparisons = []
    for erp_field, platform_value in expected_payload.items():
        if erp_field == "invoiceLines" or erp_field in _STATUS_ONLY_FIELDS:
            continue
        erp_value = erp_response.get(erp_field)
        match = _values_match(platform_value, erp_value)
        comparisons.append({"field": erp_field, "platform_value": platform_value, "erp_value": erp_value, "result": "MATCH" if match else "MISMATCH"})

    for status_field in _STATUS_ONLY_FIELDS:
        if status_field in erp_response:
            comparisons.append({"field": status_field, "platform_value": None, "erp_value": erp_response.get(status_field), "result": "INFO"})

    overall = ApInvoiceReconciliationStatus.RECONCILED if all(c["result"] != "MISMATCH" for c in comparisons) else ApInvoiceReconciliationStatus.MISMATCH

    row = ApInvoiceReconciliation(
        organization_id=invoice.organization_id, invoice_id=invoice.id, erp_transaction_id=erp_transaction_id,
        status=overall, field_comparisons=comparisons,
    )
    db.add(row)
    invoice.reconciliation_status = overall
    await db.commit()
    await db.refresh(row)

    mismatched_fields = [c["field"] for c in comparisons if c["result"] == "MISMATCH"]
    if overall == ApInvoiceReconciliationStatus.MISMATCH:
        await _report_mismatch_incident(db, invoice=invoice, mismatched_fields=mismatched_fields)

    # INV13 — narrow lifecycle event, fires on every run (MATCH or
    # MISMATCH alike — "this reconciliation ran" is the event, not a
    # judgment on its outcome), never fails the real reconciliation.
    try:
        from app.models.invoice_intelligence.learning import ApInvoiceAuditEventType
        from app.services.invoice import invoice_learning_service
        await invoice_learning_service.record_event(
            db, invoice_id=invoice.id, organization_id=invoice.organization_id,
            event_type=ApInvoiceAuditEventType.RECONCILIATION_RUN,
            message=f"Reconciliation: {overall.value}.",
            details={"status": overall.value, "mismatched_fields": mismatched_fields},
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        pass

    return row


async def _report_mismatch_incident(db: AsyncSession, *, invoice: ApInvoice, mismatched_fields: list[str]) -> None:
    """INV12 — best-effort Incident + Alert on a genuine post-success
    field mismatch. Deliberately reuses AlertCondition.RECONCILIATION_
    MISMATCH (not a new invoice-specific condition) — see that enum
    member's own docstring for why. Same "never affect the real
    reconciliation outcome" convention as invoice_erp_service's dead-
    letter hook."""
    try:
        from app.models.incident import IncidentSeverity, IncidentSource
        from app.monitoring import alerts
        from app.services import incident_service

        label = invoice.invoice_number or f"invoice #{invoice.id}"
        fields_str = ", ".join(mismatched_fields) or "unknown field(s)"
        incident = await incident_service.report_incident(
            db, organization_id=invoice.organization_id, source=IncidentSource.INVOICE_RECONCILIATION.value,
            title=f"Reconciliation mismatch — {label}",
            description=f"Field(s) diverged from the ERP's own record after posting: {fields_str}.",
            severity=IncidentSeverity.MEDIUM.value, dataset_name=label,
        )
        fired = await alerts.evaluate_rules(db, organization_id=invoice.organization_id, ctx={
            "event": "RECONCILIATION_MISMATCH",
            "message": f"Invoice {label} — reconciliation mismatch on: {fields_str}.",
        })
        if fired:
            for event in fired:
                if event.incident_id is None:
                    event.incident_id = incident.id
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - monitoring/incident wiring must never fail a real reconciliation result
        import logging
        logging.getLogger(__name__).error("invoice_reconciliation_service mismatch incident hook failed for invoice %s: %s", invoice.id, exc)


async def list_reconciliations(db: AsyncSession, invoice_id: int) -> list[ApInvoiceReconciliation]:
    result = await db.execute(
        select(ApInvoiceReconciliation).where(ApInvoiceReconciliation.invoice_id == invoice_id).order_by(ApInvoiceReconciliation.created_at.desc())
    )
    return list(result.scalars().all())
