"""
INV19 — automated acceptance tests for the Invoice Intelligence module,
against a real (isolated) SQLite DB via the actual service-layer
functions — no mocking of the code under test. Same convention as
SB33's test_billing_platform.py: real ORM models, real service
functions, the only test double is at the genuine network boundary (the
Oracle Fusion ERP Cloud REST API this build has never had a live tenant
to call — see tests/mock_erp_adapter.py and connectors/application/
oracle_erp.py's own docstring).

Scope, matching the build's own phase table:
  1-3   intake/materialization (INV5) + business-duplicate detection
  4-11  validation rule categories (INV6) — SCHEMA/ARITHMETIC/REFERENCE/PO/DUPLICATE/POLICY
  12-15 decision engine (INV7) — all four outcomes
  16-24 ERP posting (INV9/INV18) — idempotency, retry classification,
        circuit breaker, dead-lettering, double-post guard, dry-run preview
  25-27 reconciliation (INV10)
  28-29 tenant isolation
  30-32 RBAC (role tier + module license, INV1's registration checklist)

HTTP-layer behavior (route auth wiring, request/response shapes) was
already live-verified end to end during INV8/INV9/INV15-18 via curl and
the browser — this suite exercises the service layer so that coverage
runs on every future change, not to redo the HTTP-layer verification a
second way (same scope note as SB33).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from app.core.deps import require_action
from app.models.connection import Connection, ConnectionCategory, ConnectionEnvironment, ConnectionType
from app.models.document import Document, DocumentStatus
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceDecision, ApInvoiceStatus
from app.models.invoice_intelligence.erp import ApInvoiceErpStatus, ApInvoiceReconciliationStatus
from app.models.invoice_intelligence.rules import ApInvoiceRuleCategory, ApInvoiceRuleResult, ApInvoiceRuleSeverity
from app.models.review_queue import ReviewQueueItem
from app.models.subscription import SubscriptionStatus
from app.models.user import User, UserRole
from app.services import connection_service
from app.services.invoice import (
    invoice_decision_service,
    invoice_erp_service,
    invoice_processing_service,
    invoice_reconciliation_service,
    invoice_validation_service,
)

from tests.mock_erp_adapter import MockErpAdapter

INVOICE_MODULE = "INVOICE_INTELLIGENCE"


def _future(days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


# ── Fixtures / helpers ───────────────────────────────────────────────

def _field(name: str, value, *, missing: bool = False, bbox: bool = True) -> dict:
    is_missing = missing or value is None
    return {
        "name": name, "value": value, "type": "text", "missing": is_missing,
        "bbox": ({"page": 1, "x": 10, "y": 10, "w": 40, "h": 10} if (bbox and not is_missing) else None),
        # review_queue_service._pick_primary_issue() (reused unmodified —
        # see invoice_decision_service.decide()'s REVIEW_REQUIRED branch)
        # reads these two keys off every field, same shape real
        # extraction_service output always carries.
        "confidence": 40.0 if is_missing else 95.0, "validation_status": "FLAGGED" if is_missing else "OK",
    }


def _invoice_fields(
    *, invoice_number="INV-HAPPY-1001", vendor_name="Acme Corp", invoice_date="2026-01-15",
    po_number="PO-1234", subtotal=1000.0, tax=100.0, discount=0.0, freight=0.0, total=1100.0,
    currency="USD", payment_terms="NET30", bbox=True, missing=None, include=None,
) -> list[dict]:
    missing = missing or set()
    values = {
        "invoice_number": invoice_number, "vendor_name": vendor_name, "invoice_date": invoice_date,
        "po_number": po_number, "currency": currency, "payment_terms": payment_terms,
        "subtotal": subtotal, "tax_amount": tax, "discount_amount": discount,
        "shipping_amount": freight, "total_amount": total,
    }
    if include is not None:
        values = {k: v for k, v in values.items() if k in include}
    return [_field(name, value, missing=name in missing, bbox=bbox) for name, value in values.items()]


async def _make_document(db, org, *, fields, extraction_percent=99.0, filename="invoice.pdf") -> Document:
    doc = Document(
        organization_id=org.id, filename=filename, storage_path=f"/tmp/{filename}",
        status=DocumentStatus.COMPLETED, extraction_percent=extraction_percent,
        extraction_result={"fields": fields, "document_type": "INVOICE"},
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _run_pipeline(db, org, *, fields, extraction_percent=99.0):
    """materialize -> seed default rules (idempotent) -> evaluate -> decide
    — the exact same chain document_service/review_queue_service trigger
    in production (see invoice_decision_service.decide's own docstring)."""
    doc = await _make_document(db, org, fields=fields, extraction_percent=extraction_percent)
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)
    decision = await invoice_decision_service.decide(db, invoice, results)
    await db.refresh(invoice)
    return doc, invoice, results, decision


