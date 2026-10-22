"""
verification_job_service.py — Batch/scheduled verification (Phase
Group 4, IV21). Mirrors bulk_migration_service.py's claim_task CAS +
fire-and-forget worker-pool shape (see that module's own docstring),
scaled to this module's much smaller volume: no dependency levels, no
per-connection semaphore, no pause/cancel (not asked for by the
approved plan — a run this short-lived doesn't need mid-flight control,
unlike a multi-hour ETL migration).

Two trigger shapes — see models/india_verification_jobs.py's own
docstring for why they're NOT symmetric:
 - create_manual_job(): real subjects + identifiers supplied by the
   caller at trigger time → real checks via verification_orchestrator.py.
 - create_refresh_job_for_expired(): a SCHEDULE-triggered sweep that
   finds results past their expires_at horizon. It CANNOT re-verify
   them — the identifier was never stored (privacy-by-design, see
   VerificationRequest.input_identifier_masked's docstring) — so it
   produces NEEDS_ATTENTION items instead: a to-do list for a human to
   re-run via the normal retry flow, never a fabricated re-verification.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database import tenant_engine_registry
from app.database.connection import AsyncSessionLocal
from app.models.india_verification import VerificationCase, VerificationCheckType, VerificationResult, VerificationUsage
from app.models.india_verification_jobs import (
    VerificationJobItem,
    VerificationJobItemStatus,
    VerificationJobRun,
    VerificationJobRunStatus,
    VerificationJobTriggerType,
)
from app.services.compliance import verification_orchestrator as orch

CLAIM_BATCH = 10
MAX_WORKERS = 3
# MCA never auto-picks an ambiguous match (needs a human), AADHAAR
# always needs fresh per-run consent, MOA is document-driven, not
# identifier-driven — none of these are safe or meaningful to batch.
BATCHABLE_CHECK_TYPES = {"PAN", "GSTIN", "TAN", "UDYAM"}

_DISPATCH = {
    "PAN": lambda db, case, identifier, connection_id: orch.run_pan_check(db, case, pan=identifier, connection_id=connection_id),
    "GSTIN": lambda db, case, identifier, connection_id: orch.run_gstin_check(db, case, gstin=identifier, connection_id=connection_id),
    "TAN": lambda db, case, identifier, connection_id: orch.run_tan_check(db, case, tan=identifier, connection_id=connection_id),
    "UDYAM": lambda db, case, identifier, connection_id: orch.run_udyam_check(db, case, udyam_number=identifier, connection_id=connection_id),
}


class VerificationJobServiceError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


async def create_manual_job(
    db: AsyncSession, organization_id: int, *, business_process: str | None, check_types: list[str],
    connection_id: int | None, subjects: list[dict], created_by: int | None,
) -> VerificationJobRun:
    """subjects: [{subject_type, subject_name, subject_id?, identifiers:
    {check_type: value}}, ...]. Dispatches asyncio.create_task(...) the
    same fire-and-forget way bulk_migration_service.start_run() does —
    the caller gets the PENDING run back immediately and polls for
    progress."""
    errors: list[str] = []
    check_types = [t.strip().upper() for t in (check_types or [])]
    unknown = [t for t in check_types if t not in BATCHABLE_CHECK_TYPES]
    if unknown:
        errors.append(
            f"Batch jobs only support {', '.join(sorted(BATCHABLE_CHECK_TYPES))} — "
            f"{', '.join(unknown)} needs its own on-demand check (MCA/MOA need a human decision point, Aadhaar needs per-run consent)."
        )
    if not check_types:
        errors.append("At least one check type is required.")
    if not subjects:
        errors.append("At least one subject is required.")
    for i, s in enumerate(subjects or []):
        if not s.get("subject_type") or not s.get("subject_name"):
            errors.append(f"Subject #{i + 1} is missing subject_type/subject_name.")
    if errors:
        raise VerificationJobServiceError(errors)

    run = VerificationJobRun(
        organization_id=organization_id, trigger_type=VerificationJobTriggerType.MANUAL, status=VerificationJobRunStatus.PENDING,
        business_process=business_process, check_types=check_types, connection_id=connection_id,
        total_items=len(subjects), created_by=created_by,
    )
    db.add(run)
    await db.flush()
    for s in subjects:
        db.add(VerificationJobItem(
            organization_id=organization_id, run_id=run.id, subject_type=s["subject_type"], subject_name=s["subject_name"],
            subject_id=s.get("subject_id"), identifiers=s.get("identifiers") or {},
        ))
    await db.commit()
    await db.refresh(run)
    asyncio.create_task(_run_worker_pool(run.id, organization_id))
    return run


async def create_refresh_job_for_expired(
    db: AsyncSession, organization_id: int, *, business_process: str, check_types: list[str] | None = None,
    schedule_id: int | None = None, created_by: int | None = None,
) -> VerificationJobRun | None:
    """Finds VerificationResult rows past expires_at for this business_
    process and records a NEEDS_ATTENTION advisory item per expired
    (case, check_type) — see this module's own docstring for why this
    never re-verifies automatically. Returns None (creates nothing) when
    there's nothing expired, so a schedule firing into a quiet period
    doesn't spam a run history with empty rows."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(VerificationResult, VerificationCase)
        .join(VerificationCase, VerificationResult.case_id == VerificationCase.id)
        .where(
            VerificationCase.organization_id == organization_id, VerificationCase.business_process == business_process,
            VerificationResult.expires_at.is_not(None), VerificationResult.expires_at <= now,
        )
    )
    if check_types:
        stmt = stmt.where(VerificationResult.check_type.in_([VerificationCheckType(t) for t in check_types]))
    rows = (await db.execute(stmt)).all()
    if not rows:
        return None

    run = VerificationJobRun(
        organization_id=organization_id, trigger_type=VerificationJobTriggerType.SCHEDULE, status=VerificationJobRunStatus.RUNNING,
        business_process=business_process, check_types=check_types or [], schedule_id=schedule_id,
        total_items=len(rows), created_by=created_by, started_at=now,
    )
    db.add(run)
    await db.flush()
    for result, case in rows:
        db.add(VerificationJobItem(
            organization_id=organization_id, run_id=run.id, subject_type=case.subject_type, subject_name=case.subject_name,
            subject_id=case.subject_id, case_id=case.id, check_type=result.check_type.value,
            status=VerificationJobItemStatus.NEEDS_ATTENTION, result_summary={result.check_type.value: "EXPIRED"},
            error_message="This check's result has expired and needs manual re-verification — the identifier is never stored, so it can't be refreshed automatically.",
            completed_at=now,
        ))
    run.needs_attention_items = len(rows)
    run.status = VerificationJobRunStatus.COMPLETED
    run.completed_at = now
    await db.commit()
    await db.refresh(run)
    return run


