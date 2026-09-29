"""
Automated tests — ZIP Batch Upload extraction + registration (ZB4). Real
ZIP files built on disk, run through the REAL chunked-upload service
(ZB3) and then real zip_security_service extraction — no mocking of the
code under test. Verifies exact classification counts (valid / duplicate /
unsupported / nested), sequential-duplicate-detection correctness, batch
idempotency (same ZIP twice -> same DocumentBatch), and empty-ZIP handling.
"""
import hashlib
import zipfile

import pytest

from app.models.document import Document
from app.models.document_batch import DocumentBatchFileStatus, DocumentBatchPipelineType, DocumentBatchStatus, DuplicatePolicy
from app.services import batch_upload_service, document_batch_service
from app.services.document_batch_service import DocumentBatchError, DocumentBatchErrorCode

ORG_A = 5301


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_upload_service.settings, "upload_dir", str(tmp_path))
    monkeypatch.setattr(document_batch_service.settings, "upload_dir", str(tmp_path))


async def _upload_zip_bytes(db, zip_bytes: bytes, *, org=ORG_A, filename="batch.zip", chunk_size=16384):
    chunks = [zip_bytes[i:i + chunk_size] for i in range(0, len(zip_bytes), chunk_size)] or [b""]
    upload = await batch_upload_service.init_upload(
        db, organization_id=org, filename=filename, size_bytes=len(zip_bytes),
        chunk_size=chunk_size, total_chunks=len(chunks), uploaded_by=1,
    )
    for i, part in enumerate(chunks):
        upload = await batch_upload_service.receive_chunk(db, upload=upload, chunk_number=i, data=part)
    upload, _, _ = await batch_upload_service.complete_upload(db, upload=upload)
    return upload


def _build_zip_bytes(entries: dict[str, bytes]) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_mixed_zip_classifies_every_entry_correctly(db):
    dup_content = b"this file already exists in the org"
    dup_sha = hashlib.sha256(dup_content).hexdigest()
    db.add(Document(organization_id=ORG_A, filename="existing.pdf", storage_path="/fake/existing.pdf", content_hash=dup_sha))
    await db.commit()

    inner_zip = _build_zip_bytes({"inner.txt": b"nested content"})
    zip_bytes = _build_zip_bytes({
        "invoice001.pdf": b"a genuinely new document body",
        "duplicate.pdf": dup_content,
        "malware.exe": b"unsupported extension content",
        "archives/sub.zip": inner_zip,
        "__MACOSX/._invoice001.pdf": b"apple double junk",
        ".DS_Store": b"finder junk",
    })

    upload = await _upload_zip_bytes(db, zip_bytes)
    # auto_process=False — this test checks extraction/registration
    # counters, not worker-pool processing (see test_document_batch_task_
    # executor.py for that), so it must not race a background task against
    # its own assertions.
    batch = await document_batch_service.create_batch_from_upload(
        db, upload=upload, pipeline_type=DocumentBatchPipelineType.DOCUMENT_INTELLIGENCE, auto_process=False,
    )

    assert batch.status == DocumentBatchStatus.QUEUED
    assert batch.total_entries == 4  # invoice001, duplicate, malware.exe, sub.zip — junk excluded entirely
    assert batch.valid_files == 1
    assert batch.duplicate_files == 1
    assert batch.unsupported_files == 2  # malware.exe + nested sub.zip

    files = await document_batch_service.list_batch_files(db, batch.id)
    by_path = {f.zip_relative_path: f for f in files}
    assert len(files) == 4

    assert by_path["invoice001.pdf"].status == DocumentBatchFileStatus.REGISTERED
    assert by_path["invoice001.pdf"].sha256 == hashlib.sha256(b"a genuinely new document body").hexdigest()

    assert by_path["duplicate.pdf"].status == DocumentBatchFileStatus.SKIPPED_DUPLICATE
    assert by_path["duplicate.pdf"].duplicate_of_document_id is not None

    assert by_path["malware.exe"].status == DocumentBatchFileStatus.UNSUPPORTED
    assert by_path["malware.exe"].error_code == "UNSUPPORTED_FILE_TYPE"

    assert by_path["archives/sub.zip"].status == DocumentBatchFileStatus.NESTED_ARCHIVE_NOT_SUPPORTED

    # real bytes actually landed on disk for the one valid file
    from app.services.batch_storage_service import get_batch_storage
    storage = get_batch_storage()
    extracted_root = storage.batch_extracted_root(ORG_A, batch.id)
    assert (extracted_root / "invoice001.pdf").read_bytes() == b"a genuinely new document body"
    # Duplicates ARE still extracted to disk (retention/audit — extraction
    # doesn't know about dedup, only classification does); only whether it
    # gets processed into a new Document later differs.
    assert (extracted_root / "duplicate.pdf").read_bytes() == dup_content