async def _post_ready_invoice(db, org, *, invoice_number):
    """A clean, fully-formed invoice run through materialize/evaluate/
    decide — ready for ERP posting tests, independent of the decision
    outcome (posting itself only cares about erp_status/final_decision)."""
    fields = _invoice_fields(invoice_number=invoice_number)
    _, invoice, _, _ = await _run_pipeline(db, org, fields=fields)
    return invoice


async def _settle_erp(db, invoice, timeout: float = 3.0):
    """Polls the invoice (via a fresh refresh, since the background
    posting task commits from its OWN session — see invoice_erp_service.
    _post_in_background's docstring) until it leaves PENDING/RETRYING."""
    elapsed = 0.0
    interval = 0.02
    while elapsed < timeout:
        await db.refresh(invoice)
        if invoice.erp_status not in (ApInvoiceErpStatus.PENDING, ApInvoiceErpStatus.RETRYING):
            return
        await asyncio.sleep(interval)
        elapsed += interval
    raise AssertionError(f"ERP post did not settle within {timeout}s — still {invoice.erp_status}")


@pytest_asyncio.fixture
async def erp_conn(db, make_org):
    org = await make_org()
    conn = Connection(
        organization_id=org.id, name="Mock Oracle ERP", category=ConnectionCategory.APPLICATION,
        connection_type=ConnectionType.ORACLE_ERP_CLOUD, environment=ConnectionEnvironment.DEVELOPMENT,
        config={"host": "http://mock-erp.test", "api_version": "11.13.18.05", "auth_type": "Basic Auth"},
        encrypted_secrets={},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return org, conn


@pytest.fixture
def mock_erp(monkeypatch):
    adapter = MockErpAdapter()
    monkeypatch.setattr(connection_service, "build_connector", lambda conn: adapter)
    # Real exponential-backoff/circuit-breaker math, just fast enough for a test run.
    monkeypatch.setattr(invoice_erp_service, "BASE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(invoice_erp_service, "MAX_DELAY_SECONDS", 0.05)
    return adapter


async def _run_pipeline_reusing_doc(db, org, *, fields, filename):
    """Same chain as _run_pipeline(), for call sites that already created
    their own Document (e.g. a duplicate-detection scenario's second doc)."""
    doc = await _make_document(db, org, fields=fields, filename=filename)
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)
    decision = await invoice_decision_service.decide(db, invoice, results)
    await db.refresh(invoice)
    return doc, invoice, results, decision


# ── 1-3: intake / materialization + business-duplicate detection (INV5) ──

@pytest.mark.asyncio
async def test_materialize_invoice_normalizes_amounts_dates_and_vendor(db, make_org):
    org = await make_org()
    doc = await _make_document(db, org, fields=_invoice_fields())
    invoice = await invoice_processing_service.materialize_invoice(db, doc)

    assert invoice.total == 1100.0
    assert invoice.subtotal == 1000.0
    assert invoice.currency == "USD"
    assert invoice.invoice_date is not None and invoice.invoice_date.year == 2026
    assert invoice.vendor_id is not None  # auto-resolved/created reference vendor
    assert invoice.document_id == doc.id


@pytest.mark.asyncio
async def test_materialize_invoice_is_idempotent_by_document_id(db, make_org):
    org = await make_org()
    doc = await _make_document(db, org, fields=_invoice_fields())
    first = await invoice_processing_service.materialize_invoice(db, doc)
    second = await invoice_processing_service.materialize_invoice(db, doc)

    assert first.id == second.id
    result = await db.execute(select(ApInvoice).where(ApInvoice.document_id == doc.id))
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_materialize_invoice_raises_without_extraction_result(db, make_org):
    org = await make_org()
    doc = Document(organization_id=org.id, filename="blank.pdf", storage_path="/tmp/blank.pdf")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(invoice_processing_service.InvoiceProcessingError):
        await invoice_processing_service.materialize_invoice(db, doc)


@pytest.mark.asyncio
async def test_duplicate_detection_blocks_exact_four_field_match(db, make_org):
    org = await make_org()
    doc1 = await _make_document(db, org, fields=_invoice_fields(invoice_number="DUP-001"), filename="a.pdf")
    inv1 = await invoice_processing_service.materialize_invoice(db, doc1)

    doc2 = await _make_document(db, org, fields=_invoice_fields(invoice_number="DUP-001"), filename="b.pdf")
    inv2 = await invoice_processing_service.materialize_invoice(db, doc2)

    assert inv2.duplicate_check_result == "BLOCKED"
    assert inv2.duplicate_of_invoice_id == inv1.id


@pytest.mark.asyncio
async def test_duplicate_detection_warns_on_partial_match(db, make_org):
    org = await make_org()
    doc1 = await _make_document(db, org, fields=_invoice_fields(invoice_number="DUP-002", total=500.0), filename="a.pdf")
    await invoice_processing_service.materialize_invoice(db, doc1)

    # same vendor + number, DIFFERENT total — a real amount discrepancy, not a clean re-send.
    doc2 = await _make_document(db, org, fields=_invoice_fields(invoice_number="DUP-002", total=650.0), filename="b.pdf")
    inv2 = await invoice_processing_service.materialize_invoice(db, doc2)

    assert inv2.duplicate_check_result == "WARN"


@pytest.mark.asyncio
async def test_duplicate_detection_none_for_genuinely_distinct_invoices(db, make_org):
    org = await make_org()
    doc1 = await _make_document(db, org, fields=_invoice_fields(invoice_number="UNIQUE-1", vendor_name="Vendor A"), filename="a.pdf")
    await invoice_processing_service.materialize_invoice(db, doc1)

    doc2 = await _make_document(db, org, fields=_invoice_fields(invoice_number="UNIQUE-2", vendor_name="Vendor B"), filename="b.pdf")
    inv2 = await invoice_processing_service.materialize_invoice(db, doc2)

    assert inv2.duplicate_check_result == "NONE"
    assert inv2.duplicate_of_invoice_id is None


# ── 4-11: validation rule categories (INV6) ───────────────────────────

@pytest.mark.asyncio
async def test_validation_schema_fails_on_missing_required_field(db, make_org):
    org = await make_org()
    fields = _invoice_fields(missing={"invoice_number"})
    doc = await _make_document(db, org, fields=fields)
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)

    schema = next(r for r in results if r.category == ApInvoiceRuleCategory.SCHEMA)
    assert schema.result == ApInvoiceRuleResult.FAIL
    assert "invoice_number" in schema.details.get("missing_fields", [])