async def claim_task(db: AsyncSession, run_id: int, worker_id: str) -> VerificationJobItem | None:
    now = datetime.now(timezone.utc)
    candidate_ids = (await db.execute(
        select(VerificationJobItem.id)
        .where(VerificationJobItem.run_id == run_id, VerificationJobItem.status == VerificationJobItemStatus.PENDING)
        .order_by(VerificationJobItem.id).limit(CLAIM_BATCH)
    )).scalars().all()
    for cid in candidate_ids:
        result = await db.execute(
            update(VerificationJobItem)
            .where(VerificationJobItem.id == cid, VerificationJobItem.status == VerificationJobItemStatus.PENDING)
            .values(status=VerificationJobItemStatus.RUNNING, worker_id=worker_id, claimed_at=now, started_at=now)
        )
        if result.rowcount == 1:
            await db.commit()
            return await db.get(VerificationJobItem, cid)
    await db.rollback()
    return None


async def _run_one_item(db: AsyncSession, run: VerificationJobRun, item: VerificationJobItem) -> None:
    """Per-item failure isolation (spec's own "partial failure must
    never hide behind one record" requirement) — any exception here is
    caught and recorded as this ONE item's failure; it never propagates
    out to kill the worker or the rest of the batch."""
    try:
        case = await db.get(VerificationCase, item.case_id) if item.case_id else None
        if case is None:
            case = await orch.get_or_create_case(
                db, organization_id=run.organization_id, subject_type=item.subject_type, subject_name=item.subject_name,
                subject_id=item.subject_id, business_process=run.business_process,
            )
            item.case_id = case.id
            await db.flush()

        summary: dict[str, str] = {}
        any_hard_failure = False
        for check_type in run.check_types:
            identifier = (item.identifiers or {}).get(check_type)
            if not identifier:
                summary[check_type] = "SKIPPED_NO_IDENTIFIER"
                continue
            dispatch = _DISPATCH.get(check_type)
            if dispatch is None:
                summary[check_type] = "SKIPPED_UNSUPPORTED"
                continue
            result = await dispatch(db, case, identifier, run.connection_id)
            summary[check_type] = result.overall_status.value
            if result.overall_status.value == "FAILED":
                any_hard_failure = True

        item.result_summary = summary
        item.status = VerificationJobItemStatus.FAILED if any_hard_failure else VerificationJobItemStatus.SUCCESS
        if item.status == VerificationJobItemStatus.SUCCESS:
            # Scrub the plaintext identifier the moment there's no more
            # reason to keep it — a successful item is never retried, so
            # nothing should still be reading item.identifiers after this
            # point. A FAILED item's identifiers are deliberately kept a
            # little longer (so "Retry Failed" works without re-asking the
            # user) and are cleared instead by the retention sweep below.
            item.identifiers = {}
    except Exception as exc:  # noqa: BLE001 - one item's crash must never kill the batch; see module docstring
        item.status = VerificationJobItemStatus.FAILED
        item.error_message = str(exc)[:500]
    item.completed_at = datetime.now(timezone.utc)
    await db.commit()


