"""
Automated tests — ZIP Batch Upload controls: pause/resume/cancel/retry-failed/
retry-selected (ZB7). Same real-DB, fake-process_document boundary as
test_document_batch_task_executor.py (see that file's docstring for why).
"""
import asyncio

import pytest

from app.models.document import DocumentStatus
from app.models.document_batch import DocumentBatchFileStatus, DocumentBatchPipelineType, DocumentBatchStatus
from app.services import batch_upload_service, document_batch_service
from app.services.document_batch_service import DocumentBatchError, DocumentBatchErrorCode

ORG_A = 7501


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
    """resume_batch()/retry_failed()/retry_selected() dispatch a real,
    fire-and-forget asyncio.create_task(_run_worker_pool(...)) — same
    production pattern as bulk_migration_service.start_run(), and same
    test pattern as test_bulk_migration.py's TestCrashRecovery (see that
    file's docstrings): let the real background task run and poll for it
    to reach a terminal status, rather than also awaiting a second,
    redundant _run_worker_pool() call that races the backgrounded one
    over the same claimable rows and can leave one of them dangling past
    this test's event loop."""
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


@pytest.mark.asyncio
async def test_pause_before_any_claim_leaves_files_untouched(db, monkeypatch):
    monkeypatch.setattr(document_batch_service.document_service, "process_document", _always_completes)

    batch = await _make_queued_batch(db, {"a.txt": b"a", "b.txt": b"b"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()

    await document_batch_service.request_pause(db, batch)
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)

    refetched = await _refetch_batch(db, batch_id)
    assert refetched.status == DocumentBatchStatus.PAUSED
    files = await _refetch_files(db, batch_id)
    for f in files:
        assert f.status == DocumentBatchFileStatus.REGISTERED
        assert f.attempt_number == 0


@pytest.mark.asyncio
async def test_resume_clears_pause_and_processing_completes(db, monkeypatch):
    monkeypatch.setattr(document_batch_service.document_service, "process_document", _always_completes)

    batch = await _make_queued_batch(db, {"a.txt": b"a", "b.txt": b"b"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await document_batch_service.request_pause(db, batch)
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)
    paused = await _refetch_batch(db, batch_id)
    assert paused.status == DocumentBatchStatus.PAUSED

    resumed = await document_batch_service.resume_batch(db, paused)
    assert resumed.pause_requested is False
    assert resumed.status == DocumentBatchStatus.PROCESSING

    final = await _wait_for_batch_status(
        db, batch_id, {DocumentBatchStatus.COMPLETED, DocumentBatchStatus.COMPLETED_WITH_ERRORS, DocumentBatchStatus.FAILED},
    )
    assert final.status == DocumentBatchStatus.COMPLETED
    assert final.completed_files == 2


@pytest.mark.asyncio
async def test_pause_rejected_on_non_processing_batch(db):
    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    with pytest.raises(DocumentBatchError) as exc_info:
        await document_batch_service.request_pause(db, batch)
    assert exc_info.value.code == DocumentBatchErrorCode.INVALID_BATCH_STATE


@pytest.mark.asyncio
async def test_resume_rejected_on_non_paused_batch(db):
    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    with pytest.raises(DocumentBatchError) as exc_info:
        await document_batch_service.resume_batch(db, batch)
    assert exc_info.value.code == DocumentBatchErrorCode.INVALID_BATCH_STATE


@pytest.mark.asyncio
async def test_cancel_queued_batch_is_immediate(db):
    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    cancelled = await document_batch_service.request_cancel(db, batch)
    assert cancelled.status == DocumentBatchStatus.CANCELLED
    assert cancelled.completed_at is not None


@pytest.mark.asyncio
async def test_cancel_processing_batch_is_cooperative(db, monkeypatch):
    monkeypatch.setattr(document_batch_service.document_service, "process_document", _always_completes)

    batch = await _make_queued_batch(db, {"a.txt": b"a", "b.txt": b"b"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()

    await document_batch_service.request_cancel(db, batch)
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)

    refetched = await _refetch_batch(db, batch_id)
    assert refetched.status == DocumentBatchStatus.CANCELLED
    files = await _refetch_files(db, batch_id)
    for f in files:
        assert f.status == DocumentBatchFileStatus.REGISTERED  # never claimed — cancel checked before the first claim


@pytest.mark.asyncio
async def test_retry_failed_resets_and_reprocesses_only_failed_files(db, monkeypatch):
    async def fake(session, doc, *, document_type=None, **kwargs):
        if doc.filename == "bad.txt":
            raise document_batch_service.document_service.DocumentValidationError("permanently bad")
        doc.status = DocumentStatus.COMPLETED
        await session.commit()
        return doc

    monkeypatch.setattr(document_batch_service.document_service, "process_document", fake)

    batch = await _make_queued_batch(db, {"good.txt": b"g", "bad.txt": b"b"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)

    after_first = await _refetch_batch(db, batch_id)
    assert after_first.status == DocumentBatchStatus.COMPLETED_WITH_ERRORS
    assert after_first.failed_files == 1
    assert after_first.completed_files == 1

    # Now let the "bad" file succeed on retry — simulating a transient
    # issue that's since been fixed (e.g. a config change), not literally
    # retrying the exact same permanent failure.
    async def fake_now_succeeds(session, doc, *, document_type=None, **kwargs):
        doc.status = DocumentStatus.COMPLETED
        await session.commit()
        return doc
    monkeypatch.setattr(document_batch_service.document_service, "process_document", fake_now_succeeds)

    retried = await document_batch_service.retry_failed(db, after_first)
    assert retried.status == DocumentBatchStatus.PROCESSING  # start_processing_for_batch flipped it

    final = await _wait_for_batch_status(
        db, batch_id, {DocumentBatchStatus.COMPLETED, DocumentBatchStatus.COMPLETED_WITH_ERRORS, DocumentBatchStatus.FAILED},
    )
    assert final.status == DocumentBatchStatus.COMPLETED
    assert final.failed_files == 0
    assert final.completed_files == 2  # good.txt untouched + bad.txt now succeeded
    files = await _refetch_files(db, batch_id)
    by_name = {f.original_filename: f for f in files}
    assert by_name["good.txt"].attempt_number == 1  # never re-run
    # retry_failed() explicitly resets attempt_number to 0 for a fresh
    # count (see its docstring) — so after one retry attempt this is 1,
    # not a running total across the original failure + the retry.
    assert by_name["bad.txt"].attempt_number == 1


@pytest.mark.asyncio
async def test_retry_failed_rejected_on_non_terminal_batch(db):
    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    with pytest.raises(DocumentBatchError) as exc_info:
        await document_batch_service.retry_failed(db, batch)
    assert exc_info.value.code == DocumentBatchErrorCode.INVALID_BATCH_STATE


@pytest.mark.asyncio
async def test_retry_selected_can_reprocess_a_completed_file(db, monkeypatch):
    monkeypatch.setattr(document_batch_service.document_service, "process_document", _always_completes)

    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)

    completed_batch = await _refetch_batch(db, batch_id)
    assert completed_batch.status == DocumentBatchStatus.COMPLETED
    files = await _refetch_files(db, batch_id)
    assert files[0].status == DocumentBatchFileStatus.COMPLETED
    assert files[0].attempt_number == 1
    file_id = files[0].id

    # _refetch_files()'s own expire_all() re-expired completed_batch (still
    # in the identity map) without reloading it — AsyncSession can't do an
    # implicit lazy-refresh outside an awaited context, so touching its
    # attributes now would raise MissingGreenlet. Re-fetch it fresh instead
    # of reusing the now-stale reference.
    completed_batch = await _refetch_batch(db, batch_id)
    retried = await document_batch_service.retry_selected(db, completed_batch, [file_id])
    assert retried.status == DocumentBatchStatus.PROCESSING  # start_processing_for_batch flipped QUEUED->PROCESSING
    assert retried.completed_files == 0  # decremented — it's being reprocessed, not counted as done anymore

    final = await _wait_for_batch_status(
        db, batch_id, {DocumentBatchStatus.COMPLETED, DocumentBatchStatus.COMPLETED_WITH_ERRORS, DocumentBatchStatus.FAILED},
    )
    assert final.status == DocumentBatchStatus.COMPLETED
    assert final.completed_files == 1
    files = await _refetch_files(db, batch_id)
    # retry_selected() resets attempt_number to 0 too (same as retry_failed()),
    # so a single re-run lands back at 1, not an accumulating total.
    assert files[0].attempt_number == 1
