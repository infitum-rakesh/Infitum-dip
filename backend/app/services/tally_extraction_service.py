"""
tally_extraction_service.py — TALLY-B orchestration.

Turns one "extract X from Tally" request into one or more real
TallyPrimeConnector calls, chunked by date range (Tally's Collection
export has no native pagination — see connectors/base.py's get_vouchers
docstring), bounded by settings.tally_max_concurrent_requests, persisting
every record TallyPrimeConnector returns as a TallyExtractionRecord and
advancing the run's checkpoint_date on success so the next INCREMENTAL
call for the same (connection, company, extraction_type, voucher_type)
picks up where this one left off.

A chunk that fails does not fail the whole run — the run finishes
COMPLETED_WITH_ERRORS with the successful chunks' records already
persisted, and the failure disclosed in error_message. This mirrors the
partial-failure-isolation convention already established by Bulk Object
Migration and ZIP Batch Upload elsewhere in this codebase.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, ConnectorValidationError, ConnectorUnsupportedError
from app.core.config import get_settings
from app.database.connection import AsyncSessionLocal
from app.models.connection import Connection
from app.models.tally_extraction import (
    TallyExtractionMode,
    TallyExtractionRecord,
    TallyExtractionRun,
    TallyExtractionStatus,
    TallyExtractionType,
)
from app.services import connection_service, tally_canonical_service

# One connector call spans at most this many days — keeps any single
# Tally Collection-export request (which returns its whole result set in
# one response, no server-side paging) bounded to a reasonable size.
CHUNK_DAYS = 31

_DATE_RANGED_TYPES = {
    TallyExtractionType.VOUCHERS,
    TallyExtractionType.PURCHASE_VOUCHERS,
    TallyExtractionType.SALES_VOUCHERS,
    TallyExtractionType.LEDGER,
    TallyExtractionType.PAYMENTS,
}


class TallyExtractionValidationError(Exception):
    """Request-shape errors caught before any Tally call is made (missing
    required party/date fields for the chosen extraction_type) — distinct
    from ConnectorError, which means Tally itself was reached and
    rejected/failed the request."""


def _date_chunks(from_date: date, to_date: date):
    cur = from_date
    while cur <= to_date:
        end = min(cur + timedelta(days=CHUNK_DAYS - 1), to_date)
        yield (cur, end)
        cur = end + timedelta(days=1)


def _result_key(extraction_type: TallyExtractionType) -> str:
    return {
        TallyExtractionType.VOUCHERS: "vouchers",
        TallyExtractionType.PURCHASE_VOUCHERS: "vouchers",
        TallyExtractionType.SALES_VOUCHERS: "vouchers",
        TallyExtractionType.PARTY_MASTER: "parties",
        TallyExtractionType.PAYMENTS: "payments",
        TallyExtractionType.OUTSTANDING: "outstanding",
        TallyExtractionType.BILL_ALLOCATIONS: "bill_allocations",
    }[extraction_type]


def _record_external_ref(extraction_type: TallyExtractionType, item: dict) -> str | None:
    if extraction_type in (
        TallyExtractionType.VOUCHERS, TallyExtractionType.PURCHASE_VOUCHERS,
        TallyExtractionType.SALES_VOUCHERS, TallyExtractionType.PAYMENTS,
    ):
        return item.get("voucher_number")
    if extraction_type == TallyExtractionType.PARTY_MASTER:
        return item.get("name")
    if extraction_type in (TallyExtractionType.OUTSTANDING, TallyExtractionType.BILL_ALLOCATIONS):
        return item.get("bill_name")
    return None


def _parse_tally_or_iso_date(raw) -> date | None:
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if len(s) == 8 and s.isdigit():
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return date.fromisoformat(s)
    except ValueError:
        return None
    return None


def _record_date(extraction_type: TallyExtractionType, item: dict) -> date | None:
    if extraction_type in (
        TallyExtractionType.VOUCHERS, TallyExtractionType.PURCHASE_VOUCHERS,
        TallyExtractionType.SALES_VOUCHERS, TallyExtractionType.PAYMENTS,
    ):
        return _parse_tally_or_iso_date(item.get("date"))
    if extraction_type in (TallyExtractionType.OUTSTANDING, TallyExtractionType.BILL_ALLOCATIONS):
        return _parse_tally_or_iso_date(item.get("bill_date"))
    return None


async def _latest_checkpoint(
    db: AsyncSession, connection_id: int, company: str,
    extraction_type: TallyExtractionType, voucher_type: str | None,
) -> date | None:
    stmt = (
        select(TallyExtractionRun.checkpoint_date)
        .where(
            TallyExtractionRun.connection_id == connection_id,
            TallyExtractionRun.company_name == company,
            TallyExtractionRun.extraction_type == extraction_type,
            TallyExtractionRun.voucher_type == voucher_type,
            TallyExtractionRun.checkpoint_date.is_not(None),
        )
        .order_by(TallyExtractionRun.checkpoint_date.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _connector_kwargs(
    extraction_type: TallyExtractionType, *, company: str, voucher_type: str | None,
    party: str | None, chunk_from: date | None, chunk_to: date | None,
) -> dict:
    kwargs: dict = {"company": company}
    if extraction_type == TallyExtractionType.LEDGER:
        kwargs.update(party=party, from_date=chunk_from, to_date=chunk_to)
    elif extraction_type in _DATE_RANGED_TYPES:
        kwargs.update(from_date=chunk_from, to_date=chunk_to)
        if party:
            kwargs["party"] = party
        if voucher_type and extraction_type == TallyExtractionType.VOUCHERS:
            kwargs["voucher_type"] = voucher_type
    elif extraction_type == TallyExtractionType.PARTY_MASTER:
        if voucher_type:
            kwargs["party_type"] = voucher_type
    elif extraction_type in (TallyExtractionType.OUTSTANDING, TallyExtractionType.BILL_ALLOCATIONS):
        if party:
            kwargs["party"] = party
    return kwargs


def _extraction_method(connector, extraction_type: TallyExtractionType):
    return {
        TallyExtractionType.VOUCHERS: connector.get_vouchers,
        TallyExtractionType.PURCHASE_VOUCHERS: connector.get_purchase_vouchers,
        TallyExtractionType.SALES_VOUCHERS: connector.get_sales_vouchers,
        TallyExtractionType.PARTY_MASTER: connector.get_party_master,
        TallyExtractionType.LEDGER: connector.get_ledger,
        TallyExtractionType.PAYMENTS: connector.get_payments,
        TallyExtractionType.OUTSTANDING: connector.get_outstanding,
        TallyExtractionType.BILL_ALLOCATIONS: connector.get_bill_allocations,
    }[extraction_type]


async def start_extraction(
    db: AsyncSession, connection: Connection, *, extraction_type: TallyExtractionType,
    company: str, voucher_type: str | None = None, party: str | None = None,
    from_date: date | None = None, to_date: date | None = None,
    mode: TallyExtractionMode = TallyExtractionMode.FULL, actor_user_id: int | None = None,
    schedule_id: int | None = None, trigger: str = "Manual",
) -> TallyExtractionRun:
    if extraction_type in _DATE_RANGED_TYPES and (from_date is None or to_date is None):
        raise TallyExtractionValidationError(f"{extraction_type.value} extraction requires both from_date and to_date.")
    if extraction_type == TallyExtractionType.LEDGER and not party:
        raise TallyExtractionValidationError("LEDGER extraction requires a party name.")
    if from_date and to_date and from_date > to_date:
        raise TallyExtractionValidationError("from_date must not be after to_date.")

    settings = get_settings()
    effective_from = from_date
    if mode == TallyExtractionMode.INCREMENTAL and extraction_type in _DATE_RANGED_TYPES:
        checkpoint = await _latest_checkpoint(db, connection.id, company, extraction_type, voucher_type)
        if checkpoint and checkpoint + timedelta(days=1) > effective_from:
            effective_from = checkpoint + timedelta(days=1)
        if effective_from > to_date:
            # Nothing new since the last checkpoint — a real, honest empty
            # run rather than silently skipping (the caller asked for an
            # extraction and gets a real, zero-record COMPLETED one back).
            run = TallyExtractionRun(
                organization_id=connection.organization_id, connection_id=connection.id,
                company_name=company, extraction_type=extraction_type, voucher_type=voucher_type,
                party_filter=party, from_date=effective_from, to_date=to_date, mode=mode,
                status=TallyExtractionStatus.COMPLETED, records_fetched=0, chunks_total=0,
                checkpoint_date=checkpoint, started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc), created_by=actor_user_id,
                schedule_id=schedule_id, trigger=trigger,
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)
            return run

    run = TallyExtractionRun(
        organization_id=connection.organization_id, connection_id=connection.id,
        company_name=company, extraction_type=extraction_type, voucher_type=voucher_type,
        party_filter=party, from_date=effective_from, to_date=to_date, mode=mode,
        status=TallyExtractionStatus.RUNNING, started_at=datetime.now(timezone.utc),
        created_by=actor_user_id, schedule_id=schedule_id, trigger=trigger,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    try:
        connector = connection_service.build_connector(connection)
    except ConnectorUnsupportedError as exc:
        run.status = TallyExtractionStatus.FAILED
        run.error_code, run.error_message = "CONNECTOR_UNSUPPORTED", str(exc)
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(run)
        return run

    if extraction_type in _DATE_RANGED_TYPES:
        chunks = list(_date_chunks(effective_from, to_date))
    else:
        chunks = [(None, None)]
    run.chunks_total = len(chunks)
    await db.commit()

    semaphore = asyncio.Semaphore(settings.tally_max_concurrent_requests)
    method = _extraction_method(connector, extraction_type)

    async def _run_chunk(chunk_from, chunk_to):
        kwargs = _connector_kwargs(
            extraction_type, company=company, voucher_type=voucher_type, party=party,
            chunk_from=chunk_from, chunk_to=chunk_to,
        )
        async with semaphore:
            try:
                result = await asyncio.to_thread(method, **kwargs)
                return True, result, None
            except (ConnectorError, ConnectorValidationError) as exc:
                return False, None, str(exc)

    outcomes = await asyncio.gather(*[_run_chunk(f, t) for f, t in chunks])

    records: list[tuple[dict, str | None, date | None]] = []
    for ok, result, err in outcomes:
        if not ok:
            run.chunks_failed += 1
            continue
        if extraction_type == TallyExtractionType.LEDGER:
            ledger = result.get("ledger")
            if ledger:
                records.append(({"kind": "ledger", **ledger}, ledger.get("name"), None))
            for v in result.get("vouchers", []):
                records.append((v, v.get("voucher_number"), _parse_tally_or_iso_date(v.get("date"))))
        else:
            key = _result_key(extraction_type)
            for item in result.get(key, []):
                records.append((item, _record_external_ref(extraction_type, item), _record_date(extraction_type, item)))

    for raw, ref, rdate in records:
        db.add(TallyExtractionRecord(run_id=run.id, extraction_type=extraction_type, external_ref=ref, record_date=rdate, raw_data=raw))
    run.records_fetched = len(records)

    if run.chunks_total == 0 or run.chunks_failed == 0:
        run.status = TallyExtractionStatus.COMPLETED
        if to_date:
            run.checkpoint_date = to_date
    elif run.chunks_failed < run.chunks_total:
        run.status = TallyExtractionStatus.COMPLETED_WITH_ERRORS
        run.error_message = f"{run.chunks_failed} of {run.chunks_total} date-range chunk(s) failed; records from the successful chunks were still saved."
        # Checkpoint only advances past a fully-clean run — a partial
        # failure must not silently skip the failed window on the next
        # incremental extraction.
    else:
        run.status = TallyExtractionStatus.FAILED
        run.error_message = "Every date-range chunk failed."
    run.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(run)

    # TALLY-C — best-effort canonicalization of whatever this run just
    # persisted. Never allowed to turn a successful raw extraction into a
    # failure report: the raw TallyExtractionRecord rows already
    # committed above are authoritative regardless of what happens here.
    if run.records_fetched and run.status in (TallyExtractionStatus.COMPLETED, TallyExtractionStatus.COMPLETED_WITH_ERRORS):
        try:
            await tally_canonical_service.materialize_from_run(db, run)
        except Exception:  # noqa: BLE001
            await db.rollback()

    return run


async def get_run(db: AsyncSession, run_id: int, organization_id: int | None) -> TallyExtractionRun | None:
    run = await db.get(TallyExtractionRun, run_id)
    if run is None:
        return None
    if organization_id is not None and run.organization_id != organization_id:
        return None
    return run


async def list_runs(
    db: AsyncSession, connection_id: int, organization_id: int | None, *, page: int = 1, page_size: int = 20,
) -> tuple[list[TallyExtractionRun], int]:
    filters = [TallyExtractionRun.connection_id == connection_id]
    if organization_id is not None:
        filters.append(TallyExtractionRun.organization_id == organization_id)
    total = (await db.execute(select(func.count()).select_from(TallyExtractionRun).where(*filters))).scalar_one()
    stmt = (
        select(TallyExtractionRun).where(*filters).order_by(TallyExtractionRun.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total


async def list_records(
    db: AsyncSession, run_id: int, organization_id: int | None, *, page: int = 1, page_size: int = 50,
) -> tuple[list[TallyExtractionRecord], int] | None:
    run = await get_run(db, run_id, organization_id)
    if run is None:
        return None
    total = (await db.execute(select(func.count()).select_from(TallyExtractionRecord).where(TallyExtractionRecord.run_id == run_id))).scalar_one()
    stmt = (
        select(TallyExtractionRecord).where(TallyExtractionRecord.run_id == run_id).order_by(TallyExtractionRecord.id)
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = list((await db.execute(stmt)).scalars().all())
    return items, total


async def recover_stuck_tally_extraction_runs() -> dict:
    """TALLY-J startup recovery — same place/shape as
    schedule_service.recover_stuck_document_schedules() and
    bulk_migration_service.recover_stuck_bulk_migration_tasks(). A
    TallyExtractionRun commits its RUNNING row before doing any real work
    (see start_extraction() above), so a backend restart mid-extraction
    genuinely leaves one stuck at RUNNING forever — unlike
    TallyReconciliationRun, whose row only survives a crash if the whole
    run already committed (flush-only until then), so it needs no
    equivalent recovery here. A stuck run is never silently re-run: it is
    marked FAILED with a clear reason, and the next scheduled/manual
    extraction naturally starts from its own real checkpoint. Runs against
    the platform/default tenant DB only (AsyncSessionLocal), the same
    disclosed scope every other startup recovery function in this codebase
    already has."""
    recovered = 0
    async with AsyncSessionLocal() as db:
        stuck_runs = (await db.execute(
            select(TallyExtractionRun).where(TallyExtractionRun.status == TallyExtractionStatus.RUNNING)
        )).scalars().all()
        for run in stuck_runs:
            run.status = TallyExtractionStatus.FAILED
            run.error_message = "Interrupted by a backend restart — the next extraction will resume from this run's last real checkpoint."
            run.completed_at = datetime.now(timezone.utc)
            recovered += 1
        if recovered:
            await db.commit()
    return {"recovered_runs": recovered}