@pytest.mark.asyncio
async def test_validation_arithmetic_fails_on_mismatched_total(db, make_org):
    org = await make_org()
    fields = _invoice_fields(subtotal=1000.0, tax=100.0, discount=0.0, freight=0.0, total=5000.0)
    doc = await _make_document(db, org, fields=fields)
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)

    arithmetic = next(r for r in results if r.category == ApInvoiceRuleCategory.ARITHMETIC)
    assert arithmetic.result == ApInvoiceRuleResult.FAIL


@pytest.mark.asyncio
async def test_validation_arithmetic_warns_when_a_variable_is_unevaluable(db, make_org):
    org = await make_org()
    # discount genuinely never extracted (not just zero) — the rule can't
    # evaluate its own expression, which must be a WARNING, never a FAIL.
    fields = _invoice_fields(include={"invoice_number", "vendor_name", "invoice_date", "subtotal", "tax_amount", "shipping_amount", "total_amount"})
    doc = await _make_document(db, org, fields=fields)
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    assert invoice.discount is None
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)

    arithmetic = next(r for r in results if r.category == ApInvoiceRuleCategory.ARITHMETIC)
    assert arithmetic.result == ApInvoiceRuleResult.WARNING
    assert "discount" in arithmetic.message


@pytest.mark.asyncio
async def test_validation_reference_passes_for_an_extracted_vendor(db, make_org):
    org = await make_org()
    doc = await _make_document(db, org, fields=_invoice_fields(vendor_name="Globex Corp"))
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)

    reference = next(r for r in results if r.category == ApInvoiceRuleCategory.REFERENCE)
    assert reference.result == ApInvoiceRuleResult.PASS


