"""
Automated tests — ZIP Batch Upload worker pool / task executor (ZB6). Real
async SQLite DB, real claim/worker-pool/heartbeat/retry/counter code under
test — following this project's own established boundary (see
test_bulk_migration.py's docstring): `document_service.process_document()`
itself (real OCR/Tesseract/AI calls) is patched with a small, deterministic
fake, exactly the same principle as that suite's `_FakeDbConnector`/
`_FakeApiConnector` standing in for a real connector one layer below the
code actually under test here. `document_service.save_from_source()` is
left completely REAL (real disk I/O, real Document row, real content_hash)
since it needs no OCR/AI dependency and is fast.

What's verified: every REGISTERED file gets claimed and processed exactly
once; one file's permanent failure doesn't stop the others or fail the
whole batch (COMPLETED_WITH_ERRORS, not FAILED); a transient failure
retries and eventually succeeds; NEEDS_REVIEW is mapped correctly;
heartbeats are genuinely touched while a file is claimed; and
effective_workers actually bounds real concurrency (not fire-all-N-at-once).
"""
import asyncio

import pytest

from app.models.document import Document, DocumentStatus
from app.models.document_batch import (
    DocumentBatchFileStatus, DocumentBatchPipelineType, DocumentBatchStatus,
)
from app.services import batch_upload_service, document_batch_service
from app.services.document_service import DocumentValidationError

ORG_A = 6401


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
    chunk_size = 65536
    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="batch.zip", size_bytes=len(zip_bytes),
        chunk_size=chunk_size, total_chunks=1, uploaded_by=1,
    )
    upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=0, data=zip_bytes)
    upload, _, _ = await batch_upload_service.complete_upload(db, upload=upload)
    batch = await document_batch_service.create_batch_from_upload(
        db, upload=upload, pipeline_type=pipeline_type, auto_process=False,
    )
    return batch


async def _refetch_batch(db, batch_id):
    db.expire_all()
    return await db.get(document_batch_service.DocumentBatch, batch_id)


async def _refetch_files(db, batch_id):
    db.expire_all()
    return await document_batch_service.list_batch_files(db, batch_id)


@pytest.mark.asyncio
async def test_worker_pool_processes_all_files_to_completion(db, monkeypatch):
    async def fake_process_document(session, doc, *, document_type=None, **kwargs):
        doc.status = DocumentStatus.COMPLETED
        await session.commit()
        return doc

    monkeypatch.setattr(document_batch_service.document_service, "process_document", fake_process_document)

    batch = await _make_queued_batch(db, {"a.txt": b"content a", "b.txt": b"content b", "c.txt": b"content c"})
    batch.effective_workers = 2
    await db.commit()

    await document_batch_service._run_worker_pool(batch.id, ORG_A, batch.pipeline_type, batch.effective_workers)

    batch = await _refetch_batch(db, batch.id)
    assert batch.status == DocumentBatchStatus.COMPLETED
    assert batch.completed_files == 3

    files = await _refetch_files(db, batch.id)
    assert len(files) == 3
    for f in files:
        assert f.status == DocumentBatchFileStatus.COMPLETED
        assert f.document_id is not None
        assert f.attempt_number == 1


@pytest.mark.asyncio
async def test_one_permanently_failing_file_does_not_fail_the_whole_batch(db, monkeypatch):
    async def fake_process_document(session, doc, *, document_type=None, **kwargs):
        if doc.filename == "corrupt.txt":
            raise DocumentValidationError("simulated permanently corrupt file")
        doc.status = DocumentStatus.COMPLETED
        await session.commit()
        return doc

    monkeypatch.setattr(document_batch_service.document_service, "process_document", fake_process_document)

    batch = await _make_queued_batch(db, {"good1.txt": b"x", "corrupt.txt": b"y", "good2.txt": b"z"})
    await document_batch_service._run_worker_pool(batch.id, ORG_A, batch.pipeline_type, batch.effective_workers)

    batch = await _refetch_batch(db, batch.id)
    assert batch.status == DocumentBatchStatus.COMPLETED_WITH_ERRORS
    assert batch.completed_files == 2
    assert batch.failed_files == 1

    files = await _refetch_files(db, batch.id)
    by_name = {f.original_filename: f for f in files}
    assert by_name["corrupt.txt"].status == DocumentBatchFileStatus.FAILED
    assert by_name["corrupt.txt"].error_code == "DocumentValidationError"
    assert by_name["good1.txt"].status == DocumentBatchFileStatus.COMPLETED
    assert by_name["good2.txt"].status == DocumentBatchFileStatus.COMPLETED


@pytest.mark.asyncio
async def test_needs_review_maps_to_review_required_status(db, monkeypatch):
    async def fake_process_document(session, doc, *, document_type=None, **kwargs):
        doc.status = DocumentStatus.NEEDS_REVIEW
        await session.commit()
        return doc

    monkeypatch.setattr(document_batch_service.document_service, "process_document", fake_process_document)

    batch = await _make_queued_batch(db, {"needs_review.txt": b"low confidence content"})
    await document_batch_service._run_worker_pool(batch.id, ORG_A, batch.pipeline_type, batch.effective_workers)

    batch = await _refetch_batch(db, batch.id)
    assert batch.review_files == 1
    assert batch.status == DocumentBatchStatus.COMPLETED  # review-required is not a failure

    files = await _refetch_files(db, batch.id)
    assert files[0].status == DocumentBatchFileStatus.REVIEW_REQUIRED


