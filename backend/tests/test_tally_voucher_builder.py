"""
Automated tests for TALLY-D: tally_mapping_service (versioned mapping),
tally_voucher_builder (pure voucher construction + XML serialization),
tally_voucher_validation_service (deterministic pre-write checks), and
tally_voucher_preview_service (orchestration — party-ledger resolution,
duplicate pre-check, persistence).
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceLine, ApInvoiceSource, ApInvoiceVoucherType
from app.models.tally_canonical import TallyCanonicalParty, TallyCanonicalVoucher
from app.models.tally_mapping import TallyVoucherPreviewStatus
from app.services import tally_mapping_service
from app.services import tally_voucher_preview_service as preview_svc
from app.services.tally_voucher_builder import TallyVoucherBuildError, build_voucher, build_voucher_xml
from app.services.tally_voucher_validation_service import overall_status, validate_voucher


async def _make_connection(db, org):
    from app.models.connection import Connection, ConnectionCategory, ConnectionType
    conn = Connection(
        organization_id=org.id, name="Verify Tally D", category=ConnectionCategory.APPLICATION,
        connection_type=ConnectionType.TALLY_PRIME, config={"host": "127.0.0.1", "port": 9999},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


async def _make_invoice(db, org, **overrides) -> ApInvoice:
    defaults = dict(
        organization_id=org.id, document_id=overrides.pop("document_id", 1), source=ApInvoiceSource.MANUAL_UPLOAD,
        invoice_number="INV-100", invoice_date=datetime(2026, 1, 15, tzinfo=timezone.utc),
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


@pytest.mark.asyncio
async def test_seed_default_mapping_idempotent(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    first = await tally_mapping_service.seed_default_mapping(db, org.id, conn.id)
    second = await tally_mapping_service.seed_default_mapping(db, org.id, conn.id)
    assert len(first) == len(second)
    assert {r.id for r in first} == {r.id for r in second}


@pytest.mark.asyncio
async def test_publish_mapping_creates_new_version_and_flips_current(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    await tally_mapping_service.seed_default_mapping(db, org.id, conn.id)

    new_rows = await tally_mapping_service.publish_mapping(
        db, org.id, conn.id,
        [{"canonical_field": "__fixed__purchase_ledger", "tally_field": "purchase_ledger_name", "default_value": "Direct Expenses", "transform_config": {"type": "fixed"}}],
        created_by=1,
    )
    assert all(r.version == 2 for r in new_rows)

    current = await tally_mapping_service.get_current_mapping(db, org.id, conn.id)
    assert len(current) == 1
    assert current[0].default_value == "Direct Expenses"


@pytest.mark.asyncio
async def test_build_voucher_balances_and_uses_party_and_gst_ledgers(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    mapping = await tally_mapping_service.seed_default_mapping(db, org.id, conn.id)
    invoice = await _make_invoice(db, org)

    voucher = build_voucher(invoice, [], mapping, party_ledger_name="Beta Supplies")
    assert voucher["voucher_type"] == "Purchase"
    assert voucher["party_ledger_name"] == "Beta Supplies"
    entry_sum = round(sum(e["amount"] for e in voucher["ledger_entries"]), 2)
    assert entry_sum == 0.0

    names = {e["ledger_name"] for e in voucher["ledger_entries"]}
    assert "Purchase Accounts" in names
    assert "CGST" in names and "SGST" in names
    assert "IGST" not in names  # zero amount, not included


@pytest.mark.asyncio
async def test_build_voucher_folds_tds_into_party_and_tds_ledger(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    mapping = await tally_mapping_service.seed_default_mapping(db, org.id, conn.id)
    invoice = await _make_invoice(db, org, tds_amount=20.0)

    voucher = build_voucher(invoice, [], mapping, party_ledger_name="Beta Supplies")
    entries = {e["ledger_name"]: e["amount"] for e in voucher["ledger_entries"]}
    assert entries["TDS Payable"] == -20.0
    assert entries["Beta Supplies"] == -(1180.0 - 20.0)
    assert round(sum(entries.values()), 2) == 0.0


@pytest.mark.asyncio
async def test_build_voucher_missing_required_field_raises(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    mapping = await tally_mapping_service.seed_default_mapping(db, org.id, conn.id)
    invoice = await _make_invoice(db, org, vendor_name=None)

    with pytest.raises(TallyVoucherBuildError):
        build_voucher(invoice, [], mapping, party_ledger_name="")


def test_build_voucher_xml_contains_import_data_and_ledger_entries():
    voucher = {
        "voucher_type": "Purchase", "voucher_date": datetime(2026, 1, 15), "party_ledger_name": "Beta Supplies",
        "narration": "INV-100", "reference": "INV-100",
        "ledger_entries": [
            {"ledger_name": "Purchase Accounts", "amount": 1000.0, "is_deemed_positive": "Yes"},
            {"ledger_name": "Beta Supplies", "amount": -1000.0, "is_deemed_positive": "No"},
        ],
    }
    xml = build_voucher_xml(voucher, company="Demo Co")
    assert "<TALLYREQUEST>Import Data</TALLYREQUEST>" in xml
    assert "<PARTYLEDGERNAME>Beta Supplies</PARTYLEDGERNAME>" in xml
    assert "<DATE>20260115</DATE>" in xml
    assert xml.count("<ALLLEDGERENTRIES.LIST>") == 2


@pytest.mark.asyncio
async def test_validate_voucher_balance_fail_when_entries_dont_sum_to_zero(db, make_org):
    org = await make_org()
    invoice = await _make_invoice(db, org)
    voucher = {
        "party_ledger_name": "Beta Supplies", "voucher_date": invoice.invoice_date,
        "ledger_entries": [{"ledger_name": "Purchase Accounts", "amount": 1000.0, "is_deemed_positive": "Yes"}],
    }
    results = validate_voucher(voucher, invoice, known_party_names={"Beta Supplies"}, known_ledger_names={"Beta Supplies"}, possible_duplicate=False)
    balance = next(r for r in results if r["category"].value == "BALANCE")
    assert balance["result"].value == "FAIL"
    assert overall_status(results) == "BLOCKED"


@pytest.mark.asyncio
async def test_validate_voucher_party_not_found_fails(db, make_org):
    org = await make_org()
    invoice = await _make_invoice(db, org)
    voucher = {
        "party_ledger_name": "Unknown Vendor", "voucher_date": invoice.invoice_date,
        "ledger_entries": [
            {"ledger_name": "Purchase Accounts", "amount": 1180.0, "is_deemed_positive": "Yes"},
            {"ledger_name": "Unknown Vendor", "amount": -1180.0, "is_deemed_positive": "No"},
        ],
    }
    results = validate_voucher(voucher, invoice, known_party_names={"Beta Supplies"}, known_ledger_names={"Beta Supplies"}, possible_duplicate=False)
    ledger_check = [r for r in results if r["category"].value == "LEDGER_EXISTS" and "Unknown Vendor" in r["message"]]
    assert ledger_check and ledger_check[0]["result"].value == "FAIL"
    assert overall_status(results) == "BLOCKED"


@pytest.mark.asyncio
async def test_validate_voucher_gst_mutual_exclusivity_warning(db, make_org):
    org = await make_org()
    invoice = await _make_invoice(db, org, igst_amount=100.0)  # both cgst/sgst AND igst set
    voucher = {
        "party_ledger_name": "Beta Supplies", "voucher_date": invoice.invoice_date,
        "ledger_entries": [
            {"ledger_name": "Purchase Accounts", "amount": 1000.0, "is_deemed_positive": "Yes"},
            {"ledger_name": "CGST", "amount": 90.0, "is_deemed_positive": "Yes"},
            {"ledger_name": "SGST", "amount": 90.0, "is_deemed_positive": "Yes"},
            {"ledger_name": "IGST", "amount": 100.0, "is_deemed_positive": "Yes"},
            {"ledger_name": "Beta Supplies", "amount": -1280.0, "is_deemed_positive": "No"},
        ],
    }
    results = validate_voucher(voucher, invoice, known_party_names={"Beta Supplies"}, known_ledger_names={"Beta Supplies", "CGST", "SGST", "IGST"}, possible_duplicate=False)
    gst = next(r for r in results if r["category"].value == "GST")
    assert gst["result"].value == "WARNING"
    # A WARNING alone never blocks
    assert overall_status(results) == "READY"


@pytest.mark.asyncio
async def test_build_preview_resolves_canonical_party_and_persists(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    db.add(TallyCanonicalParty(
        organization_id=org.id, connection_id=conn.id, company_name="Demo Co", guid="beta-guid", alter_id=1,
        name="Beta Supplies Pvt Ltd", normalized_name="beta supplies pvt ltd", parent="Sundry Creditors", party_type="SUPPLIER",
    ))
    await db.commit()
    invoice = await _make_invoice(db, org, vendor_name="Beta Supplies Pvt. Ltd.")  # extracted spelling differs slightly

    preview = await preview_svc.build_preview(db, conn, invoice_id=invoice.id, company="Demo Co", actor_user_id=1)

    assert preview.party_ledger_name == "Beta Supplies Pvt Ltd"  # resolved to the REAL canonical ledger name, not the raw extracted spelling
    assert preview.status == TallyVoucherPreviewStatus.READY
    assert preview.idempotency_key == f"ap-invoice-{invoice.id}-tally-{conn.id}"
    assert preview.voucher_xml and "<TALLYREQUEST>Import Data</TALLYREQUEST>" in preview.voucher_xml

    results = await preview_svc.list_validation_results(db, preview.id)
    assert any(r.category.value == "BALANCE" and r.result.value == "PASS" for r in results)


@pytest.mark.asyncio
async def test_build_preview_upserts_not_duplicates_on_rebuild(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    invoice = await _make_invoice(db, org)

    first = await preview_svc.build_preview(db, conn, invoice_id=invoice.id, company="Demo Co")
    second = await preview_svc.build_preview(db, conn, invoice_id=invoice.id, company="Demo Co")
    assert first.id == second.id

    from app.models.tally_mapping import TallyVoucherPreview
    all_previews = (await db.execute(select(TallyVoucherPreview).where(TallyVoucherPreview.invoice_id == invoice.id))).scalars().all()
    assert len(all_previews) == 1


@pytest.mark.asyncio
async def test_build_preview_flags_possible_duplicate_against_extracted_voucher(db, make_org):
    org = await make_org()
    conn = await _make_connection(db, org)
    db.add(TallyCanonicalVoucher(
        organization_id=org.id, connection_id=conn.id, company_name="Demo Co", guid="existing-guid", alter_id=1,
        voucher_number="PB/999", voucher_type="Purchase", party_ledger_name="Beta Supplies", total_amount=1180.0,
    ))
    await db.commit()
    invoice = await _make_invoice(db, org)  # same party + same total (1180.0)

    preview = await preview_svc.build_preview(db, conn, invoice_id=invoice.id, company="Demo Co")
    results = await preview_svc.list_validation_results(db, preview.id)
    dup = next(r for r in results if r.category.value == "DUPLICATE")
    assert dup.result.value == "WARNING"


@pytest.mark.asyncio
async def test_build_preview_rejects_invoice_from_other_org(db, make_org):
    org1 = await make_org()
    org2 = await make_org()
    conn = await _make_connection(db, org1)
    invoice = await _make_invoice(db, org2)  # belongs to a different org

    with pytest.raises(preview_svc.TallyVoucherPreviewError):
        await preview_svc.build_preview(db, conn, invoice_id=invoice.id, company="Demo Co")