@pytest.mark.asyncio
async def test_validation_reference_warns_when_no_vendor_name_extracted(db, make_org):
    org = await make_org()
    fields = _invoice_fields(missing={"vendor_name"})
    doc = await _make_document(db, org, fields=fields)
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)

    reference = next(r for r in results if r.category == ApInvoiceRuleCategory.REFERENCE)
    assert reference.result == ApInvoiceRuleResult.WARNING


@pytest.mark.asyncio
async def test_validation_po_format_fails_on_bad_pattern(db, make_org):
    org = await make_org()
    doc = await _make_document(db, org, fields=_invoice_fields(po_number="!! not a po !!"))
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)

    po = next(r for r in results if r.category == ApInvoiceRuleCategory.PO)
    assert po.result == ApInvoiceRuleResult.FAIL


@pytest.mark.asyncio
async def test_validation_po_passes_when_absent(db, make_org):
    org = await make_org()
    fields = _invoice_fields(missing={"po_number"})
    doc = await _make_document(db, org, fields=fields)
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)

    po = next(r for r in results if r.category == ApInvoiceRuleCategory.PO)
    assert po.result == ApInvoiceRuleResult.PASS


@pytest.mark.asyncio
async def test_validation_duplicate_rule_reflects_blocked_status(db, make_org):
    org = await make_org()
    doc1 = await _make_document(db, org, fields=_invoice_fields(invoice_number="DUP-RULE-1"), filename="a.pdf")
    await invoice_processing_service.materialize_invoice(db, doc1)
    doc2 = await _make_document(db, org, fields=_invoice_fields(invoice_number="DUP-RULE-1"), filename="b.pdf")
    invoice2 = await invoice_processing_service.materialize_invoice(db, doc2)

    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice2)

    duplicate = next(r for r in results if r.category == ApInvoiceRuleCategory.DUPLICATE)
    assert duplicate.result == ApInvoiceRuleResult.FAIL


@pytest.mark.asyncio
async def test_validation_policy_fails_outside_tax_band(db, make_org):
    org = await make_org()
    # tax = 60% of subtotal — well outside the default 0-30% band.
    doc = await _make_document(db, org, fields=_invoice_fields(subtotal=1000.0, tax=600.0, total=1600.0))
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)

    policy = next(r for r in results if r.category == ApInvoiceRuleCategory.POLICY)
    assert policy.result == ApInvoiceRuleResult.FAIL


@pytest.mark.asyncio
async def test_validation_policy_passes_within_tax_band(db, make_org):
    org = await make_org()
    doc = await _make_document(db, org, fields=_invoice_fields(subtotal=1000.0, tax=100.0, total=1100.0))
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    await invoice_validation_service.seed_default_rules(db, org.id)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)

    policy = next(r for r in results if r.category == ApInvoiceRuleCategory.POLICY)
    assert policy.result == ApInvoiceRuleResult.PASS


# ── 12-15: decision engine (INV7) — all four outcomes ─────────────────

@pytest.mark.asyncio
async def test_decision_straight_through_when_every_score_clears_the_bar(db, make_org):
    org = await make_org()
    fields = _invoice_fields(missing={"po_number"})  # PO absent -> PASS, never drags the score down
    _, invoice, _, decision = await _run_pipeline(db, org, fields=fields, extraction_percent=99.0)

    assert decision == ApInvoiceDecision.STRAIGHT_THROUGH
    assert invoice.status == ApInvoiceStatus.READY_TO_POST
    assert invoice.evidence_quality == 100.0


@pytest.mark.asyncio
async def test_decision_review_required_on_exception_rule_failure_and_opens_review_item(db, make_org):
    org = await make_org()
    fields = _invoice_fields(missing={"invoice_number"})  # SCHEMA is EXCEPTION-severity by default
    doc, invoice, _, decision = await _run_pipeline(db, org, fields=fields)

    assert decision == ApInvoiceDecision.REVIEW_REQUIRED
    assert invoice.status == ApInvoiceStatus.REVIEW_REQUIRED
    result = await db.execute(select(ReviewQueueItem).where(ReviewQueueItem.document_id == doc.id))
    assert result.scalars().first() is not None  # real Review Queue item, reused infra (INV7 docstring)


@pytest.mark.asyncio
async def test_decision_reject_on_blocked_duplicate(db, make_org):
    org = await make_org()
    doc1 = await _make_document(db, org, fields=_invoice_fields(invoice_number="DUP-DECIDE-1"), filename="a.pdf")
    await invoice_processing_service.materialize_invoice(db, doc1)

    fields2 = _invoice_fields(invoice_number="DUP-DECIDE-1")
    _, invoice2, results2, decision2 = await _run_pipeline_reusing_doc(db, org, fields=fields2, filename="b.pdf")

    assert decision2 == ApInvoiceDecision.REJECT
    assert invoice2.status == ApInvoiceStatus.REJECTED