@pytest.mark.asyncio
async def test_process_duplicate_policy_registers_instead_of_skipping(db):
    dup_content = b"policy test duplicate content"
    dup_sha = hashlib.sha256(dup_content).hexdigest()
    db.add(Document(organization_id=ORG_A, filename="existing2.pdf", storage_path="/fake/existing2.pdf", content_hash=dup_sha))
    await db.commit()

    zip_bytes = _build_zip_bytes({"resubmit.pdf": dup_content})
    upload = await _upload_zip_bytes(db, zip_bytes, filename="policy.zip")
    batch = await document_batch_service.create_batch_from_upload(
        db, upload=upload, pipeline_type=DocumentBatchPipelineType.DOCUMENT_INTELLIGENCE,
        duplicate_policy=DuplicatePolicy.PROCESS_DUPLICATE, auto_process=False,
    )

    assert batch.valid_files == 1
    assert batch.duplicate_files == 0
    files = await document_batch_service.list_batch_files(db, batch.id)
    assert files[0].status == DocumentBatchFileStatus.REGISTERED
    assert files[0].duplicate_of_document_id is not None  # traceability preserved even though it's processed anyway


@pytest.mark.asyncio
async def test_empty_zip_completes_immediately(db):
    zip_bytes = _build_zip_bytes({})
    upload = await _upload_zip_bytes(db, zip_bytes, filename="empty.zip")
    batch = await document_batch_service.create_batch_from_upload(
        db, upload=upload, pipeline_type=DocumentBatchPipelineType.INVOICE_INTELLIGENCE,
    )
    assert batch.status == DocumentBatchStatus.COMPLETED
    assert batch.total_entries == 0
    assert batch.valid_files == 0


@pytest.mark.asyncio
async def test_reuploading_identical_zip_returns_the_same_batch(db):
    """_find_existing_batch_for_zip() only short-circuits onto a batch
    that's genuinely terminal AND not "broken" (see that function's own
    docstring for the real live bug this guards against) — a batch
    created with auto_process=False never reaches a terminal status on
    its own, so this test drives batch1's one file to a genuinely
    COMPLETED, non-broken state by hand (this file tests
    create_batch_from_upload's own dedup logic in isolation; running the
    real worker pool/OCR pipeline to reach that state is
    test_document_batch_task_executor.py's job, not this one's)."""
    zip_bytes = _build_zip_bytes({"doc.pdf": b"idempotency check content"})

    upload1 = await _upload_zip_bytes(db, zip_bytes, filename="first.zip")
    batch1 = await document_batch_service.create_batch_from_upload(
        db, upload=upload1, pipeline_type=DocumentBatchPipelineType.DOCUMENT_INTELLIGENCE, auto_process=False,
    )

    doc = Document(organization_id=ORG_A, filename="doc.pdf", storage_path="/fake/doc.pdf")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    files = await document_batch_service.list_batch_files(db, batch1.id)
    files[0].status = DocumentBatchFileStatus.COMPLETED
    files[0].document_id = doc.id
    batch1.status = DocumentBatchStatus.COMPLETED
    await db.commit()

    upload2 = await _upload_zip_bytes(db, zip_bytes, filename="second_same_bytes.zip")
    batch2 = await document_batch_service.create_batch_from_upload(
        db, upload=upload2, pipeline_type=DocumentBatchPipelineType.DOCUMENT_INTELLIGENCE, auto_process=False,
    )

    assert batch1.id == batch2.id  # no duplicate extraction/registration work happened


@pytest.mark.asyncio
async def test_zip_security_violation_fails_the_whole_batch(db):
    zip_bytes = _build_zip_bytes({"toomany1.txt": b"a", "toomany2.txt": b"b", "toomany3.txt": b"c"})
    upload = await _upload_zip_bytes(db, zip_bytes, filename="bomb.zip")

    monkeypatch_target = document_batch_service.settings
    original = monkeypatch_target.max_files_per_zip
    monkeypatch_target.max_files_per_zip = 1
    try:
        with pytest.raises(Exception):
            await document_batch_service.create_batch_from_upload(
                db, upload=upload, pipeline_type=DocumentBatchPipelineType.DOCUMENT_INTELLIGENCE,
            )
    finally:
        monkeypatch_target.max_files_per_zip = original

    # _find_existing_batch_for_zip() deliberately only matches a genuinely
    # terminal COMPLETED/COMPLETED_WITH_ERRORS batch (see its own
    # docstring) — a FAILED batch (the whole ZIP was rejected, nothing
    # usable came of it) must NOT be handed back as "already processed"
    # on a later re-upload, so it's intentionally excluded. Look the row
    # up directly instead to confirm the failure itself was persisted.
    from sqlalchemy import select as _select
    from app.models.document_batch import DocumentBatch
    result = await db.execute(
        _select(DocumentBatch).where(DocumentBatch.organization_id == ORG_A, DocumentBatch.zip_sha256 == upload.sha256)
    )
    batch = result.scalars().first()
    assert batch is not None
    assert batch.status == DocumentBatchStatus.FAILED
    assert batch.error_code is not None
    assert batch.status == DocumentBatchStatus.FAILED
    assert batch.error_code == DocumentBatchErrorCode.ZIP_EXTRACTION_FAILED.value


@pytest.mark.asyncio
async def test_create_batch_rejects_upload_not_yet_completed(db):
    upload = await batch_upload_service.init_upload(
        db, organization_id=ORG_A, filename="incomplete.zip", size_bytes=100,
        chunk_size=50, total_chunks=2, uploaded_by=1,
    )
    with pytest.raises(DocumentBatchError) as exc_info:
        await document_batch_service.create_batch_from_upload(
            db, upload=upload, pipeline_type=DocumentBatchPipelineType.DOCUMENT_INTELLIGENCE,
        )
    assert exc_info.value.code == DocumentBatchErrorCode.UPLOAD_NOT_READY
