"""
Automated tests for TALLY-J: scheduled/batch Tally reconciliation.

Covers the new pieces added on top of the existing Schedules module and
Tally reconciliation engine:
  - schedule_service.py's tally_config CRUD (create/update/delete/list
    filter) and mutual-exclusivity with anomaly_job_id/document_config.
  - execute_schedule()'s real Tally dispatch branch
    (_execute_tally_reconciliation_schedule), including its
    SUCCESS/WARNING/FAILED classification.
  - tally_scheduled_reconciliation_service.run_scheduled_reconciliation()
    end-to-end against a real (unreachable) connection — extraction
    failures must not abort reconciliation.
  - tally_extraction_service.recover_stuck_tally_extraction_runs() startup
    recovery.
  - monitoring/repository.py's tally_overview() aggregation.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.connection import Connection, ConnectionCategory, ConnectionType
from app.models.schedule import RunStatus, Schedule, ScheduleFrequency, ScheduleRun, ScheduleStatus
from app.models.tally_extraction import TallyExtractionRun, TallyExtractionStatus, TallyExtractionType
from app.models.tally_reconciliation import TallyReconciliationRun, TallyReconciliationRunStatus
from app.models.tally_schedule import TallyReconciliationScheduleConfig
from app.schemas.schedules import ScheduleCreateRequest, ScheduleUpdateRequest, TallyReconciliationScheduleConfigIn
from app.services import schedule_service as sched_svc
from app.services import tally_extraction_service, tally_reconciliation_service
from app.services import tally_scheduled_reconciliation_service as tally_sched_recon_svc


async def _make_tally_connection(db, org, *, name="Verify Tally J") -> Connection:
    conn = Connection(
        organization_id=org.id, name=name, category=ConnectionCategory.APPLICATION,
        connection_type=ConnectionType.TALLY_PRIME, config={"host": "127.0.0.1", "port": 9999},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


async def _make_non_tally_connection(db, org) -> Connection:
    conn = Connection(
        organization_id=org.id, name="Verify Postgres J", category=ConnectionCategory.DATABASE,
        connection_type=ConnectionType.POSTGRESQL, config={"host": "127.0.0.1", "port": 5432},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


def _create_body(*, tally_config=None, anomaly_job_id=None, name="Tally Schedule") -> ScheduleCreateRequest:
    return ScheduleCreateRequest(
        name=name, frequency="DAILY", start_date="2026-01-01", start_time="02:00",
        tally_config=tally_config, anomaly_job_id=anomaly_job_id,
    )


# ── create_schedule / update_schedule / delete_schedule tally_config ──

async def test_create_schedule_with_tally_config_persists_and_pins_sentinel_pipeline(db, make_org):
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    body = _create_body(tally_config=TallyReconciliationScheduleConfigIn(
        connection_id=conn.id, company_name="Beta Supplies", tolerance=0.05, lookback_days=14,
    ))
    schedule = await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=body)

    assert schedule.pipeline_id == sched_svc.NON_PIPELINE_SCHEDULE_ID
    assert schedule.pipeline_name == "Tally Reconciliation: Beta Supplies"

    config = await sched_svc.get_tally_config(db, schedule.id)
    assert config is not None
    assert config.connection_id == conn.id
    assert config.company_name == "Beta Supplies"
    assert config.tolerance == 0.05
    assert config.lookback_days == 14


async def test_create_schedule_rejects_non_tally_prime_connection(db, make_org):
    org = await make_org()
    conn = await _make_non_tally_connection(db, org)
    body = _create_body(tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn.id, company_name="Beta Supplies"))
    with pytest.raises(sched_svc.ScheduleValidationError):
        await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=body)


async def test_create_schedule_rejects_connection_from_another_org(db, make_org):
    org = await make_org()
    other_org = await make_org()
    conn = await _make_tally_connection(db, other_org)
    body = _create_body(tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn.id, company_name="Beta Supplies"))
    with pytest.raises(sched_svc.ScheduleValidationError):
        await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=body)


async def test_create_schedule_rejects_tally_config_combined_with_anomaly_job_id(db, make_org):
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    body = _create_body(
        tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn.id, company_name="Beta Supplies"),
        anomaly_job_id=999,
    )
    with pytest.raises(sched_svc.ScheduleValidationError, match="pick one"):
        await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=body)


async def test_update_schedule_tally_config_updates_existing_row_not_a_duplicate(db, make_org):
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    schedule = await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=_create_body(
        tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn.id, company_name="Beta Supplies", tolerance=0.02, lookback_days=7),
    ))

    update = ScheduleUpdateRequest(tally_config=TallyReconciliationScheduleConfigIn(
        connection_id=conn.id, company_name="Beta Supplies", tolerance=0.10, lookback_days=30,
    ))
    await sched_svc.update_schedule(db, schedule, update)

    rows = (await db.execute(select(TallyReconciliationScheduleConfig).where(TallyReconciliationScheduleConfig.schedule_id == schedule.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].tolerance == 0.10
    assert rows[0].lookback_days == 30


async def test_delete_schedule_removes_tally_config_row(db, make_org):
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    schedule = await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=_create_body(
        tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn.id, company_name="Beta Supplies"),
    ))
    await sched_svc.delete_schedule(db, schedule)

    rows = (await db.execute(select(TallyReconciliationScheduleConfig).where(TallyReconciliationScheduleConfig.schedule_id == schedule.id))).scalars().all()
    assert rows == []


async def test_list_schedules_filters_by_tally_connection_id(db, make_org):
    org = await make_org()
    conn_a = await _make_tally_connection(db, org, name="Tally A")
    conn_b = await _make_tally_connection(db, org, name="Tally B")
    sched_a = await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=_create_body(
        name="Schedule A", tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn_a.id, company_name="Co A"),
    ))
    await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=_create_body(
        name="Schedule B", tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn_b.id, company_name="Co B"),
    ))

    schedules, total, _ = await sched_svc.list_schedules(db, organization_id=org.id, tally_connection_id=conn_a.id)
    assert total == 1
    assert schedules[0].id == sched_a.id


# ── execute_schedule() dispatch + run_scheduled_reconciliation() ──

async def test_run_scheduled_reconciliation_survives_extraction_failure_and_stamps_schedule_and_trigger(db, make_org):
    """No mock Tally gateway is reachable at 127.0.0.1:9999 in this test
    environment — every extraction attempt must fail gracefully (logged,
    not raised) while reconciliation itself still completes against
    whatever canonical data already exists, exactly as TALLY-J3's live
    smoke test found running against a real backend."""
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    schedule = await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=_create_body(
        tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn.id, company_name="Beta Supplies", tolerance=0.02, lookback_days=7),
    ))

    recon_run, notes = await tally_sched_recon_svc.run_scheduled_reconciliation(
        db, conn, company="Beta Supplies", tolerance=0.02, lookback_days=7, schedule_id=schedule.id, created_by=1,
    )

    assert recon_run.schedule_id == schedule.id
    assert recon_run.trigger == "Schedule"
    assert recon_run.status in (TallyReconciliationRunStatus.SUCCEEDED, TallyReconciliationRunStatus.FAILED)
    assert len(notes) == 3  # PURCHASE_VOUCHERS, PAYMENTS, PARTY_MASTER
    assert any("FAILED" in n for n in notes)  # no real gateway reachable

    extraction_runs = (await db.execute(
        select(TallyExtractionRun).where(TallyExtractionRun.schedule_id == schedule.id)
    )).scalars().all()
    assert all(r.trigger == "Schedule" for r in extraction_runs)


async def test_execute_schedule_dispatches_tally_branch_and_classifies_warning_on_exceptions(db, make_org, monkeypatch):
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    schedule = await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=_create_body(
        tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn.id, company_name="Beta Supplies"),
    ))

    async def _fake_run_scheduled_reconciliation(db_, connection, *, company, tolerance, lookback_days, schedule_id, created_by):
        recon_run = TallyReconciliationRun(
            organization_id=org.id, connection_id=connection.id, company_name=company, tolerance=tolerance,
            status=TallyReconciliationRunStatus.SUCCEEDED, started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc), source_count=5, target_count=5, matched_count=3,
            missing_count=1, extra_count=0, mismatched_count=1, ambiguous_count=0, duplicate_target_count=0,
            ledger_balance_mismatch_count=0, payment_voucher_missing_count=0,
            schedule_id=schedule_id, trigger="Schedule",
        )
        db_.add(recon_run)
        await db_.flush()
        return recon_run, ["PURCHASE_VOUCHERS: COMPLETED, 5 record(s)."]

    monkeypatch.setattr(tally_sched_recon_svc, "run_scheduled_reconciliation", _fake_run_scheduled_reconciliation)

    run = await sched_svc.execute_schedule(db, schedule, trigger="Schedule")

    assert run.status == RunStatus.WARNING  # missing_count + mismatched_count > 0
    assert run.records_processed == 5  # matched + missing + extra + mismatched
    await db.refresh(schedule)
    assert schedule.last_run_status == RunStatus.WARNING.value


async def test_execute_schedule_dispatches_tally_branch_and_classifies_success_with_no_exceptions(db, make_org, monkeypatch):
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    schedule = await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=_create_body(
        tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn.id, company_name="Beta Supplies"),
    ))

    async def _fake_clean(db_, connection, *, company, tolerance, lookback_days, schedule_id, created_by):
        recon_run = TallyReconciliationRun(
            organization_id=org.id, connection_id=connection.id, company_name=company, tolerance=tolerance,
            status=TallyReconciliationRunStatus.SUCCEEDED, started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc), source_count=3, target_count=3, matched_count=3,
            missing_count=0, extra_count=0, mismatched_count=0, ambiguous_count=0, duplicate_target_count=0,
            ledger_balance_mismatch_count=0, payment_voucher_missing_count=0,
            schedule_id=schedule_id, trigger="Schedule",
        )
        db_.add(recon_run)
        await db_.flush()
        return recon_run, []

    monkeypatch.setattr(tally_sched_recon_svc, "run_scheduled_reconciliation", _fake_clean)

    run = await sched_svc.execute_schedule(db, schedule, trigger="Schedule")
    assert run.status == RunStatus.SUCCESS


async def test_execute_schedule_dispatches_tally_branch_and_reports_failed_run(db, make_org, monkeypatch):
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    schedule = await sched_svc.create_schedule(db, organization_id=org.id, owner_id=1, body=_create_body(
        tally_config=TallyReconciliationScheduleConfigIn(connection_id=conn.id, company_name="Beta Supplies"),
    ))

    async def _fake_failed(db_, connection, *, company, tolerance, lookback_days, schedule_id, created_by):
        recon_run = TallyReconciliationRun(
            organization_id=org.id, connection_id=connection.id, company_name=company, tolerance=tolerance,
            status=TallyReconciliationRunStatus.FAILED, error_message="Simulated engine failure.",
            started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
            ledger_balance_mismatch_count=0, payment_voucher_missing_count=0,
            schedule_id=schedule_id, trigger="Schedule",
        )
        db_.add(recon_run)
        await db_.flush()
        return recon_run, []

    monkeypatch.setattr(tally_sched_recon_svc, "run_scheduled_reconciliation", _fake_failed)

    run = await sched_svc.execute_schedule(db, schedule, trigger="Schedule")
    assert run.status == RunStatus.FAILED
    assert run.error_message == "Simulated engine failure."


# ── recover_stuck_tally_extraction_runs() ──

async def test_recover_stuck_tally_extraction_runs_marks_running_rows_failed(db, make_org):
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    stuck = TallyExtractionRun(
        organization_id=org.id, connection_id=conn.id, company_name="Beta Supplies",
        extraction_type=TallyExtractionType.PURCHASE_VOUCHERS, status=TallyExtractionStatus.RUNNING,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    db.add(stuck)
    await db.commit()
    await db.refresh(stuck)

    result = await tally_extraction_service.recover_stuck_tally_extraction_runs()
    assert result["recovered_runs"] == 1

    await db.refresh(stuck)
    assert stuck.status == TallyExtractionStatus.FAILED
    assert "restart" in stuck.error_message.lower()
    assert stuck.completed_at is not None


async def test_recover_stuck_tally_extraction_runs_is_a_true_no_op_when_nothing_stuck(db, make_org):
    org = await make_org()
    conn = await _make_tally_connection(db, org)
    done = TallyExtractionRun(
        organization_id=org.id, connection_id=conn.id, company_name="Beta Supplies",
        extraction_type=TallyExtractionType.PARTY_MASTER, status=TallyExtractionStatus.COMPLETED,
    )
    db.add(done)
    await db.commit()

    result = await tally_extraction_service.recover_stuck_tally_extraction_runs()
    assert result["recovered_runs"] == 0

    await db.refresh(done)
    assert done.status == TallyExtractionStatus.COMPLETED  # untouched


# ── monitoring tally_overview() ──

async def test_monitoring_tally_overview_aggregates_extraction_reconciliation_writeback(db, make_org):
    from app.models.tally_writeback import TallyVoucherWriteBackTransaction, TallyWriteBackStatus
    from app.monitoring import repository as monitoring_repo

    org = await make_org()
    conn = await _make_tally_connection(db, org)
    since = datetime.now(timezone.utc) - timedelta(days=7)

    db.add(TallyExtractionRun(
        organization_id=org.id, connection_id=conn.id, company_name="Beta Supplies",
        extraction_type=TallyExtractionType.PURCHASE_VOUCHERS, status=TallyExtractionStatus.COMPLETED, records_fetched=12,
    ))
    db.add(TallyExtractionRun(
        organization_id=org.id, connection_id=conn.id, company_name="Beta Supplies",
        extraction_type=TallyExtractionType.PAYMENTS, status=TallyExtractionStatus.FAILED,
    ))
    manual_run = TallyReconciliationRun(
        organization_id=org.id, connection_id=conn.id, company_name="Beta Supplies", tolerance=0.02,
        status=TallyReconciliationRunStatus.SUCCEEDED, matched_count=2, missing_count=1, extra_count=0,
        mismatched_count=0, ambiguous_count=0, duplicate_target_count=0,
        ledger_balance_mismatch_count=0, payment_voucher_missing_count=0, trigger="Manual",
    )
    scheduled_run = TallyReconciliationRun(
        organization_id=org.id, connection_id=conn.id, company_name="Beta Supplies", tolerance=0.02,
        status=TallyReconciliationRunStatus.SUCCEEDED, matched_count=1, missing_count=0, extra_count=0,
        mismatched_count=0, ambiguous_count=0, duplicate_target_count=0,
        ledger_balance_mismatch_count=0, payment_voucher_missing_count=0, trigger="Schedule",
    )
    db.add(manual_run)
    db.add(scheduled_run)
    db.add(TallyVoucherWriteBackTransaction(
        organization_id=org.id, connection_id=conn.id, preview_id=1, status=TallyWriteBackStatus.SUCCEEDED,
        idempotency_key="verify-key-1", correlation_id="verify-correlation-1",
    ))
    await db.commit()

    overview = await monitoring_repo.tally_overview(db, org.id, since)

    assert overview["extraction_runs_total"] == 2
    assert overview["extraction_completed"] == 1
    assert overview["extraction_failed"] == 1
    assert overview["extraction_records_fetched"] == 12
    assert overview["reconciliation_runs_total"] == 2
    assert overview["reconciliation_succeeded"] == 2
    assert overview["scheduled_reconciliation_runs"] == 1
    assert overview["writeback_total"] == 1
    assert overview["writeback_succeeded"] == 1
