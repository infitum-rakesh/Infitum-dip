"""
Automated tests — ZIP Batch Upload chunked/resumable upload session (ZB3).
Real async SQLite DB (via conftest.py's shared fixtures), real filesystem
I/O through batch_storage_service.LocalBatchStorageProvider (pointed at a
tmp_path for this run via monkeypatching settings.upload_dir) — no mocking
of the code under test.
"""
import hashlib

import pytest

from app.models.document_batch import BatchUpload, BatchUploadStatus
from app.services import batch_upload_service
from app.services.batch_upload_service import BatchUploadError, BatchUploadErrorCode

ORG_A = 4201
ORG_B = 4202


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
    """Point every batch_storage_service call at a throwaway directory for
    this test run, instead of the real backend/uploads/ tree."""
    monkeypatch.setattr(batch_upload_service.settings, "upload_dir", str(tmp_path))


def _chunks(data: bytes, chunk_size: int) -> list[bytes]:
    return [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]


@pytest.mark.asyncio
async def test_full_chunked_upload_assembles_correctly(db):
    content = (b"real zip bytes, not actually a zip for this test - " * 5000)
    chunk_size = 8192
    parts = _chunks(content, chunk_size)

    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="batch.zip", size_bytes=len(content),
        chunk_size=chunk_size, total_chunks=len(parts), uploaded_by=1,
    )
    assert upload.status == BatchUploadStatus.INITIATED

    for i, part in enumerate(parts):
        upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=i, data=part)
    assert upload.status == BatchUploadStatus.UPLOADING
    assert sorted(upload.received_chunks) == list(range(len(parts)))

    upload, is_dup, dup_of = await batch_upload_service.complete_upload(db, upload=upload)
    assert not is_dup
    assert dup_of is None
    assert upload.status == BatchUploadStatus.COMPLETED
    assert upload.sha256 == hashlib.sha256(content).hexdigest()
    assert upload.size_bytes == len(content)

    from app.services.batch_storage_service import get_batch_storage
    storage = get_batch_storage()
    assembled = storage.assembled_path(ORG_A, upload.id)
    assert assembled.is_file()
    assert assembled.read_bytes() == content
    # chunk parts cleaned up after successful assembly
    assert storage.chunk_numbers_present(ORG_A, upload.id) == set()


@pytest.mark.asyncio
async def test_resume_after_partial_upload(db):
    content = b"X" * 50000
    chunk_size = 10000
    parts = _chunks(content, chunk_size)
    assert len(parts) == 5

    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="resume.zip", size_bytes=len(content),
        chunk_size=chunk_size, total_chunks=len(parts), uploaded_by=1,
    )
    for i in (0, 1, 3, 4):  # deliberately skip chunk 2, simulating a dropped connection
        upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=i, data=parts[i])

    status = await batch_upload_service.get_status(db, upload)
    assert status["missing_chunks"] == [2]

    with pytest.raises(BatchUploadError) as exc_info:
        await batch_upload_service.complete_upload(db, upload=upload)
    assert exc_info.value.code == BatchUploadErrorCode.MISSING_CHUNKS

    # resume: send only the missing chunk
    upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=2, data=parts[2])
    status = await batch_upload_service.get_status(db, upload)
    assert status["missing_chunks"] == []

    upload, is_dup, _ = await batch_upload_service.complete_upload(db, upload=upload)
    assert upload.status == BatchUploadStatus.COMPLETED
    assert upload.sha256 == hashlib.sha256(content).hexdigest()


@pytest.mark.asyncio
async def test_duplicate_upload_detected_by_sha256(db):
    content = b"identical archive bytes for dedup test" * 1000
    chunk_size = 16384
    parts = _chunks(content, chunk_size)

    async def _do_upload(filename):
        u = await batch_upload_service.init_upload(
            db, organization_id=ORG_A, filename=filename, size_bytes=len(content),
            chunk_size=chunk_size, total_chunks=len(parts), uploaded_by=1,
        )
        for i, part in enumerate(parts):
            u = await batch_upload_service.receive_chunk(db, upload=u, chunk_number=i, data=part)
        return await batch_upload_service.complete_upload(db, upload=u)

    first, is_dup_1, dup_of_1 = await _do_upload("first.zip")
    assert not is_dup_1 and dup_of_1 is None

    second, is_dup_2, dup_of_2 = await _do_upload("second_upload_same_bytes.zip")
    assert is_dup_2 is True
    assert dup_of_2 == first.id
    assert second.sha256 == first.sha256


