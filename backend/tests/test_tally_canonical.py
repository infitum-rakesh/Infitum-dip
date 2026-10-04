"""
Automated tests for TALLY-C: tally_canonical_service.py's materialization
of TallyCanonicalVoucher/Party/Ledger from TallyExtractionRecord rows.

Exercises materialize_from_run() directly against hand-built
TallyExtractionRun/TallyExtractionRecord rows (no live Tally connector
needed here — that wiring is covered by test_tally_extraction.py's
test_start_extraction_also_materializes_canonical_voucher) so these
tests isolate the upsert/idempotency/normalization logic itself.
"""
from datetime import date

import pytest
from sqlalchemy import select

from app.models.tally_canonical import TallyCanonicalLedger, TallyCanonicalParty, TallyCanonicalVoucher
from app.models.tally_extraction import TallyExtractionRecord, TallyExtractionRun, TallyExtractionStatus, TallyExtractionType
from app.services import tally_canonical_service as svc


async def _make_run(db, org, extraction_type, *, connection_id=1):
    run = TallyExtractionRun(
        organization_id=org.id, connection_id=connection_id, company_name="Demo Co",
        extraction_type=extraction_type, status=TallyExtractionStatus.COMPLETED,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _add_record(db, run, raw_data, *, external_ref=None):
    rec = TallyExtractionRecord(run_id=run.id, extraction_type=run.extraction_type, external_ref=external_ref, raw_data=raw_data)
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return rec


@pytest.mark.asyncio
async def test_materialize_voucher_creates_canonical_row(db, make_org):
    org = await make_org()
    run = await _make_run(db, org, TallyExtractionType.VOUCHERS)
    await _add_record(db, run, {
        "date": "20240405", "voucher_number": "PB/001", "voucher_type": "Purchase",
        "party_ledger_name": "Beta Supplies", "narration": None, "guid": "PB-001-guid", "alter_id": "1",
        "ledger_entries": [{"ledger_name": "Beta Supplies", "amount": "1180.00", "is_deemed_positive": "Yes"}],
    })

    counts = await svc.materialize_from_run(db, run)
    assert counts == {"vouchers": 1, "parties": 0, "ledgers": 0, "skipped_no_identity": 0}

    voucher = (await db.execute(select(TallyCanonicalVoucher).where(TallyCanonicalVoucher.guid == "PB-001-guid"))).scalar_one()
    assert voucher.voucher_number == "PB/001"
    assert voucher.voucher_date == date(2024, 4, 5)
    assert voucher.total_amount == 1180.00
    assert voucher.alter_id == 1


@pytest.mark.asyncio
async def test_materialize_voucher_upsert_is_idempotent_on_unchanged_alter_id(db, make_org):
    org = await make_org()
    run = await _make_run(db, org, TallyExtractionType.VOUCHERS)
    raw = {
        "date": "20240405", "voucher_number": "PB/001", "voucher_type": "Purchase",
        "party_ledger_name": "Beta Supplies", "guid": "PB-001-guid", "alter_id": "1",
        "ledger_entries": [{"ledger_name": "Beta Supplies", "amount": "1180.00", "is_deemed_positive": "Yes"}],
    }
    await _add_record(db, run, raw)
    first_counts = await svc.materialize_from_run(db, run)
    assert first_counts["vouchers"] == 1

    # Re-run against the SAME record (same alter_id) — must be a real no-op,
    # not a second row and not even a rewrite of the existing one.
    second_counts = await svc.materialize_from_run(db, run)
    assert second_counts["vouchers"] == 0

    all_vouchers = (await db.execute(select(TallyCanonicalVoucher).where(TallyCanonicalVoucher.guid == "PB-001-guid"))).scalars().all()
    assert len(all_vouchers) == 1


@pytest.mark.asyncio
async def test_materialize_voucher_updates_in_place_when_alter_id_changes(db, make_org):
    org = await make_org()
    run = await _make_run(db, org, TallyExtractionType.VOUCHERS)
    await _add_record(db, run, {
        "date": "20240405", "voucher_number": "PB/001", "voucher_type": "Purchase",
        "party_ledger_name": "Beta Supplies", "guid": "PB-001-guid", "alter_id": "1",
        "ledger_entries": [{"ledger_name": "Beta Supplies", "amount": "1180.00", "is_deemed_positive": "Yes"}],
    })
    await svc.materialize_from_run(db, run)

    # A second run, same GUID, ALTERID bumped (Tally's own edit signal) and
    # the amount actually changed — must update the SAME row, not insert one.
    run2 = await _make_run(db, org, TallyExtractionType.VOUCHERS, connection_id=run.connection_id)
    await _add_record(db, run2, {
        "date": "20240405", "voucher_number": "PB/001", "voucher_type": "Purchase",
        "party_ledger_name": "Beta Supplies", "guid": "PB-001-guid", "alter_id": "2",
        "ledger_entries": [{"ledger_name": "Beta Supplies", "amount": "1500.00", "is_deemed_positive": "Yes"}],
    })
    counts = await svc.materialize_from_run(db, run2)
    assert counts["vouchers"] == 1

    all_vouchers = (await db.execute(select(TallyCanonicalVoucher).where(TallyCanonicalVoucher.guid == "PB-001-guid"))).scalars().all()
    assert len(all_vouchers) == 1
    assert all_vouchers[0].alter_id == 2
    assert all_vouchers[0].total_amount == 1500.00


@pytest.mark.asyncio
async def test_materialize_party_master_computes_normalized_name(db, make_org):
    org = await make_org()
    run = await _make_run(db, org, TallyExtractionType.PARTY_MASTER)
    await _add_record(db, run, {
        "name": "Beta Supplies Pvt. Ltd.", "parent": "Sundry Creditors", "gstin": "27AAAAA0000A1Z5",
        "state": "Maharashtra", "address_lines": ["1 MG Road"], "email": None, "mobile": None,
        "opening_balance": "0.00", "closing_balance": "1180.00", "guid": "beta-guid", "alter_id": "3",
        "party_type": "SUPPLIER",
    })

    counts = await svc.materialize_from_run(db, run)
    assert counts["parties"] == 1

    party = (await db.execute(select(TallyCanonicalParty).where(TallyCanonicalParty.guid == "beta-guid"))).scalar_one()
    assert party.name == "Beta Supplies Pvt. Ltd."
    assert party.normalized_name == "beta supplies pvt ltd"
    assert party.party_type == "SUPPLIER"
    assert party.gstin == "27AAAAA0000A1Z5"
    assert party.closing_balance == 1180.00


@pytest.mark.asyncio
async def test_materialize_ledger_extraction_splits_ledger_and_voucher_records(db, make_org):
    org = await make_org()
    run = await _make_run(db, org, TallyExtractionType.LEDGER)
    await _add_record(db, run, {
        "kind": "ledger", "name": "Beta Supplies", "parent": "Sundry Creditors",
        "opening_balance": "0.00", "closing_balance": "1180.00", "guid": "beta-ledger-guid", "alter_id": "1",
    })
    await _add_record(db, run, {
        "date": "20240405", "voucher_number": "PB/001", "voucher_type": "Purchase",
        "party_ledger_name": "Beta Supplies", "guid": "PB-001-guid", "alter_id": "1",
        "ledger_entries": [{"ledger_name": "Beta Supplies", "amount": "1180.00", "is_deemed_positive": "Yes"}],
    })

    counts = await svc.materialize_from_run(db, run)
    assert counts == {"vouchers": 1, "parties": 0, "ledgers": 1, "skipped_no_identity": 0}

    ledger = (await db.execute(select(TallyCanonicalLedger).where(TallyCanonicalLedger.guid == "beta-ledger-guid"))).scalar_one()
    assert ledger.name == "Beta Supplies"
    assert ledger.closing_balance == 1180.00


@pytest.mark.asyncio
async def test_materialize_bill_allocations_has_no_stable_identity_and_is_disclosed_skipped(db, make_org):
    org = await make_org()
    run = await _make_run(db, org, TallyExtractionType.BILL_ALLOCATIONS)
    await _add_record(db, run, {"party": "Beta Supplies", "bill_name": "PB/001", "bill_date": "20240405", "closing_balance": "1180.00"})

    counts = await svc.materialize_from_run(db, run)
    assert counts == {"vouchers": 0, "parties": 0, "ledgers": 0, "skipped_no_identity": 1}


@pytest.mark.asyncio
async def test_materialize_voucher_without_guid_is_skipped_not_silently_dropped(db, make_org):
    org = await make_org()
    run = await _make_run(db, org, TallyExtractionType.VOUCHERS)
    await _add_record(db, run, {
        "date": "20240405", "voucher_number": "PB/002", "voucher_type": "Purchase",
        "party_ledger_name": "Gamma Vendors", "guid": None, "alter_id": None, "ledger_entries": [],
    })

    counts = await svc.materialize_from_run(db, run)
    assert counts == {"vouchers": 0, "parties": 0, "ledgers": 0, "skipped_no_identity": 1}


@pytest.mark.asyncio
async def test_re_materialize_run_scopes_by_organization(db, make_org):
    org1 = await make_org()
    org2 = await make_org()
    run = await _make_run(db, org1, TallyExtractionType.PARTY_MASTER)
    await _add_record(db, run, {"name": "Acme", "parent": "Sundry Debtors", "guid": "acme-guid", "alter_id": "1"})

    assert await svc.re_materialize_run(db, run.id, org2.id) is None  # cross-org: not found
    result = await svc.re_materialize_run(db, run.id, org1.id)
    assert result["parties"] == 1