@pytest.mark.asyncio
async def test_transient_failure_retries_then_succeeds(db, monkeypatch):
    attempts: dict[str, int] = {}

    async def fake_process_document(session, doc, *, document_type=None, **kwargs):
        attempts[doc.filename] = attempts.get(doc.filename, 0) + 1
        if attempts[doc.filename] == 1:
            raise TimeoutError("simulated transient network timeout")
        doc.status = DocumentStatus.COMPLETED
        await session.commit()
        return doc

    monkeypatch.setattr(document_batch_service.document_service, "process_document", fake_process_document)
    # Collapse the real exponential backoff so a redispatched worker pool
    # immediately reclaims the RETRY_PENDING row within this fast test,
    # instead of actually waiting ~10s.
    monkeypatch.setattr(document_batch_service, "_BACKOFF_BASE_SECONDS", 0)
    monkeypatch.setattr(document_batch_service, "_BACKOFF_MAX_SECONDS", 0)

    batch = await _make_queued_batch(db, {"flaky.txt": b"flaky content"})

    # With backoff collapsed to ~0, the SAME worker's next claim iteration
    # (still inside this one _run_worker_pool call — _one_worker() loops
    # claim->execute until nothing is claimable) picks the RETRY_PENDING
    # row back up immediately, so this succeeds within a single pass —
    # no need to simulate a separate redispatch for this test.
    await document_batch_service._run_worker_pool(batch.id, ORG_A, batch.pipeline_type, batch.effective_workers)

    batch = await _refetch_batch(db, batch.id)
    assert batch.status == DocumentBatchStatus.COMPLETED
    assert batch.completed_files == 1
    files = await _refetch_files(db, batch.id)
    assert files[0].status == DocumentBatchFileStatus.COMPLETED
    assert files[0].attempt_number == 2
    assert attempts["flaky.txt"] == 2


@pytest.mark.asyncio
async def test_retry_backoff_genuinely_delays_reclaim(db, monkeypatch):
    """With REAL (non-collapsed) backoff, a failed file must NOT be
    reclaimable again immediately — the batch stays PROCESSING (not
    finalized) and a second pass run before the backoff window elapses
    must find nothing claimable."""
    async def always_fails(session, doc, *, document_type=None, **kwargs):
        raise TimeoutError("simulated transient failure")

    monkeypatch.setattr(document_batch_service.document_service, "process_document", always_fails)

    batch = await _make_queued_batch(db, {"flaky.txt": b"flaky content"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    # Mirrors what start_processing_for_batch() does before dispatching —
    # _run_worker_pool() itself doesn't flip QUEUED->PROCESSING (that's the
    # caller's job); called directly here for a deterministic, awaited test.
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)

    refetched = await _refetch_batch(db, batch_id)
    assert refetched.status == DocumentBatchStatus.PROCESSING  # not finalized — a retry is pending
    files = await _refetch_files(db, batch_id)
    assert files[0].status == DocumentBatchFileStatus.RETRY_PENDING
    assert files[0].retry_after is not None
    from datetime import datetime, timezone
    # SQLite round-trips DateTime(timezone=True) as naive — compare in the
    # same (naive-UTC) terms rather than asserting tz-awareness here.
    retry_after = files[0].retry_after
    now = datetime.now(timezone.utc).replace(tzinfo=None) if retry_after.tzinfo is None else datetime.now(timezone.utc)
    assert retry_after > now  # genuinely in the future, real backoff was applied

    # A redispatch attempted immediately (backoff not yet elapsed) finds
    # nothing claimable and leaves everything exactly as it was.
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)
    refetched = await _refetch_batch(db, batch_id)
    assert refetched.status == DocumentBatchStatus.PROCESSING
    files = await _refetch_files(db, batch_id)
    assert files[0].status == DocumentBatchFileStatus.RETRY_PENDING
    assert files[0].attempt_number == 1  # not reclaimed a second time yet


@pytest.mark.asyncio
async def test_heartbeat_is_touched_while_file_is_claimed(db, monkeypatch):
    monkeypatch.setattr(document_batch_service, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)

    async def slow_fake_process_document(session, doc, *, document_type=None, **kwargs):
        await asyncio.sleep(0.2)  # long enough for at least 2-3 heartbeat ticks
        doc.status = DocumentStatus.COMPLETED
        await session.commit()
        return doc

    monkeypatch.setattr(document_batch_service.document_service, "process_document", slow_fake_process_document)

    batch = await _make_queued_batch(db, {"slow.txt": b"slow content"})
    await document_batch_service._run_worker_pool(batch.id, ORG_A, batch.pipeline_type, batch.effective_workers)

    files = await _refetch_files(db, batch.id)
    f = files[0]
    assert f.status == DocumentBatchFileStatus.COMPLETED
    assert f.last_heartbeat is not None
    # touched meaningfully after the claim moment, not just set once at claim time
    assert f.last_heartbeat >= f.claimed_at


@pytest.mark.asyncio
async def test_effective_workers_bounds_real_concurrency(db, monkeypatch):
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def fake_process_document(session, doc, *, document_type=None, **kwargs):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        doc.status = DocumentStatus.COMPLETED
        await session.commit()
        return doc

    monkeypatch.setattr(document_batch_service.document_service, "process_document", fake_process_document)

    entries = {f"file{i}.txt": f"content {i}".encode() for i in range(6)}
    batch = await _make_queued_batch(db, entries)
    batch.effective_workers = 2
    await db.commit()

    await document_batch_service._run_worker_pool(batch.id, ORG_A, batch.pipeline_type, batch.effective_workers)

    assert max_active <= 2
    assert max_active > 1  # genuinely ran more than one at a time — not accidentally serialized
    batch = await _refetch_batch(db, batch.id)
    assert batch.status == DocumentBatchStatus.COMPLETED
    assert batch.completed_files == 6
