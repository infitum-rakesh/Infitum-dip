"""
tally_scheduled_reconciliation_service.py — TALLY-J batch orchestration.

The one thing a scheduled Tally reconciliation run does that a manual
POST /connections/{id}/reconciliation/runs call doesn't: refresh the data
TALLY-E's engine actually reads BEFORE reconciling, instead of assuming
whatever was extracted last time is still current. Three real
tally_extraction_service.start_extraction() calls — each already
auto-materializing into the canonical model via
tally_canonical_service.materialize_from_run() (see that function's own
call site) — feed the exact three things
tally_reconciliation_service.run_reconciliation() reads:
  - PURCHASE_VOUCHERS (INCREMENTAL, rolling window): the two-sided
    voucher diff itself.
  - PAYMENTS (INCREMENTAL, same window): TallyCanonicalVoucher rows with
    direction=PAYMENT, which the payment-voucher-existence check queries.
  - PARTY_MASTER (FULL — this extraction type has no incremental/date
    concept at all): TallyCanonicalParty.closing_balance, which the
    ledger-balance check reads.

Each extraction's own checkpoint_date (see TallyExtractionRun) already
makes every scheduled run but the first cheap — only genuinely new
vouchers/payments since the last run are re-fetched, regardless of how
lookback_days is set. A single extraction failure does not abort the
whole scheduled run: it's logged and the orchestration proceeds to
reconciliation anyway with whatever data is already on hand, the same
partial-failure-isolation convention start_extraction() itself already
applies at the chunk level.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import Connection
from app.models.tally_extraction import TallyExtractionMode, TallyExtractionType
from app.models.tally_reconciliation import TallyReconciliationRun
from app.services import tally_extraction_service, tally_reconciliation_service

_EXTRACTION_TYPES = (TallyExtractionType.PURCHASE_VOUCHERS, TallyExtractionType.PAYMENTS)


async def _refresh_extractions(
    db: AsyncSession, connection: Connection, *, company: str, lookback_days: int, actor_user_id: int | None,
    schedule_id: int,
) -> list[str]:
    """Best-effort: logs and swallows a single extraction type's failure so
    one bad call (e.g. Tally briefly unreachable) doesn't block the other
    extraction or the reconciliation itself from running against whatever
    data is already on hand."""
    notes: list[str] = []
    today = datetime.now(timezone.utc).date()
    from_date = today - timedelta(days=lookback_days)

    for extraction_type in _EXTRACTION_TYPES:
        try:
            run = await tally_extraction_service.start_extraction(
                db, connection, extraction_type=extraction_type, company=company,
                from_date=from_date, to_date=today, mode=TallyExtractionMode.INCREMENTAL,
                actor_user_id=actor_user_id, schedule_id=schedule_id, trigger="Schedule",
            )
            notes.append(f"{extraction_type.value}: {run.status.value}, {run.records_fetched} record(s).")
        except Exception as exc:  # noqa: BLE001 — one extraction type's failure must not block the others or reconciliation
            notes.append(f"{extraction_type.value}: FAILED — {exc}")

    try:
        run = await tally_extraction_service.start_extraction(
            db, connection, extraction_type=TallyExtractionType.PARTY_MASTER, company=company,
            mode=TallyExtractionMode.FULL, actor_user_id=actor_user_id, schedule_id=schedule_id, trigger="Schedule",
        )
        notes.append(f"{TallyExtractionType.PARTY_MASTER.value}: {run.status.value}, {run.records_fetched} record(s).")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"{TallyExtractionType.PARTY_MASTER.value}: FAILED — {exc}")

    return notes


async def run_scheduled_reconciliation(
    db: AsyncSession, connection: Connection, *, company: str, tolerance: float, lookback_days: int,
    schedule_id: int, created_by: int | None,
) -> tuple[TallyReconciliationRun, list[str]]:
    """The real dispatch target for a Schedule with a
    TallyReconciliationScheduleConfig row — see
    schedule_service._execute_tally_reconciliation_schedule(). Returns the
    completed run plus a human-readable log of what each extraction step
    did, for that glue function to fold into ScheduleRun.logs."""
    extraction_notes = await _refresh_extractions(
        db, connection, company=company, lookback_days=lookback_days, actor_user_id=created_by, schedule_id=schedule_id,
    )
    run = await tally_reconciliation_service.run_reconciliation(
        db, connection, company=company, tolerance=tolerance, created_by=created_by,
        schedule_id=schedule_id, trigger="Schedule",
    )
    return run, extraction_notes
