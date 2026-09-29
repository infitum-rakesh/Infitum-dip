"""
Automated tests — ZIP Batch Upload crash recovery (ZB8): startup recovery
(recover_stuck_document_batches) + periodic stale-heartbeat sweep
(sweep_stale_document_batch_files). Same real-DB, fake-process_document
boundary as test_document_batch_task_executor.py (see that file's
docstring for why), and the same "simulate the orphaned DB state a crash
leaves behind, then call the recovery function directly" pattern
test_bulk_migration.py's own TestCrashRecovery uses — there's no way to
actually kill this test process mid-run and restart it, so the orphaned
PROCESSING rows a real crash would leave are constructed directly.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models.document import DocumentStatus
from app.models.document_batch import DocumentBatchFileStatus, DocumentBatchPipelineType, DocumentBatchStatus
from app.services import batch_upload_service, document_batch_service
from app.services.document_batch_service import _MAX_FILE_RETRIES

ORG_A = 8601


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_upload_service.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(document_batch_service.settings, "upload_dir", str(tmp_path))


def _build_zip_bytes(entries: dict[str, bytes]) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def _make_queued_batch(db, entries: dict[str, bytes], *, pipeline_type=DocumentBatchPipelineType.DOCUMENT_INTELLIGENCE):
    zip_bytes = _build_zip_bytes(entries)
    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="batch.zip", size_bytes=len(zip_bytes),
        chunk_size=65536, total_chunks=1, uploaded_by=1,
    )
    upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=0, data=zip_bytes)
    upload, _, _ = await batch_upload_service.complete_upload(db, upload=upload)
    return await document_batch_service.create_batch_from_upload(db, upload=upload, pipeline_type=pipeline_type, auto_process=False)


async def _refetch_batch(db, batch_id):
    db.expire_all()
    return await db.get(document_batch_service.DocumentBatch, batch_id)


async def _refetch_files(db, batch_id):
    db.expire_all()
    return await document_batch_service.list_batch_files(db, batch_id)


async def _wait_for_batch_status(db, batch_id, terminal_statuses, timeout=5.0):
    """recover_stuck_document_batches()/sweep_stale_document_batch_files()
    redispatch via a real fire-and-forget asyncio.create_task(_run_worker_
    pool(...)) — same reasoning as test_document_batch_controls.py's own
    helper of the same name: poll for the real background task to reach a
    terminal status instead of also awaiting a second, redundant
    _run_worker_pool() call that would race it over the same claimable
    rows and risk leaving one of them dangling past this test's event
    loop."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        batch = await _refetch_batch(db, batch_id)
        if batch.status in terminal_statuses:
            return batch
        if loop.time() >= deadline:
            return batch
        await asyncio.sleep(0.02)


async def _always_completes(session, doc, *, document_type=None, **kwargs):
    doc.status = DocumentStatus.COMPLETED
    await session.commit()
    return doc


async def _claim_and_orphan(db, batch, filenames: list[str], *, attempt_number: int = 1, heartbeat_age_seconds: int = 0):
    """Simulates a worker having claimed these files and then the process
    dying mid-flight — exactly the state _execute_one_file_task() leaves
    behind right after 'attempt_number += 1' but before it ever resolves
    to a terminal status. Returns the claimed DocumentBatchFile rows."""
    files = await document_batch_service.list_batch_files(db, batch.id)
    claimed = []
    now = datetime.now(timezone.utc)
    for f in files:
        if f.original_filename in filenames:
            f.status = DocumentBatchFileStatus.PROCESSING
            f.attempt_number = attempt_number
            f.worker_id = "dead-worker"
            f.claimed_at = now
            f.started_at = now
            f.last_heartbeat = now - timedelta(seconds=heartbeat_age_seconds)
            claimed.append(f)
    await db.commit()
    return claimed


@pytest.mark.asyncio
async def test_recover_stuck_batch_requeues_and_completes_with_budget_left(db, monkeypatch):
    monkeypatch.setattr(document_batch_service.document_service, "process_document", _always_completes)

    batch = await _make_queued_batch(db, {"a.txt": b"a", "b.txt": b"b"})
    batch_id = batch.id
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await _claim_and_orphan(db, batch, ["a.txt", "b.txt"], attempt_number=1)

    stats = await document_batch_service.recover_stuck_document_batches()
    assert stats["recovered_batches"] == 1
    assert stats["requeued_files"] == 2
    assert stats["failed_files"] == 0

    files = await _refetch_files(db, batch_id)
    for f in files:
        assert f.status == DocumentBatchFileStatus.RETRY_PENDING
        assert f.retry_after is not None  # real backoff window, not immediately reclaimable

    # Clear the backoff window so the redispatched pool can actually claim
    # them within this test, same trick as test_bulk_migration.py's own
    # crash-recovery test.
    for f in files:
        f.retry_after = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()

    final = await _wait_for_batch_status(
        db, batch_id, {DocumentBatchStatus.COMPLETED, DocumentBatchStatus.COMPLETED_WITH_ERRORS, DocumentBatchStatus.FAILED},
    )
    assert final.status == DocumentBatchStatus.COMPLETED
    assert final.completed_files == 2