@pytest.mark.asyncio
async def test_decision_retry_when_no_confidence_score_is_computable(db, make_org):
    org = await make_org()
    # every field genuinely missing, no extraction confidence, no rules
    # seeded -> every one of the five scores is None, not a fabricated low number.
    fields = [_field("invoice_number", None, missing=True, bbox=False)]
    doc = await _make_document(db, org, fields=fields, extraction_percent=None)
    invoice = await invoice_processing_service.materialize_invoice(db, doc)
    results = await invoice_validation_service.evaluate_invoice(db, invoice)  # no rules seeded -> []
    decision = await invoice_decision_service.decide(db, invoice, results)
    await db.refresh(invoice)

    assert decision == ApInvoiceDecision.RETRY
    assert invoice.status == ApInvoiceStatus.EXCEPTION
    assert invoice.evidence_quality is None


# ── 16-24: ERP posting — idempotency, retry, circuit breaker (INV9/INV18) ──

@pytest.mark.asyncio
async def test_erp_happy_path_succeeds_on_first_attempt(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-HAPPY-1")

    txn = await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)
    assert txn.status == ApInvoiceErpStatus.PENDING
    await _settle_erp(db, invoice)

    assert invoice.erp_status == ApInvoiceErpStatus.SUCCEEDED
    assert invoice.erp_transaction_id is not None
    assert len(mock_erp.calls) == 1
    assert mock_erp.calls[0]["idempotency_key"] == f"ap-invoice-{invoice.id}-erp-{conn.id}"


@pytest.mark.asyncio
async def test_erp_retry_recovers_after_transient_failures_same_idempotency_key(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-RETRY-1")

    await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)
    await _settle_erp(db, invoice)

    assert invoice.erp_status == ApInvoiceErpStatus.SUCCEEDED
    assert len(mock_erp.calls) == 3  # 2 real transient failures + the successful 3rd attempt
    keys = {c["idempotency_key"] for c in mock_erp.calls}
    assert keys == {f"ap-invoice-{invoice.id}-erp-{conn.id}"}  # identical across every attempt

    txns = sorted(await invoice_erp_service.list_transactions(db, invoice.id), key=lambda t: t.attempt_number)
    assert [t.status for t in txns] == [ApInvoiceErpStatus.RETRYING, ApInvoiceErpStatus.RETRYING, ApInvoiceErpStatus.SUCCEEDED]


@pytest.mark.asyncio
async def test_erp_permanent_rejection_fails_without_retrying(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-REJECT-1")

    await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)
    await _settle_erp(db, invoice)

    assert invoice.erp_status == ApInvoiceErpStatus.FAILED  # not DEAD_LETTER — a business rejection, not a retryable one
    assert len(mock_erp.calls) == 1  # no retry attempted for a genuine 4xx


@pytest.mark.asyncio
async def test_erp_dead_letters_after_max_attempts_and_opens_circuit(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-DEADLETTER-1")

    await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)
    await _settle_erp(db, invoice)

    assert invoice.erp_status == ApInvoiceErpStatus.DEAD_LETTER
    assert len(mock_erp.calls) == invoice_erp_service.MAX_ATTEMPTS
    assert invoice_erp_service.circuit_status(conn.id)["state"] == "OPEN"


@pytest.mark.asyncio
async def test_erp_circuit_breaker_blocks_further_posts_while_open(db, mock_erp, erp_conn):
    org, conn = erp_conn
    dead_lettered = await _post_ready_invoice(db, org, invoice_number="INV-DEADLETTER-2")
    await invoice_erp_service.post_invoice(db, dead_lettered, erp_connection_id=conn.id)
    await _settle_erp(db, dead_lettered)
    calls_before = len(mock_erp.calls)
    assert invoice_erp_service.circuit_status(conn.id)["state"] == "OPEN"

    # A perfectly postable invoice, on the SAME now-open connection.
    blocked = await _post_ready_invoice(db, org, invoice_number="INV-HAPPY-blocked")
    await invoice_erp_service.post_invoice(db, blocked, erp_connection_id=conn.id)
    await _settle_erp(db, blocked)

    assert blocked.erp_status == ApInvoiceErpStatus.FAILED
    assert len(mock_erp.calls) == calls_before  # breaker blocked it before any real call reached the ERP
    txns = await invoice_erp_service.list_transactions(db, blocked.id)
    assert txns[0].error_code == "CIRCUIT_OPEN"