async def _run_worker_pool(run_id: int, organization_id: int) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)

    async with sessionmaker() as db:
        run = await db.get(VerificationJobRun, run_id)
        if run is None:
            return
        run.status = VerificationJobRunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        await db.commit()

    async def _one_worker(worker_id: str) -> None:
        async with sessionmaker() as db:
            while True:
                item = await claim_task(db, run_id, worker_id)
                if item is None:
                    return
                run_row = await db.get(VerificationJobRun, run_id)
                await _run_one_item(db, run_row, item)

    await asyncio.gather(*[_one_worker(f"w{i}") for i in range(MAX_WORKERS)])

    async with sessionmaker() as db:
        run = await db.get(VerificationJobRun, run_id)
        items = (await db.execute(select(VerificationJobItem).where(VerificationJobItem.run_id == run_id))).scalars().all()
        success = sum(1 for i in items if i.status == VerificationJobItemStatus.SUCCESS)
        failed = sum(1 for i in items if i.status == VerificationJobItemStatus.FAILED)
        needs_attention = sum(1 for i in items if i.status == VerificationJobItemStatus.NEEDS_ATTENTION)
        run.success_items, run.failed_items, run.needs_attention_items = success, failed, needs_attention
        if failed == 0:
            run.status = VerificationJobRunStatus.COMPLETED
        elif failed < run.total_items:
            run.status = VerificationJobRunStatus.COMPLETED_WITH_ERRORS
        else:
            run.status = VerificationJobRunStatus.FAILED
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()


async def retry_failed_items(db: AsyncSession, run: VerificationJobRun) -> int:
    """Resets FAILED items back to PENDING and re-dispatches the worker
    pool — a fresh run of the SAME job's remaining failures, not a new
    VerificationJobRun (keeps one history row per logical batch)."""
    result = await db.execute(
        update(VerificationJobItem)
        .where(VerificationJobItem.run_id == run.id, VerificationJobItem.status == VerificationJobItemStatus.FAILED)
        .values(status=VerificationJobItemStatus.PENDING, worker_id=None, claimed_at=None, started_at=None, completed_at=None, error_message=None)
    )
    requeued = result.rowcount
    if requeued:
        run.status = VerificationJobRunStatus.PENDING
        run.failed_items = 0
        await db.commit()
        asyncio.create_task(_run_worker_pool(run.id, run.organization_id))
    else:
        await db.rollback()
    return requeued


# ── Crash recovery ─────────────────────────────────────────────────

