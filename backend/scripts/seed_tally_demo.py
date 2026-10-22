"""
seed_tally_demo.py — one-shot seeder for a client-facing Tally Integration
demo. Drives the REAL backend service layer in-process (no HTTP/curl) so
every number the demo shows is the product's own real output, not a
fixture dumped straight into the DB.

What this does, in order:
  1. Runs a real VOUCHERS + PARTY_MASTER extraction against connection 19
     ("Rakesh Tally Test", org 2) which talks to the local mock Tally
     gateway at 127.0.0.1:9501 (see scripts/mock_tally_gateway.py) — this
     populates TallyExtractionRun/Record and, via the extraction
     service's own auto-canonicalization step, TallyCanonicalVoucher +
     TallyCanonicalParty.
  2. Creates 11 synthetic Document + ApInvoice rows (the exact
     Document-then-ApInvoice pattern backend/tests/test_tally_reconciliation.py
     already uses to seed invoice-side data without OCR/AI) — one for
     each purchase-side reconciliation scenario (matched / mismatch /
     missing / duplicate target / payment-voucher-missing / ledger
     mismatch). One deliberately has NO invoice (the EXTRA scenario).
  3. Runs real reconciliation (tally_reconciliation_service.run_reconciliation)
     and reports the resulting exception type counts.
  4. Publishes one ApprovalRule scoped to TALLY_VOUCHER so write-back
     approval is genuinely gated by a human, not auto-approved.
  5. Builds one TallyVoucherPreview for a clean matched invoice and
     submits it for approval — left PENDING on purpose so the live demo
     presenter can click Approve themselves in the browser.

Idempotent-ish: re-running after a partial failure will create duplicate
Document/ApInvoice rows (no unique constraint guards this script's own
rows), so on a genuine re-run, delete previously-created demo invoices
first (see cleanup block commented at the bottom).
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select

import app.main  # noqa: F401 -- registers every model (incl. schedules) on the shared metadata before any FK resolution

from app.database.connection import AsyncSessionLocal
from app.models.approval.rules import (
    ApprovalEntityType, ApproverType, ApprovalStepType,
)
from app.models.connection import Connection
from app.models.document import Document
from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceSource, ApInvoiceVoucherType
from app.models.tally_canonical import TallyCanonicalVoucher, TallyCanonicalParty
from app.models.tally_extraction import TallyExtractionMode, TallyExtractionType
from app.models.tally_reconciliation import TallyExceptionType
from app.services import tally_extraction_service, tally_reconciliation_service
from app.services import tally_voucher_preview_service, tally_approval_service
from app.services.approval import approval_rule_service
from app.services.approval.approval_rule_service import RuleInput, StepInput

ORG_ID = 2
CONNECTION_ID = 19
COMPANY = "Rakesh Test Co"
APPROVER_USER_ID = 3  # rakeshbkec208@gmail.com, active ORG_ADMIN in org 2

FROM_DATE = date(2026, 8, 1)
TO_DATE = date(2026, 10, 16)

OLD_DATE = datetime.now(timezone.utc) - timedelta(days=40)   # triggers PAYMENT_VOUCHER_MISSING (>30d grace)
RECENT_DATE = datetime.now(timezone.utc) - timedelta(days=3)


async def _make_document(db, *, filename: str) -> Document:
    doc = Document(organization_id=ORG_ID, filename=filename, storage_path=f"/tmp/tally-demo/{filename}")
    db.add(doc)
    await db.flush()
    return doc


async def _make_invoice(db, **overrides) -> ApInvoice:
    doc = overrides.pop("_document")
    defaults = dict(
        organization_id=ORG_ID, document_id=doc.id, source=ApInvoiceSource.MANUAL_UPLOAD,
        voucher_type=ApInvoiceVoucherType.PURCHASE, currency="INR",
        discount=0.0, freight=0.0, cess_amount=None, tds_amount=None,
    )
    defaults.update(overrides)
    invoice = ApInvoice(**defaults)
    db.add(invoice)
    await db.flush()
    return invoice


async def seed_invoices(db):
    """11 synthetic Document+ApInvoice rows mirroring the 11 purchase
    vouchers in mock_tally_gateway.py's _default_vouchers() that are
    meant to pair with an invoice. Amounts/GST must match the Tally
    voucher exactly for MATCHED, deliberately differ for MISMATCH."""
    scenarios = [
        # SGT1 — clean match
        dict(invoice_number="INV-SGT-1001", vendor_name="Shree Ganesh Traders",
             invoice_date=RECENT_DATE, subtotal=50000.0, total=59000.0,
             cgst_amount=4500.0, sgst_amount=4500.0, igst_amount=None),
        # SGT2 — clean match
        dict(invoice_number="INV-SGT-1002", vendor_name="Shree Ganesh Traders",
             invoice_date=RECENT_DATE, subtotal=30000.0, total=35400.0,
             cgst_amount=2700.0, sgst_amount=2700.0, igst_amount=None),
        # SGT3 — MISMATCH: Tally has CGST/SGST 1800 each; invoice carries wrong 1300
        dict(invoice_number="INV-SGT-1003", vendor_name="Shree Ganesh Traders",
             invoice_date=RECENT_DATE, subtotal=20000.0, total=23600.0,
             cgst_amount=1300.0, sgst_amount=1300.0, igst_amount=None),
        # SGT4 — MISSING: invoice exists, no Tally voucher at all (no P-1004 in mock)
        dict(invoice_number="INV-SGT-1004", vendor_name="Shree Ganesh Traders",
             invoice_date=RECENT_DATE, subtotal=35000.0, total=41300.0,
             cgst_amount=3150.0, sgst_amount=3150.0, igst_amount=None),
        # PE1 — clean match, inter-state (IGST)
        dict(invoice_number="INV-PE-2001", vendor_name="Patel Electricals Pvt Ltd",
             invoice_date=RECENT_DATE, subtotal=80000.0, total=94400.0,
             cgst_amount=None, sgst_amount=None, igst_amount=14400.0),
        # PE2 — MISMATCH: Tally has IGST 7200 (gross 47200); invoice carries wrong 6400 but same gross total so the engine still pairs them
        dict(invoice_number="INV-PE-2002", vendor_name="Patel Electricals Pvt Ltd",
             invoice_date=RECENT_DATE, subtotal=40000.0, total=47200.0,
             cgst_amount=None, sgst_amount=None, igst_amount=6400.0),
        # OS1 — matched but old (40 days), vendor has zero Payment vouchers -> PAYMENT_VOUCHER_MISSING
        dict(invoice_number="INV-OS-3001", vendor_name="Om Steel Industries",
             invoice_date=OLD_DATE, subtotal=60000.0, total=70800.0,
             cgst_amount=5400.0, sgst_amount=5400.0, igst_amount=None),
        # KP1 — matched; vendor also used for LEDGER_BALANCE_MISMATCH (closing_balance seeded at 56000 vs invoiced 47200)
        dict(invoice_number="INV-KP-4001", vendor_name="Krishna Packaging Co",
             invoice_date=RECENT_DATE, subtotal=25000.0, total=29500.0,
             cgst_amount=2250.0, sgst_amount=2250.0, igst_amount=None),
        # KP-dup — matches BOTH duplicate Tally vouchers (DUPLICATE_TARGET_MATCH)
        dict(invoice_number="INV-KP-4002", vendor_name="Krishna Packaging Co",
             invoice_date=RECENT_DATE, subtotal=15000.0, total=17700.0,
             cgst_amount=1350.0, sgst_amount=1350.0, igst_amount=None),
        # BOS1 — clean match
        dict(invoice_number="INV-BOS-5001", vendor_name="Bharat Office Supplies",
             invoice_date=RECENT_DATE, subtotal=12000.0, total=14160.0,
             cgst_amount=1080.0, sgst_amount=1080.0, igst_amount=None),
        # SLS1 — clean match with TDS. NOTE: TallyCanonicalVoucher.total_amount for a
        # TDS voucher is the party ledger's own (net-of-TDS) credit entry, not the
        # gross — the reconciliation key must match that net figure (23600-400=23200).
        dict(invoice_number="INV-SLS-6001", vendor_name="Sai Logistics Services",
             invoice_date=RECENT_DATE, subtotal=20000.0, total=23200.0,
             cgst_amount=1800.0, sgst_amount=1800.0, igst_amount=None, tds_amount=400.0),
    ]
    created = []
    for sc in scenarios:
        doc = await _make_document(db, filename=f"{sc['invoice_number']}.pdf")
        inv = await _make_invoice(db, _document=doc, **sc)
        created.append(inv)
    await db.commit()
    print(f"[seed] created {len(created)} synthetic invoices (Document+ApInvoice)")
    return created


async def run_extraction(db, connection: Connection):
    for etype in (TallyExtractionType.VOUCHERS, TallyExtractionType.PARTY_MASTER):
        run = await tally_extraction_service.start_extraction(
            db, connection, extraction_type=etype, company=COMPANY,
            from_date=FROM_DATE if etype == TallyExtractionType.VOUCHERS else None,
            to_date=TO_DATE if etype == TallyExtractionType.VOUCHERS else None,
            mode=TallyExtractionMode.FULL, trigger="Demo Seed",
        )
        print(f"[extraction] {etype.value}: status={run.status.value} records_fetched={run.records_fetched} "
              f"chunks_failed={run.chunks_failed}/{run.chunks_total} error={run.error_message}")

    voucher_count = (await db.execute(
        select(func.count()).select_from(TallyCanonicalVoucher).where(TallyCanonicalVoucher.connection_id == connection.id)
    )).scalar_one()
    party_count = (await db.execute(
        select(func.count()).select_from(TallyCanonicalParty).where(TallyCanonicalParty.connection_id == connection.id)
    )).scalar_one()
    print(f"[canonical] TallyCanonicalVoucher rows for this connection: {voucher_count}")
    print(f"[canonical] TallyCanonicalParty rows for this connection: {party_count}")


async def run_recon(db, connection: Connection):
    run = await tally_reconciliation_service.run_reconciliation(db, connection, company=COMPANY, trigger="Demo Seed")
    print(f"[reconciliation] run #{run.id} status={run.status.value}")
    print(f"  matched={run.matched_count} missing={run.missing_count} extra={run.extra_count} "
          f"mismatch={run.mismatched_count}")
    from app.models.tally_reconciliation import TallyReconciliationException
    rows = (await db.execute(
        select(TallyReconciliationException.exception_type, func.count())
        .where(TallyReconciliationException.run_id == run.id)
        .group_by(TallyReconciliationException.exception_type)
    )).all()
    print("  exception breakdown:")
    for exc_type, count in rows:
        print(f"    {exc_type.value}: {count}")
    return run


async def publish_approval_rule(db):
    payload = RuleInput(
        name="Tally Voucher Write-Back Approval",
        entity_type=ApprovalEntityType.TALLY_VOUCHER,
        description="Every Tally write-back voucher must be approved by an org admin before posting.",
        approval_required=True, approval_count=1,
        steps=[
            StepInput(
                step_type=ApprovalStepType.APPROVAL, step_number=1,
                approver_type=ApproverType.USER, approver_user_id=APPROVER_USER_ID,
                label="Admin approval",
            ),
        ],
    )
    rule = await approval_rule_service.publish_rule(db, ORG_ID, payload, actor_id=APPROVER_USER_ID)
    print(f"[approval] published rule id={rule.id} name={rule.name!r} entity_type={rule.entity_type.value} v{rule.version}")
    return rule


async def submit_one_preview_for_approval(db, connection: Connection, invoices):
    # SGT1 — the first clean matched invoice
    sgt1 = next(i for i in invoices if i.invoice_number == "INV-SGT-1001")
    preview = await tally_voucher_preview_service.build_preview(
        db, connection, invoice_id=sgt1.id, company=COMPANY, actor_user_id=APPROVER_USER_ID,
    )
    print(f"[preview] built preview id={preview.id} status={preview.status.value} for invoice {sgt1.invoice_number}")
    if preview.status.value != "READY":
        print(f"[preview] NOT READY — skipping approval submission. Validation issues should be reviewed in the UI.")
        return preview, None
    preview, workflow = await tally_approval_service.submit_for_approval(
        db, preview_id=preview.id, organization_id=ORG_ID, actor_user_id=APPROVER_USER_ID,
    )
    print(f"[approval] submitted preview {preview.id} -> workflow {workflow.id} approval_status={preview.approval_status.value}")
    return preview, workflow


async def main():
    async with AsyncSessionLocal() as db:
        connection = await db.get(Connection, CONNECTION_ID)
        if connection is None or connection.organization_id != ORG_ID:
            raise SystemExit(f"Connection {CONNECTION_ID} not found in org {ORG_ID}")

        print("=" * 70)
        print("STEP 1/5 — Real Tally extraction (Vouchers + Party Master)")
        print("=" * 70)
        await run_extraction(db, connection)

        print()
        print("=" * 70)
        print("STEP 2/5 — Seeding synthetic invoice-side data (Document+ApInvoice)")
        print("=" * 70)
        invoices = await seed_invoices(db)

        print()
        print("=" * 70)
        print("STEP 3/5 — Real reconciliation run")
        print("=" * 70)
        await run_recon(db, connection)

        print()
        print("=" * 70)
        print("STEP 4/5 — Publishing TALLY_VOUCHER approval rule")
        print("=" * 70)
        await publish_approval_rule(db)

        print()
        print("=" * 70)
        print("STEP 5/5 — Building + submitting one voucher preview for live approval")
        print("=" * 70)
        await submit_one_preview_for_approval(db, connection, invoices)

        print()
        print("DONE. Demo data is live in the database. Log in as the org-2 admin")
        print(f"(user id {APPROVER_USER_ID}) to see the PENDING approval and walk the full flow.")


if __name__ == "__main__":
    asyncio.run(main())