@pytest.mark.asyncio
async def test_erp_circuit_breaker_recovers_after_cooldown(db, monkeypatch, mock_erp, erp_conn):
    monkeypatch.setattr(invoice_erp_service, "CIRCUIT_COOLDOWN_SECONDS", 0.05)
    org, conn = erp_conn
    dead_lettered = await _post_ready_invoice(db, org, invoice_number="INV-DEADLETTER-3")
    await invoice_erp_service.post_invoice(db, dead_lettered, erp_connection_id=conn.id)
    await _settle_erp(db, dead_lettered)
    assert invoice_erp_service.circuit_status(conn.id)["state"] == "OPEN"

    await asyncio.sleep(0.15)  # let the (patched-short) cooldown elapse

    probe = await _post_ready_invoice(db, org, invoice_number="INV-HAPPY-probe")
    await invoice_erp_service.post_invoice(db, probe, erp_connection_id=conn.id)
    await _settle_erp(db, probe)

    assert probe.erp_status == ApInvoiceErpStatus.SUCCEEDED
    assert invoice_erp_service.circuit_status(conn.id)["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_erp_double_post_guard_blocks_a_concurrent_second_post(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-HAPPY-guard")

    await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)
    assert invoice.erp_status == ApInvoiceErpStatus.PENDING  # set synchronously before the background task even starts

    with pytest.raises(invoice_erp_service.InvoiceErpError, match="already has an ERP post in progress"):
        await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)

    await _settle_erp(db, invoice)
    assert invoice.erp_status == ApInvoiceErpStatus.SUCCEEDED
    assert len(mock_erp.calls) == 1  # the blocked second call never reached the ERP


@pytest.mark.asyncio
async def test_erp_post_after_success_is_rejected(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-HAPPY-again")
    await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)
    await _settle_erp(db, invoice)

    with pytest.raises(invoice_erp_service.InvoiceErpError, match="already been posted"):
        await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)


@pytest.mark.asyncio
async def test_erp_preview_matches_what_a_real_post_actually_sends(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-HAPPY-preview")

    preview = await invoice_erp_service.preview_erp_payload(db, invoice, erp_connection_id=conn.id)
    assert preview["idempotency_key"] == f"ap-invoice-{invoice.id}-erp-{conn.id}"
    assert mock_erp.calls == []  # genuinely no network call for a dry run

    await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)
    await _settle_erp(db, invoice)

    assert mock_erp.calls[0]["payload"]["InvoiceNumber"] == preview["payload"]["InvoiceNumber"]
    assert mock_erp.calls[0]["payload"]["InvoiceAmount"] == preview["payload"]["InvoiceAmount"]


@pytest.mark.asyncio
async def test_erp_preview_surfaces_missing_required_field(db, mock_erp, erp_conn):
    org, conn = erp_conn
    fields = _invoice_fields(include={"invoice_number", "vendor_name", "invoice_date", "subtotal", "tax_amount", "total_amount"})
    doc = await _make_document(db, org, fields=fields)
    invoice = await invoice_processing_service.materialize_invoice(db, doc)

    with pytest.raises(invoice_erp_service.InvoiceErpError, match="currency"):
        await invoice_erp_service.preview_erp_payload(db, invoice, erp_connection_id=conn.id)


# ── 25-27: reconciliation (INV10) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_reconciliation_reconciled_when_erp_agrees(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-HAPPY-recon")
    await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)
    await _settle_erp(db, invoice)

    row = await invoice_reconciliation_service.reconcile_invoice(db, invoice)
    assert row.status == ApInvoiceReconciliationStatus.RECONCILED
    assert all(c["result"] in ("MATCH", "INFO") for c in row.field_comparisons)


@pytest.mark.asyncio
async def test_reconciliation_mismatch_when_erp_value_diverges(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-HAPPY-mismatch")
    await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=conn.id)
    await _settle_erp(db, invoice)

    mock_erp.status_overrides[invoice.erp_transaction_id] = {"InvoiceAmount": 999999.0}
    row = await invoice_reconciliation_service.reconcile_invoice(db, invoice)

    assert row.status == ApInvoiceReconciliationStatus.MISMATCH
    mismatches = [c for c in row.field_comparisons if c["result"] == "MISMATCH"]
    assert any(c["field"] == "InvoiceAmount" for c in mismatches)