async def recover_stuck_verification_jobs() -> dict:
    """Startup sweep — same place/shape as bulk_migration_service.
    recover_stuck_bulk_migration_tasks(), called from main.py's
    lifespan(). A run left PENDING/RUNNING by a process that died mid-
    batch gets its RUNNING items reset to PENDING and the worker pool
    re-dispatched for any run with remaining work."""
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(None)
    recovered_runs = requeued_items = 0
    async with sessionmaker() as db:
        stuck_runs = (await db.execute(
            select(VerificationJobRun).where(VerificationJobRun.status.in_([VerificationJobRunStatus.PENDING, VerificationJobRunStatus.RUNNING]))
        )).scalars().all()
        resumable: list[tuple[int, int]] = []
        for run in stuck_runs:
            result = await db.execute(
                update(VerificationJobItem)
                .where(VerificationJobItem.run_id == run.id, VerificationJobItem.status == VerificationJobItemStatus.RUNNING)
                .values(status=VerificationJobItemStatus.PENDING, worker_id=None, claimed_at=None, started_at=None)
            )
            requeued_items += result.rowcount
            recovered_runs += 1
            resumable.append((run.id, run.organization_id))
        await db.commit()
    for run_id, organization_id in resumable:
        asyncio.create_task(_run_worker_pool(run_id, organization_id))
    return {"recovered_runs": recovered_runs, "requeued_items": requeued_items}


_STALE_CLAIM_THRESHOLD_SECONDS = 300


async def sweep_stale_verification_job_items() -> dict:
    """Periodic APScheduler sweep (2 min, same cadence as bulk_migration's
    stale-task sweep) — catches a single hung worker inside an otherwise-
    healthy process: an item claimed more than 5 minutes ago inside a
    still-RUNNING run gets reset to PENDING so another worker picks it up
    on the next generation."""
    threshold_dt = datetime.now(timezone.utc) - timedelta(seconds=_STALE_CLAIM_THRESHOLD_SECONDS)
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(None)
    requeued = 0
    async with sessionmaker() as db:
        running_run_ids = (await db.execute(
            select(VerificationJobRun.id).where(VerificationJobRun.status == VerificationJobRunStatus.RUNNING)
        )).scalars().all()
        if running_run_ids:
            result = await db.execute(
                update(VerificationJobItem)
                .where(
                    VerificationJobItem.run_id.in_(running_run_ids), VerificationJobItem.status == VerificationJobItemStatus.RUNNING,
                    VerificationJobItem.claimed_at.is_not(None), VerificationJobItem.claimed_at < threshold_dt,
                )
                .values(status=VerificationJobItemStatus.PENDING, worker_id=None, claimed_at=None, started_at=None)
            )
            requeued = result.rowcount
            await db.commit()
    return {"requeued_items": requeued}


async def purge_expired_verification_data() -> dict:
    """Retention/deletion sweep (Phase Group 5, IV24) — once daily, same
    cadence as cleanup_expired_document_batches(). Two independent
    cleanups, both scoped to settings.verification_data_retention_days
    (default 90):
     1. Scrubs the plaintext `identifiers` dict off any VerificationJobItem
        still carrying one past the retention window — normally this is
        already empty (SUCCESS items are scrubbed immediately, see
        _run_one_item()); this only catches FAILED items nobody ever
        retried, so a real identifier doesn't sit in the DB forever just
        because a user walked away from a failed batch.
     2. Deletes VerificationUsage rows past the window — an append-only
        raw log (see its own docstring), safe to prune once it's aged out
        of any realistic reporting window; Monitoring/Reports aggregate
        from it at read time, not by keeping it forever.
    Scoped to the default tenant DB only, same documented limitation as
    every other retention sweep in this codebase (cleanup_expired_
    document_batches, etc.) — a BYO-database org's own DB isn't swept."""
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.verification_data_retention_days)
    scrubbed_items = 0
    purged_usage_rows = 0
    async with AsyncSessionLocal() as db:
        # Filtered on created_at alone (not "identifiers is non-empty") —
        # a JSON-column inequality predicate isn't portably expressible
        # across SQLite/Postgres, and re-writing an already-empty dict is
        # a harmless no-op, so this stays a plain age filter.
        scrub_result = await db.execute(
            update(VerificationJobItem)
            .where(VerificationJobItem.created_at < cutoff)
            .values(identifiers={})
        )
        scrubbed_items = scrub_result.rowcount
        delete_result = await db.execute(
            VerificationUsage.__table__.delete().where(VerificationUsage.created_at < cutoff)
        )
        purged_usage_rows = delete_result.rowcount
        await db.commit()
    return {"scrubbed_items": scrubbed_items, "purged_usage_rows": purged_usage_rows}