@pytest.mark.asyncio
async def test_recover_stuck_batch_fails_files_with_exhausted_retry_budget(db):
    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    batch_id = batch.id
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await _claim_and_orphan(db, batch, ["a.txt"], attempt_number=_MAX_FILE_RETRIES)

    stats = await document_batch_service.recover_stuck_document_batches()
    assert stats["recovered_batches"] == 1
    assert stats["requeued_files"] == 0
    assert stats["failed_files"] == 1

    files = await _refetch_files(db, batch_id)
    assert files[0].status == DocumentBatchFileStatus.FAILED
    assert files[0].error_code == "WORKER_CRASHED"

    # Nothing left claimable -> finalized immediately, no redispatch needed.
    final = await _refetch_batch(db, batch_id)
    assert final.status == DocumentBatchStatus.COMPLETED_WITH_ERRORS
    assert final.failed_files == 1


@pytest.mark.asyncio
async def test_recover_stuck_batch_honors_pending_cancel_request(db):
    batch = await _make_queued_batch(db, {"a.txt": b"a", "b.txt": b"b"})
    batch_id = batch.id
    batch.status = DocumentBatchStatus.PROCESSING
    batch.cancel_requested = True
    await db.commit()
    await _claim_and_orphan(db, batch, ["a.txt"], attempt_number=1)

    stats = await document_batch_service.recover_stuck_document_batches()
    assert stats["recovered_batches"] == 1

    final = await _refetch_batch(db, batch_id)
    assert final.status == DocumentBatchStatus.CANCELLED
    assert final.completed_at is not None
    # The other, never-claimed file is untouched — cancel doesn't rewrite
    # rows that were never in-flight.
    files = await _refetch_files(db, batch_id)
    by_name = {f.original_filename: f for f in files}
    assert by_name["b.txt"].status == DocumentBatchFileStatus.REGISTERED


@pytest.mark.asyncio
async def test_recover_stuck_batch_honors_pending_pause_request(db):
    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    batch_id = batch.id
    batch.status = DocumentBatchStatus.PROCESSING
    batch.pause_requested = True
    await db.commit()
    await _claim_and_orphan(db, batch, ["a.txt"], attempt_number=1)

    stats = await document_batch_service.recover_stuck_document_batches()
    assert stats["recovered_batches"] == 1

    final = await _refetch_batch(db, batch_id)
    assert final.status == DocumentBatchStatus.PAUSED
    # Requeued (budget remained) but parked, not dispatched — resume_batch()
    # is what would pick this back up.
    files = await _refetch_files(db, batch_id)
    assert files[0].status == DocumentBatchFileStatus.RETRY_PENDING


@pytest.mark.asyncio
async def test_recover_ignores_batches_that_are_not_processing(db):
    """A QUEUED batch waiting for a manual "Start" click (auto_process=
    False) is not orphaned — recovery must never force-start it."""
    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    batch_id = batch.id
    assert batch.status == DocumentBatchStatus.QUEUED

    stats = await document_batch_service.recover_stuck_document_batches()
    assert stats["recovered_batches"] == 0

    unchanged = await _refetch_batch(db, batch_id)
    assert unchanged.status == DocumentBatchStatus.QUEUED


@pytest.mark.asyncio
async def test_sweep_only_touches_files_past_the_stale_threshold(db, monkeypatch):
    monkeypatch.setattr(document_batch_service, "_STALE_FILE_HEARTBEAT_THRESHOLD_SECONDS", 60)

    batch = await _make_queued_batch(db, {"stale.txt": b"a", "healthy.txt": b"b"})
    batch_id = batch.id
    batch.status = DocumentBatchStatus.PROCESSING  # outer pool still alive — this is the "one hung task" case
    await db.commit()
    await _claim_and_orphan(db, batch, ["stale.txt"], attempt_number=1, heartbeat_age_seconds=120)
    await _claim_and_orphan(db, batch, ["healthy.txt"], attempt_number=1, heartbeat_age_seconds=5)

    stats = await document_batch_service.sweep_stale_document_batch_files()
    assert stats["requeued_files"] == 1
    assert stats["failed_files"] == 0

    files = await _refetch_files(db, batch_id)
    by_name = {f.original_filename: f for f in files}
    assert by_name["stale.txt"].status == DocumentBatchFileStatus.RETRY_PENDING
    assert by_name["healthy.txt"].status == DocumentBatchFileStatus.PROCESSING  # untouched — heartbeat is fresh

    # healthy.txt stays claimed/PROCESSING forever in this test (there's no
    # second live worker to ever resolve it) — the batch can never reach a
    # terminal status here, so this test stops at verifying the sweep's own
    # selectivity. sweep did requeue stale.txt, which fires a real fire-
    # and-forget redispatch (asyncio.create_task) per the production
    # contract; let it run to its own natural (fast, nothing claimable
    # since stale.txt's backoff window hasn't elapsed) completion before
    # the test's event loop closes, instead of leaving it dangling.
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_sweep_ignores_files_whose_batch_is_no_longer_processing(db, monkeypatch):
    """The stale-heartbeat sweep is the 'batch is otherwise healthy, one
    task hung' case — a batch that already finished/cancelled by other
    means must not be touched here (that's recover_stuck_document_
    batches()'s job at startup, not this periodic sweep's)."""
    monkeypatch.setattr(document_batch_service, "_STALE_FILE_HEARTBEAT_THRESHOLD_SECONDS", 60)

    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    batch_id = batch.id
    batch.status = DocumentBatchStatus.CANCELLED
    await db.commit()
    await _claim_and_orphan(db, batch, ["a.txt"], attempt_number=1, heartbeat_age_seconds=120)

    stats = await document_batch_service.sweep_stale_document_batch_files()
    assert stats["requeued_files"] == 0
    assert stats["failed_files"] == 0

    files = await _refetch_files(db, batch_id)
    assert files[0].status == DocumentBatchFileStatus.PROCESSING  # untouched