@pytest.mark.asyncio
async def test_duplicate_detection_is_per_organization(db):
    """The same bytes uploaded by two different tenants must NOT be
    cross-detected as duplicates of each other — tenant isolation applies
    to dedup, not just to row visibility."""
    content = b"shared-looking content but different tenants" * 500
    chunk_size = 8192
    parts = _chunks(content, chunk_size)

    async def _do_upload(org_id):
        u = await batch_upload_service.init_upload(
            db, organization_id=org_id, filename="x.zip", size_bytes=len(content),
            chunk_size=chunk_size, total_chunks=len(parts), uploaded_by=1,
        )
        for i, part in enumerate(parts):
            u = await batch_upload_service.receive_chunk(db, upload=u, chunk_number=i, data=part)
        return await batch_upload_service.complete_upload(db, upload=u)

    _, is_dup_a, _ = await _do_upload(ORG_A)
    _, is_dup_b, _ = await _do_upload(ORG_B)
    assert not is_dup_a
    assert not is_dup_b  # different org — not a duplicate of ORG_A's upload


@pytest.mark.asyncio
async def test_checksum_mismatch_rejected(db):
    content = b"some real content" * 100
    chunk_size = 4096
    parts = _chunks(content, chunk_size)
    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="bad_checksum.zip", size_bytes=len(content),
        chunk_size=chunk_size, total_chunks=len(parts), uploaded_by=1,
    )
    for i, part in enumerate(parts):
        upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=i, data=part)

    with pytest.raises(BatchUploadError) as exc_info:
        await batch_upload_service.complete_upload(db, upload=upload, expected_sha256="0" * 64)
    assert exc_info.value.code == BatchUploadErrorCode.CHECKSUM_MISMATCH

    await db.refresh(upload)
    assert upload.status == BatchUploadStatus.FAILED
    assert upload.error_code == BatchUploadErrorCode.CHECKSUM_MISMATCH.value


@pytest.mark.asyncio
async def test_cancel_upload_removes_storage_and_blocks_further_chunks(db):
    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="cancel_me.zip", size_bytes=100,
        chunk_size=50, total_chunks=2, uploaded_by=1,
    )
    upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=0, data=b"a" * 50)

    from app.services.batch_storage_service import get_batch_storage
    storage = get_batch_storage()
    assert storage.chunk_numbers_present(ORG_A, upload.id) == {0}

    upload = await batch_upload_service.cancel_upload(db, upload)
    assert upload.status == BatchUploadStatus.CANCELLED
    assert storage.chunk_numbers_present(ORG_A, upload.id) == set()

    with pytest.raises(BatchUploadError) as exc_info:
        await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=1, data=b"b" * 50)
    assert exc_info.value.code == BatchUploadErrorCode.UPLOAD_NOT_ACCEPTING_CHUNKS


@pytest.mark.asyncio
async def test_reject_non_zip_filename(db):
    with pytest.raises(BatchUploadError) as exc_info:
        await batch_upload_service.init_upload(
            db, organization_id=ORG_A, filename="not_a_zip.txt", size_bytes=100,
            chunk_size=50, total_chunks=2, uploaded_by=1,
        )
    assert exc_info.value.code == BatchUploadErrorCode.UNSUPPORTED_ARCHIVE_TYPE


@pytest.mark.asyncio
async def test_reject_declared_size_over_configured_max(db):
    oversized = (batch_upload_service.settings.max_zip_size_mb + 1) * 1024 * 1024
    with pytest.raises(BatchUploadError) as exc_info:
        await batch_upload_service.init_upload(
            db, organization_id=ORG_A, filename="huge.zip", size_bytes=oversized,
            chunk_size=8_000_000, total_chunks=2, uploaded_by=1,
        )
    assert exc_info.value.code == BatchUploadErrorCode.ZIP_SECURITY_LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_get_upload_is_tenant_isolated(db):
    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="tenant_a_only.zip", size_bytes=100,
        chunk_size=50, total_chunks=2, uploaded_by=1,
    )
    same_org = await batch_upload_service.get_upload(db, upload.id, ORG_A)
    other_org = await batch_upload_service.get_upload(db, upload.id, ORG_B)
    assert same_org is not None and same_org.id == upload.id
    assert other_org is None


@pytest.mark.asyncio
async def test_invalid_chunk_number_rejected(db):
    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="edge.zip", size_bytes=100,
        chunk_size=50, total_chunks=2, uploaded_by=1,
    )
    with pytest.raises(BatchUploadError) as exc_info:
        await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=5, data=b"x" * 50)
    assert exc_info.value.code == BatchUploadErrorCode.INVALID_CHUNK_NUMBER
