"""
Automated tests — ZIP Batch Upload retention/cleanup (ZB9):
cleanup_expired_document_batches() (extracted/ file purge) and
cleanup_expired_uploads() (completed-ZIP purge + abandoned-upload cleanup).
Same real-DB, real-filesystem pattern as the rest of this feature's tests —
only timestamps are backdated directly (there's no way to actually wait
30 days in a test).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.document import DocumentStatus
from app.models.document_batch import BatchUploadStatus, DocumentBatchFileStatus, DocumentBatchPipelineType, DocumentBatchStatus
from app.services import batch_upload_service, document_batch_service
from app.services.batch_storage_service import get_batch_storage

ORG_A = 9201


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


async def _always_completes(session, doc, *, document_type=None, **kwargs):
    doc.status = DocumentStatus.COMPLETED
    await session.commit()
    return doc


@pytest.mark.asyncio
async def test_purges_extracted_files_for_old_fully_settled_batch(db, monkeypatch):
    monkeypatch.setattr(document_batch_service.settings, "extracted_file_retention_days", 30)
    monkeypatch.setattr(document_batch_service.document_service, "process_document", _always_completes)

    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)

    db.expire_all()
    settled = await db.get(document_batch_service.DocumentBatch, batch_id)
    assert settled.status == DocumentBatchStatus.COMPLETED
    settled.completed_at = datetime.now(timezone.utc) - timedelta(days=31)
    await db.commit()

    storage = get_batch_storage()
    extracted_root = storage.batch_extracted_root(ORG_A, batch_id)
    assert extracted_root.exists()
    assert (extracted_root / "a.txt").exists()

    stats = await document_batch_service.cleanup_expired_document_batches()
    assert stats["purged_batches"] == 1
    assert not extracted_root.exists()

    # DocumentBatch/DocumentBatchFile DB rows are the audit trail — never
    # touched by this cleanup.
    db.expire_all()
    still_there = await db.get(document_batch_service.DocumentBatch, batch_id)
    assert still_there is not None
    assert still_there.status == DocumentBatchStatus.COMPLETED


@pytest.mark.asyncio
async def test_skips_batch_still_within_the_retention_window(db, monkeypatch):
    monkeypatch.setattr(document_batch_service.settings, "extracted_file_retention_days", 30)
    monkeypatch.setattr(document_batch_service.document_service, "process_document", _always_completes)

    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)
    # completed_at defaults to "now" — well inside the 30-day window.

    storage = get_batch_storage()
    extracted_root = storage.batch_extracted_root(ORG_A, batch_id)
    assert extracted_root.exists()

    stats = await document_batch_service.cleanup_expired_document_batches()
    assert stats["purged_batches"] == 0
    assert extracted_root.exists()


@pytest.mark.asyncio
async def test_skips_old_batch_that_still_has_a_review_required_file(db, monkeypatch):
    monkeypatch.setattr(document_batch_service.settings, "extracted_file_retention_days", 30)

    async def _needs_review(session, doc, *, document_type=None, **kwargs):
        doc.status = DocumentStatus.NEEDS_REVIEW
        await session.commit()
        return doc
    monkeypatch.setattr(document_batch_service.document_service, "process_document", _needs_review)

    async def _noop_create_from_document(*a, **k):
        return None
    monkeypatch.setattr(document_batch_service.review_queue_service, "create_from_document", _noop_create_from_document)

    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)

    db.expire_all()
    settled = await db.get(document_batch_service.DocumentBatch, batch_id)
    assert settled.review_files == 1
    settled.completed_at = datetime.now(timezone.utc) - timedelta(days=31)
    await db.commit()

    storage = get_batch_storage()
    extracted_root = storage.batch_extracted_root(ORG_A, batch_id)

    stats = await document_batch_service.cleanup_expired_document_batches()
    assert stats["purged_batches"] == 0
    assert extracted_root.exists()  # a reviewer might still need this evidence


@pytest.mark.asyncio
async def test_skips_old_batch_that_still_has_a_retry_pending_file(db):
    """A CANCELLED batch's request_cancel() doesn't reset whatever
    REGISTERED/RETRY_PENDING file rows it already had — retention must
    check the real file rows, not just trust batch.status alone."""
    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    files = await document_batch_service.list_batch_files(db, batch.id)
    files[0].status = DocumentBatchFileStatus.RETRY_PENDING
    files[0].retry_after = datetime.now(timezone.utc) + timedelta(seconds=30)
    batch.status = DocumentBatchStatus.CANCELLED
    batch.completed_at = datetime.now(timezone.utc) - timedelta(days=31)
    await db.commit()

    storage = get_batch_storage()
    extracted_root = storage.batch_extracted_root(ORG_A, batch.id)
    assert extracted_root.exists()

    stats = await document_batch_service.cleanup_expired_document_batches()
    assert stats["purged_batches"] == 0
    assert extracted_root.exists()


@pytest.mark.asyncio
async def test_second_cleanup_run_is_a_cheap_noop(db, monkeypatch):
    monkeypatch.setattr(document_batch_service.settings, "extracted_file_retention_days", 30)
    monkeypatch.setattr(document_batch_service.document_service, "process_document", _always_completes)

    batch = await _make_queued_batch(db, {"a.txt": b"a"})
    batch_id, pipeline_type, effective_workers = batch.id, batch.pipeline_type, batch.effective_workers
    batch.status = DocumentBatchStatus.PROCESSING
    await db.commit()
    await document_batch_service._run_worker_pool(batch_id, ORG_A, pipeline_type, effective_workers)

    db.expire_all()
    settled = await db.get(document_batch_service.DocumentBatch, batch_id)
    settled.completed_at = datetime.now(timezone.utc) - timedelta(days=31)
    await db.commit()

    first = await document_batch_service.cleanup_expired_document_batches()
    assert first["purged_batches"] == 1
    second = await document_batch_service.cleanup_expired_document_batches()
    assert second["purged_batches"] == 0  # already gone — skipped via the exists() check, no error


@pytest.mark.asyncio
async def test_purges_old_completed_upload_zip_but_keeps_the_db_row(db, monkeypatch):
    monkeypatch.setattr(batch_upload_service.settings, "zip_retention_days", 30)

    zip_bytes = _build_zip_bytes({"a.txt": b"a"})
    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="old.zip", size_bytes=len(zip_bytes),
        chunk_size=65536, total_chunks=1, uploaded_by=1,
    )
    upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=0, data=zip_bytes)
    upload, _, _ = await batch_upload_service.complete_upload(db, upload=upload)
    upload_id = upload.id

    storage = get_batch_storage()
    assembled = storage.assembled_path(ORG_A, upload_id)
    assert assembled.exists()

    upload.completed_at = datetime.now(timezone.utc) - timedelta(days=31)
    await db.commit()

    stats = await batch_upload_service.cleanup_expired_uploads()
    assert stats["purged_completed_zips"] == 1
    assert not assembled.exists()

    db.expire_all()
    still_there = await db.get(batch_upload_service.BatchUpload, upload_id)
    assert still_there is not None
    assert still_there.status == BatchUploadStatus.COMPLETED
    assert still_there.sha256 is not None  # idempotency lookup data intact


@pytest.mark.asyncio
async def test_marks_abandoned_upload_failed_and_deletes_its_working_directory(db, monkeypatch):
    monkeypatch.setattr(batch_upload_service.settings, "zip_retention_days", 30)

    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="abandoned.zip", size_bytes=100,
        chunk_size=50, total_chunks=2, uploaded_by=1,
    )
    upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=0, data=b"x" * 50)
    assert upload.status == BatchUploadStatus.UPLOADING
    upload_id = upload.id

    storage = get_batch_storage()
    chunks_dir = storage._chunks_dir(ORG_A, upload_id)
    assert chunks_dir.exists()

    upload.created_at = datetime.now(timezone.utc) - timedelta(days=31)
    await db.commit()

    stats = await batch_upload_service.cleanup_expired_uploads()
    assert stats["marked_abandoned"] == 1
    assert not chunks_dir.exists()

    db.expire_all()
    final = await db.get(batch_upload_service.BatchUpload, upload_id)
    assert final.status == BatchUploadStatus.FAILED
    assert final.error_code == "UPLOAD_ABANDONED"


@pytest.mark.asyncio
async def test_leaves_recent_uploads_untouched(db, monkeypatch):
    monkeypatch.setattr(batch_upload_service.settings, "zip_retention_days", 30)

    zip_bytes = _build_zip_bytes({"a.txt": b"a"})
    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="recent.zip", size_bytes=len(zip_bytes),
        chunk_size=65536, total_chunks=1, uploaded_by=1,
    )
    upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=0, data=zip_bytes)
    upload, _, _ = await batch_upload_service.complete_upload(db, upload=upload)
    # completed_at defaults to "now" — well inside the retention window.

    storage = get_batch_storage()
    assembled = storage.assembled_path(ORG_A, upload.id)

    stats = await batch_upload_service.cleanup_expired_uploads()
    assert stats["purged_completed_zips"] == 0
    assert stats["marked_abandoned"] == 0
    assert assembled.exists()