@pytest.mark.asyncio
async def test_reconciliation_before_any_post_raises(db, mock_erp, erp_conn):
    org, conn = erp_conn
    invoice = await _post_ready_invoice(db, org, invoice_number="INV-HAPPY-unposted")

    with pytest.raises(invoice_reconciliation_service.InvoiceReconciliationError):
        await invoice_reconciliation_service.reconcile_invoice(db, invoice)


# ── 28-29: tenant isolation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_org_specific_rule_override_never_leaks_to_other_orgs(db, make_org):
    org1 = await make_org()
    org2 = await make_org()
    await invoice_validation_service.seed_default_rules(db, org1.id)
    await invoice_validation_service.seed_default_rules(db, org2.id)

    await invoice_validation_service.publish_rule(
        db, org1.id, name="Tax Rate Within Expected Range", category=ApInvoiceRuleCategory.POLICY,
        severity=ApInvoiceRuleSeverity.WARNING, config={"numerator": "tax", "denominator": "subtotal", "min_pct": 0, "max_pct": 5},
        message="Tightened for org1 only.", enabled=True, created_by=None,
    )

    org1_rules = await invoice_validation_service.get_current_rules(db, org1.id)
    org2_rules = await invoice_validation_service.get_current_rules(db, org2.id)
    org1_tax = next(r for r in org1_rules if r.name == "Tax Rate Within Expected Range")
    org2_tax = next(r for r in org2_rules if r.name == "Tax Rate Within Expected Range")

    assert org1_tax.organization_id == org1.id and org1_tax.config["max_pct"] == 5
    # org2 seeded its OWN independent copy of the defaults (seed_default_
    # rules() is per-organization, not a shared platform row here) — the
    # real isolation guarantee is that org1's override never touches it.
    assert org2_tax.organization_id == org2.id and org2_tax.config["max_pct"] == 30 and org2_tax.version == 1


@pytest.mark.asyncio
async def test_invoices_are_scoped_to_their_own_organization(db, make_org):
    org1 = await make_org()
    org2 = await make_org()
    doc1 = await _make_document(db, org1, fields=_invoice_fields(invoice_number="INV-ORG1-1"), filename="a.pdf")
    doc2 = await _make_document(db, org2, fields=_invoice_fields(invoice_number="INV-ORG2-1"), filename="b.pdf")
    inv1 = await invoice_processing_service.materialize_invoice(db, doc1)
    inv2 = await invoice_processing_service.materialize_invoice(db, doc2)

    result = await db.execute(select(ApInvoice).where(ApInvoice.organization_id == org1.id))
    org1_invoice_ids = {i.id for i in result.scalars().all()}
    assert org1_invoice_ids == {inv1.id}
    assert inv2.id not in org1_invoice_ids


# ── 30-32: RBAC (role tier + module license) ───────────────────────────

@pytest.mark.asyncio
async def test_rbac_role_without_execute_is_denied_regardless_of_module():
    checker = require_action("EXECUTE", module=INVOICE_MODULE)
    viewer = User(role=UserRole.VIEWER, is_active=True)  # ROLE_ACTIONS[VIEWER] == {"VIEW"} only

    with pytest.raises(HTTPException) as exc:
        await checker(user=viewer, org=None, db=None)  # role check short-circuits before any DB/module check
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_rbac_org_without_invoice_intelligence_license_is_denied(db, make_org):
    checker = require_action("EXECUTE", module=INVOICE_MODULE)
    org = await make_org()  # no subscription at all
    admin = User(role=UserRole.ORG_ADMIN, organization_id=org.id, is_active=True)

    with pytest.raises(HTTPException) as exc:
        await checker(user=admin, org=org, db=db)
    assert exc.value.status_code == 403
    assert "not licensed" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_rbac_granted_when_role_and_module_license_both_ok(db, make_org, make_subscription):
    checker = require_action("EXECUTE", module=INVOICE_MODULE)
    org = await make_org()
    await make_subscription(organization_id=org.id, modules=[INVOICE_MODULE], status=SubscriptionStatus.ACTIVE, end_date=_future(30))
    admin = User(role=UserRole.ORG_ADMIN, organization_id=org.id, is_active=True)

    result = await checker(user=admin, org=org, db=db)
    assert result is admin
