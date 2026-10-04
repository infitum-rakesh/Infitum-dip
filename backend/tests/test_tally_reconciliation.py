"""
Automated tests for TALLY-E: app/reconciliation/engine.py's ListRowSource +
tolerance_columns extension, and tally_reconciliation_service.py's
orchestration (voucher two-sided diff, ledger-balance check,
payment-voucher-existence check).
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.connection import Connection, ConnectionCategory, ConnectionType
from app.models.document import Document
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceSource
from app.models.review_queue import ReviewQueueItem, ReviewStatus, ReviewType
from app.models.tally_canonical import TallyCanonicalParty, TallyCanonicalVoucher, TallyVoucherDirection
from app.models.tally_reconciliation import TallyExceptionType, TallyReconciliationRunStatus
from app.reconciliation.engine import ListRowSource, diff_keys_and_fields
from app.services import tally_reconciliation_service as svc

OLD = datetime.now(timezone.utc) - timedelta(days=60)
RECENT = datetime.now(timezone.utc) - timedelta(days=2)


# ── Engine extension (ListRowSource + tolerance_columns) ────────────

def test_list_row_source_projects_only_requested_columns():
    src = ListRowSource([{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}])
    assert src.count() == 2
    assert set(src.get_columns()) == {"a", "b", "c"}
    chunks = list(src.iter_rows(["a", "c"], chunk_size=1))
    assert chunks == [[{"a": 1, "c": 3}], [{"a": 4, "c": 6}]]


def test_diff_keys_and_fields_tolerance_columns_suppresses_small_deltas():
    source = ListRowSource([{"k": "1", "amt": 100.00}])
    target = ListRowSource([{"k": "1", "amt": 100.01}])
    result = diff_keys_and_fields(
        source, target, key_columns=["k"], compare_columns=["amt"], tolerance_columns={"amt": 0.02},
    )
    assert result["mismatches"] == []
    assert result["matched_count"] == 1


def test_diff_keys_and_fields_tolerance_columns_still_flags_beyond_tolerance():
    source = ListRowSource([{"k": "1", "amt": 100.00}])
    target = ListRowSource([{"k": "1", "amt": 105.00}])
    result = diff_keys_and_fields(
        source, target, key_columns=["k"], compare_columns=["amt"], tolerance_columns={"amt": 0.02},
    )
    assert len(result["mismatches"]) == 1
    assert result["mismatches"][0]["column"] == "amt"


def test_diff_keys_and_fields_without_tolerance_columns_is_unchanged():
    source = ListRowSource([{"k": "1", "amt": "100.00"}])
    target = ListRowSource([{"k": "1", "amt": "100.00"}])
    result = diff_keys_and_fields(source, target, key_columns=["k"], compare_columns=["amt"])
    assert result["mismatches"] == []
    assert result["matched_count"] == 1


# ── tally_reconciliation_service orchestration ──────────────────────

async def _make_connection(db, org):
    conn = Connection(
        organization_id=org.id, name="Verify Tally E", category=ConnectionCategory.APPLICATION,
        connection_type=ConnectionType.TALLY_PRIME, config={"host": "127.0.0.1", "port": 9999},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


async def _make_document(db, org, *, filename="beta-supplies-invoice.pdf") -> Document:
    doc = Document(organization_id=org.id, filename=filename, storage_path=f"/tmp/{filename}")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _make_invoice(db, org, **overrides) -> ApInvoice:
    defaults = dict(
        organization_id=org.id, document_id=overrides.pop("document_id", 1), source=ApInvoiceSource.MANUAL_UPLOAD,
        invoice_number="INV-100", invoice_date=OLD,
        vendor_name="Beta Supplies", currency="INR",
        subtotal=1000.0, discount=0.0, freight=0.0, total=1180.0,
        cgst_amount=90.0, sgst_amount=90.0, igst_amount=None, cess_amount=None, tds_amount=None,
    )
    defaults.update(overrides)
    invoice = ApInvoice(**defaults)
    db.add(invoice)
    await db.commit()
    await db.refresh(invoice)
    return invoice


async def _make_voucher(db, org, conn, *, guid, party_ledger_name="Beta Supplies", total_amount=1180.0,
                         ledger_entries=None, voucher_type="Purchase", direction=None) -> TallyCanonicalVoucher:
    if ledger_entries is None:
        ledger_entries = [
            {"ledger_name": "Purchase Accounts", "amount": 1000.0, "is_deemed_positive": "Yes"},
            {"ledger_name": "CGST", "amount": 90.0, "is_deemed_positive": "Yes"},
            {"ledger_name": "SGST", "amount": 90.0, "is_deemed_positive": "Yes"},
        ]
    voucher = TallyCanonicalVoucher(
        organization_id=org.id, connection_id=conn.id, company_name="Demo Co", guid=guid, alter_id=1,
        voucher_number=guid, voucher_type=voucher_type, party_ledger_name=party_ledger_name,
        total_amount=total_amount, ledger_entries=ledger_entries, direction=direction,
    )
    db.add(voucher)
    await db.commit()
    await db.refresh(voucher)
    return voucher


async def _make_party(db, org, conn, *, guid, name="Beta Supplies", closing_balance=None) -> TallyCanonicalParty:
    party = TallyCanonicalParty(
        organization_id=org.id, connection_id=conn.id, company_name="Demo Co", guid=guid, alter_id=1,
        name=name, normalized_name=svc._normalize_name(name), closing_balance=closing_balance,
    )
    db.add(party)
    await db.commit()
    await db.refresh(party)
    return party


@pytest.mark.asyncio
async def test_matched_invoice_and_voucher_produce_no_exceptions(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    await _make_invoice(db, org)
    await _make_voucher(db, org, conn, guid="v1")

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.status == TallyReconciliationRunStatus.SUCCEEDED
    assert run.matched_count == 1
    assert run.missing_count == 0
    assert run.extra_count == 0
    assert run.mismatched_count == 0

    exceptions, total = await svc.list_exceptions(db, run.id, org.id)
    types = {e.exception_type for e in exceptions}
    assert TallyExceptionType.MATCHED in types
    assert TallyExceptionType.MISSING not in types
    assert TallyExceptionType.MISMATCH not in types


@pytest.mark.asyncio
async def test_invoice_with_no_matching_voucher_is_missing(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.missing_count == 1
    assert run.extra_count == 0

    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.MISSING)
    assert len(exceptions) == 1
    assert exceptions[0].invoice_id == invoice.id
    assert exceptions[0].party_name == "Beta Supplies"


@pytest.mark.asyncio
async def test_voucher_with_no_matching_invoice_is_extra(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    voucher = await _make_voucher(db, org, conn, guid="v1")

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.extra_count == 1
    assert run.missing_count == 0

    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.EXTRA)
    assert len(exceptions) == 1
    assert exceptions[0].voucher_id == voucher.id


@pytest.mark.asyncio
async def test_gst_mismatch_beyond_tolerance_is_flagged(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org, cgst_amount=90.0)
    voucher = await _make_voucher(db, org, conn, guid="v1", ledger_entries=[
        {"ledger_name": "Purchase Accounts", "amount": 1000.0, "is_deemed_positive": "Yes"},
        {"ledger_name": "CGST", "amount": 120.0, "is_deemed_positive": "Yes"},  # 30 off — beyond default 0.02 tolerance
        {"ledger_name": "SGST", "amount": 90.0, "is_deemed_positive": "Yes"},
    ])

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.mismatched_count == 1
    assert run.matched_count == 1  # still counted as matched (id-matched), just with a field mismatch

    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.MISMATCH)
    assert len(exceptions) == 1
    assert exceptions[0].column == "cgst_amount"
    assert exceptions[0].invoice_id == invoice.id
    assert exceptions[0].voucher_id == voucher.id


@pytest.mark.asyncio
async def test_gst_mismatch_within_tolerance_is_not_flagged(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    await _make_invoice(db, org, cgst_amount=90.0)
    await _make_voucher(db, org, conn, guid="v1", ledger_entries=[
        {"ledger_name": "Purchase Accounts", "amount": 1000.0, "is_deemed_positive": "Yes"},
        {"ledger_name": "CGST", "amount": 90.01, "is_deemed_positive": "Yes"},
        {"ledger_name": "SGST", "amount": 90.0, "is_deemed_positive": "Yes"},
    ])

    run = await svc.run_reconciliation(db, conn, company="Demo Co", tolerance=0.02)
    assert run.mismatched_count == 0
    assert run.matched_count == 1


@pytest.mark.asyncio
async def test_ledger_balance_mismatch_flagged_beyond_tolerance(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    await _make_invoice(db, org, total=1180.0)
    await _make_voucher(db, org, conn, guid="v1")
    await _make_party(db, org, conn, guid="p1", closing_balance=5000.0)  # far from ApInvoice total sum of 1180

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.ledger_balance_mismatch_count == 1

    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.LEDGER_BALANCE_MISMATCH)
    assert len(exceptions) == 1
    assert exceptions[0].party_name == "Beta Supplies"


@pytest.mark.asyncio
async def test_ledger_balance_matching_is_not_flagged(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    await _make_invoice(db, org, total=1180.0)
    await _make_voucher(db, org, conn, guid="v1")
    await _make_party(db, org, conn, guid="p1", closing_balance=1180.0)

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.ledger_balance_mismatch_count == 0


@pytest.mark.asyncio
async def test_payment_voucher_missing_flagged_for_old_matched_invoice(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org, invoice_date=OLD)
    await _make_voucher(db, org, conn, guid="v1")
    # No PAYMENT-direction voucher exists for this party at all.

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.payment_voucher_missing_count == 1

    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.PAYMENT_VOUCHER_MISSING)
    assert len(exceptions) == 1
    assert exceptions[0].invoice_id == invoice.id


@pytest.mark.asyncio
async def test_payment_voucher_present_suppresses_missing_flag(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    await _make_invoice(db, org, invoice_date=OLD)
    await _make_voucher(db, org, conn, guid="v1")
    await _make_voucher(
        db, org, conn, guid="pay1", voucher_type="Payment", direction=TallyVoucherDirection.PAYMENT,
        total_amount=1180.0, ledger_entries=[{"ledger_name": "Bank", "amount": 1180.0, "is_deemed_positive": "No"}],
    )

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.payment_voucher_missing_count == 0


@pytest.mark.asyncio
async def test_payment_voucher_missing_check_skips_recent_invoices(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    await _make_invoice(db, org, invoice_date=RECENT)
    await _make_voucher(db, org, conn, guid="v1")

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.payment_voucher_missing_count == 0


@pytest.mark.asyncio
async def test_run_scoped_by_connection_and_org(db, make_org):
    org1 = await make_org()
    org2 = await make_org()
    conn1 = await _make_connection(db, org1)
    conn2 = await _make_connection(db, org2)
    await _make_invoice(db, org1, document_id=1)
    await svc.run_reconciliation(db, conn1, company="Demo Co")
    await _make_invoice(db, org2, document_id=2)
    await svc.run_reconciliation(db, conn2, company="Demo Co")

    runs1, total1 = await svc.list_runs(db, org1.id, conn1.id)
    assert total1 == 1
    assert runs1[0].connection_id == conn1.id

    # Cross-org get_run must not leak.
    assert await svc.get_run(db, runs1[0].id, org2.id) is None
    assert await svc.get_run(db, runs1[0].id, org1.id) is not None


# ── TALLY-F: Review Queue escalation ────────────────────────────────

@pytest.mark.asyncio
async def test_missing_exception_escalates_to_real_review_queue_item(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    doc = await _make_document(db, org)
    invoice = await _make_invoice(db, org, document_id=doc.id)

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.MISSING)
    assert len(exceptions) == 1
    assert exceptions[0].review_item_id is not None

    item = await db.get(ReviewQueueItem, exceptions[0].review_item_id)
    assert item is not None
    assert item.document_id == doc.id
    assert item.organization_id == org.id
    assert item.review_type == ReviewType.BUSINESS_RULE
    assert item.status == ReviewStatus.PENDING
    assert item.issue == exceptions[0].message


@pytest.mark.asyncio
async def test_mismatch_exception_escalates_to_real_review_queue_item(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    doc = await _make_document(db, org)
    invoice = await _make_invoice(db, org, document_id=doc.id, cgst_amount=90.0)
    await _make_voucher(db, org, conn, guid="v1", ledger_entries=[
        {"ledger_name": "Purchase Accounts", "amount": 1000.0, "is_deemed_positive": "Yes"},
        {"ledger_name": "CGST", "amount": 120.0, "is_deemed_positive": "Yes"},
        {"ledger_name": "SGST", "amount": 90.0, "is_deemed_positive": "Yes"},
    ])

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.MISMATCH)
    assert len(exceptions) == 1
    assert exceptions[0].review_item_id is not None

    item = await db.get(ReviewQueueItem, exceptions[0].review_item_id)
    assert item.document_id == doc.id


@pytest.mark.asyncio
async def test_payment_voucher_missing_escalates_to_real_review_queue_item(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    doc = await _make_document(db, org)
    await _make_invoice(db, org, document_id=doc.id, invoice_date=OLD)
    await _make_voucher(db, org, conn, guid="v1")

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.PAYMENT_VOUCHER_MISSING)
    assert len(exceptions) == 1
    assert exceptions[0].review_item_id is not None


@pytest.mark.asyncio
async def test_extra_exception_never_escalates_no_invoice_anchor(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    await _make_voucher(db, org, conn, guid="v1")  # no matching invoice at all -> EXTRA

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.EXTRA)
    assert len(exceptions) == 1
    assert exceptions[0].review_item_id is None


@pytest.mark.asyncio
async def test_ledger_balance_mismatch_never_escalates_no_invoice_anchor(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    doc = await _make_document(db, org)
    await _make_invoice(db, org, document_id=doc.id, total=1180.0)
    await _make_voucher(db, org, conn, guid="v1")
    await _make_party(db, org, conn, guid="p1", closing_balance=5000.0)

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.LEDGER_BALANCE_MISMATCH)
    assert len(exceptions) == 1
    assert exceptions[0].review_item_id is None


@pytest.mark.asyncio
async def test_matched_exception_never_escalates_even_with_invoice_anchor(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    doc = await _make_document(db, org)
    await _make_invoice(db, org, document_id=doc.id)
    await _make_voucher(db, org, conn, guid="v1")

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.MATCHED)
    assert len(exceptions) == 1
    assert exceptions[0].review_item_id is None


@pytest.mark.asyncio
async def test_repeat_reconciliation_reuses_same_open_review_item(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    doc = await _make_document(db, org)
    await _make_invoice(db, org, document_id=doc.id)  # still-missing invoice, same voucher never added

    run1 = await svc.run_reconciliation(db, conn, company="Demo Co")
    exceptions1, _ = await svc.list_exceptions(db, run1.id, org.id, exception_type=TallyExceptionType.MISSING)
    review_item_id = exceptions1[0].review_item_id
    assert review_item_id is not None

    run2 = await svc.run_reconciliation(db, conn, company="Demo Co")
    exceptions2, _ = await svc.list_exceptions(db, run2.id, org.id, exception_type=TallyExceptionType.MISSING)
    assert exceptions2[0].review_item_id == review_item_id  # same open item reused, not a duplicate

    result = await db.execute(select(ReviewQueueItem).where(ReviewQueueItem.document_id == doc.id))
    assert len(list(result.scalars().all())) == 1


@pytest.mark.asyncio
async def test_escalation_skips_silently_when_no_real_document_exists(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    await _make_invoice(db, org, document_id=999999)  # no Document row with this id

    run = await svc.run_reconciliation(db, conn, company="Demo Co")
    assert run.status == TallyReconciliationRunStatus.SUCCEEDED  # never fails the run
    exceptions, _ = await svc.list_exceptions(db, run.id, org.id, exception_type=TallyExceptionType.MISSING)
    assert exceptions[0].review_item_id is None
