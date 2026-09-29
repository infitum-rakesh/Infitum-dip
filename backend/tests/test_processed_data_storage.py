"""
Automated tests for app/services/document_storage_service.py (spec item 5,
session Phase 3 / PS1-PS6) — written in Phase 7 to close the gap PS6 left:
that work was verified live via a real Docker Postgres only, never
captured as a repeatable pytest suite.

Uses a real LOCAL_FILE Connection pointed at a pytest tmp_path — a real
file write through the real connector/BulkWriter-adjacent
_push_to_file()/append_jsonl() path, not a mock of push_document() or its
helpers. No Docker dependency needed to prove the writer/config/
flatten/error-handling logic is correct; the database-writer path
(_push_to_database) reuses the SAME loading/writers/ BulkWriter machinery
already covered by test_target_load_config.py and the live Postgres
verification in PS6/Phase-7-report.
"""
import json

import pytest
from sqlalchemy import select

from app.models.connection import Connection, ConnectionCategory, ConnectionType
from app.models.document import Document, DocumentStatus, DocumentType
from app.models.document_storage import (
    DocumentDatasetType,
    DocumentProcessedDataset,
    ProcessedDataStorageLoadMode,
    ProcessedDataStorageMode,
    ProcessedDataStoragePushStatus,
)
from app.services import document_storage_service as svc


async def _make_document(db, *, organization_id: int, **overrides) -> Document:
    fields = {
        "organization_id": organization_id, "filename": "invoice.pdf", "doc_type": DocumentType.PDF,
        "storage_path": "uploads/1/invoice.pdf", "status": DocumentStatus.COMPLETED,
        "confidence": 96.5, "ocr_result": {"full_text": "Total: $100", "confidence": 94.2},
        "extraction_result": {"fields": {"total": "100.00", "vendor": "Acme Corp"}},
        "dataset": [{"total": "100.00"}], "ai_raw_response": {"raw": "model output"},
    }
    fields.update(overrides)
    doc = Document(**fields)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _make_local_file_connection(db, *, organization_id: int, base_path: str) -> Connection:
    conn = Connection(
        organization_id=organization_id, name="Test Local File", category=ConnectionCategory.STORAGE,
        connection_type=ConnectionType.LOCAL_FILE, config={"base_path": base_path},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    return conn


@pytest.mark.asyncio
async def test_default_mode_is_a_true_noop(db, make_org):
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await svc.get_or_create_config(db, org.id)
    assert config.mode == ProcessedDataStorageMode.DEFAULT

    await svc.push_document(db, doc, organization_id=org.id)

    await db.refresh(config)
    assert config.last_pushed_document_id is None
    assert config.last_status == ProcessedDataStoragePushStatus.NEVER_RUN


@pytest.mark.asyncio
async def test_external_mode_without_target_connection_is_a_noop(db, make_org):
    """EXTERNAL mode with no target_connection_id configured yet must
    also no-op, not raise — matches update_config()'s own guard that
    refuses to even SET external mode without a connection, but this is
    a defense-in-depth check on push_document() itself."""
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    config = await svc.get_or_create_config(db, org.id)
    config.mode = ProcessedDataStorageMode.EXTERNAL
    config.target_connection_id = None
    await db.commit()

    await svc.push_document(db, doc, organization_id=org.id)
    await db.refresh(config)
    assert config.last_pushed_document_id is None


@pytest.mark.asyncio
async def test_external_mode_pushes_real_file_with_correct_flattened_record(db, make_org, tmp_path):
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path=str(tmp_path))
    doc = await _make_document(db, organization_id=org.id)

    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="processed_documents.jsonl",
    )

    await svc.push_document(db, doc, organization_id=org.id)

    await db.refresh(config)
    assert config.last_status == ProcessedDataStoragePushStatus.SUCCESS
    assert config.last_error is None
    assert config.last_pushed_document_id == doc.id

    out_file = tmp_path / "processed_documents.jsonl"
    assert out_file.exists()
    lines = out_file.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["document_id"] == doc.id
    assert record["filename"] == "invoice.pdf"
    assert record["doc_type"] == "PDF"
    assert record["status"] == "COMPLETED"
    assert record["ocr_text"] == "Total: $100"
    assert record["ocr_confidence"] == 94.2
    assert json.loads(record["extraction_fields"]) == {"total": "100.00", "vendor": "Acme Corp"}
    assert json.loads(record["dataset"]) == [{"total": "100.00"}]
    # include_ai_raw_response defaults to False — must be omitted entirely.
    assert "ai_raw_response" not in record


@pytest.mark.asyncio
async def test_include_ai_raw_response_flag_gates_the_field(db, make_org, tmp_path):
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path=str(tmp_path))
    doc = await _make_document(db, organization_id=org.id)

    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="out.jsonl",
        include_ai_raw_response=True, include_dataset=False,
    )

    await svc.push_document(db, doc, organization_id=org.id)

    record = json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert json.loads(record["ai_raw_response"]) == {"raw": "model output"}
    assert "dataset" not in record


@pytest.mark.asyncio
async def test_multiple_pushes_append_rather_than_overwrite(db, make_org, tmp_path):
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path=str(tmp_path))
    doc1 = await _make_document(db, organization_id=org.id, filename="a.pdf")
    doc2 = await _make_document(db, organization_id=org.id, filename="b.pdf")

    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="out.jsonl",
    )

    await svc.push_document(db, doc1, organization_id=org.id)
    await svc.push_document(db, doc2, organization_id=org.id)

    lines = (tmp_path / "out.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    filenames = {json.loads(l)["filename"] for l in lines}
    assert filenames == {"a.pdf", "b.pdf"}


@pytest.mark.asyncio
async def test_deleted_target_connection_records_failure_never_raises(db, make_org):
    """A target connection that's been deleted out from under an EXTERNAL
    config must record last_status=FAILED with a clear error — and, per
    push_document()'s own contract, never raise past itself (document
    processing must never be retroactively broken by an export failure)."""
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path="/tmp")
    doc = await _make_document(db, organization_id=org.id)

    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="out.jsonl",
    )
    conn.is_active = False  # soft-delete, same as a real connection delete
    await db.commit()

    await svc.push_document(db, doc, organization_id=org.id)  # must not raise

    await db.refresh(config)
    assert config.last_status == ProcessedDataStoragePushStatus.FAILED
    assert "no longer exists" in config.last_error


@pytest.mark.asyncio
async def test_unsupported_connection_type_records_clear_failure(db, make_org):
    org = await make_org()
    conn = Connection(
        organization_id=org.id, name="An AI conn", category=ConnectionCategory.AI_PROVIDER,
        connection_type=ConnectionType.OPENAI, config={},
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)
    doc = await _make_document(db, organization_id=org.id)

    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="out.jsonl",
    )

    await svc.push_document(db, doc, organization_id=org.id)

    await db.refresh(config)
    assert config.last_status == ProcessedDataStoragePushStatus.FAILED
    assert "isn't a supported Processed Data Storage destination" in config.last_error


@pytest.mark.asyncio
async def test_enabling_external_mode_without_a_connection_is_rejected(db, make_org):
    from app.services.document_storage_service import DocumentStorageServiceError

    org = await make_org()
    config = await svc.get_or_create_config(db, org.id)
    with pytest.raises(DocumentStorageServiceError):
        await svc.update_config(db, config, organization_id=org.id, updated_by=None, mode="EXTERNAL")


# ── Part 5 additions: local SQLite datasets + Storage Type/Load Mode/File Format ──


async def _get_dataset(db, document_id: int, dataset_type: DocumentDatasetType) -> DocumentProcessedDataset | None:
    stmt = select(DocumentProcessedDataset).where(
        DocumentProcessedDataset.document_id == document_id, DocumentProcessedDataset.dataset_type == dataset_type.value,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


@pytest.mark.asyncio
async def test_store_local_datasets_writes_one_row_per_dataset_type(db, make_org):
    """The always-on storage_type=SQLITE path (spec item #5's "must work
    out-of-the-box" requirement) — push_document() must populate
    DocumentProcessedDataset rows even in plain DEFAULT mode, with no
    Connection configured at all."""
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)

    await svc.push_document(db, doc, organization_id=org.id)

    metadata = await _get_dataset(db, doc.id, DocumentDatasetType.METADATA)
    assert metadata is not None
    assert metadata.payload["filename"] == "invoice.pdf"

    ocr = await _get_dataset(db, doc.id, DocumentDatasetType.OCR)
    assert ocr is not None
    assert ocr.payload["full_text"] == "Total: $100"

    extraction = await _get_dataset(db, doc.id, DocumentDatasetType.AI_EXTRACTION)
    assert extraction is not None
    assert extraction.payload == {"total": "100.00", "vendor": "Acme Corp"}

    structured = await _get_dataset(db, doc.id, DocumentDatasetType.STRUCTURED_DATA)
    assert structured is not None
    assert structured.payload == [{"total": "100.00"}]

    run = await _get_dataset(db, doc.id, DocumentDatasetType.PROCESSING_RUN)
    assert run is not None


@pytest.mark.asyncio
async def test_store_local_datasets_upsert_never_touches_other_dataset_types(db, make_org):
    """Re-pushing a document (e.g. after a reprocess that only changed the
    structured dataset) must upsert ONLY the STRUCTURED_DATA row — the OCR
    row already written must be left completely untouched, since each
    dataset_type has its own row keyed on UNIQUE(document_id, dataset_type)."""
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    await svc.push_document(db, doc, organization_id=org.id)

    ocr_before = await _get_dataset(db, doc.id, DocumentDatasetType.OCR)
    assert ocr_before is not None

    doc.dataset = [{"total": "999.00"}]
    await db.commit()
    await db.refresh(doc)
    await svc.push_document(db, doc, organization_id=org.id)

    ocr_after = await _get_dataset(db, doc.id, DocumentDatasetType.OCR)
    assert ocr_after.payload == ocr_before.payload

    structured_after = await _get_dataset(db, doc.id, DocumentDatasetType.STRUCTURED_DATA)
    assert structured_after.payload == [{"total": "999.00"}]


@pytest.mark.asyncio
async def test_local_datasets_populate_alongside_external_push(db, make_org, tmp_path):
    """Local SQLite storage and an EXTERNAL push are additive, not
    either/or — store_local_datasets() runs unconditionally, before
    push_document() even checks config.mode."""
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path=str(tmp_path))
    doc = await _make_document(db, organization_id=org.id)
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="out.jsonl",
    )

    await svc.push_document(db, doc, organization_id=org.id)

    await db.refresh(config)
    assert config.last_status == ProcessedDataStoragePushStatus.SUCCESS
    metadata = await _get_dataset(db, doc.id, DocumentDatasetType.METADATA)
    assert metadata is not None


@pytest.mark.asyncio
async def test_storage_type_auto_inferred_from_local_file_connection(db, make_org, tmp_path):
    """update_config() callers that don't pass storage_type explicitly
    (the pre-Part-5 shape) still get a correct, consistent storage_type —
    inferred from the connection's own type rather than left at the
    DATABASE default, which would otherwise make a LOCAL_FILE connection
    look like an invalid Database target."""
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path=str(tmp_path))
    config = await svc.get_or_create_config(db, org.id)

    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="out.jsonl",
    )

    assert config.storage_type.value == "FILE"


@pytest.mark.asyncio
async def test_explicit_storage_type_mismatch_is_rejected(db, make_org, tmp_path):
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path=str(tmp_path))
    config = await svc.get_or_create_config(db, org.id)

    with pytest.raises(svc.DocumentStorageServiceError, match="pick a Postgres"):
        await svc.update_config(
            db, config, organization_id=org.id, updated_by=None,
            storage_type="DATABASE", target_connection_id=conn.id, target_table="out.jsonl",
        )


@pytest.mark.asyncio
async def test_file_format_csv_writes_real_csv_rows(db, make_org, tmp_path):
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path=str(tmp_path))
    doc1 = await _make_document(db, organization_id=org.id, filename="a.pdf")
    doc2 = await _make_document(db, organization_id=org.id, filename="b.pdf")
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="out.csv", file_format="CSV",
    )

    await svc.push_document(db, doc1, organization_id=org.id)
    await svc.push_document(db, doc2, organization_id=org.id)

    import csv as csv_module
    with (tmp_path / "out.csv").open(newline="") as f:
        rows = list(csv_module.reader(f))
    assert rows[0][:3] == ["document_id", "organization_id", "filename"]
    assert len(rows) == 3  # header + 2 data rows
    filenames = {row[2] for row in rows[1:]}
    assert filenames == {"a.pdf", "b.pdf"}


@pytest.mark.asyncio
async def test_file_format_parquet_writes_one_file_per_document(db, make_org, tmp_path):
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path=str(tmp_path))
    doc = await _make_document(db, organization_id=org.id)
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="processed_out", file_format="PARQUET",
    )

    await svc.push_document(db, doc, organization_id=org.id)

    import pyarrow.parquet as pq
    out_file = tmp_path / "processed_out" / f"document_{doc.id}.parquet"
    assert out_file.exists()
    table = pq.read_table(str(out_file))
    assert table.column("filename")[0].as_py() == "invoice.pdf"


@pytest.mark.asyncio
async def test_selecting_destructive_load_mode_arms_pending_reset(db, make_org, tmp_path):
    """Selecting TRUNCATE_AND_LOAD/DROP_AND_RECREATE arms a ONE-TIME
    pending_reset flag (see model docstring) rather than resetting
    immediately — the actual reset happens on the next push, and only
    once, so a long-running config doesn't re-truncate on every document."""
    org = await make_org()
    conn = await _make_local_file_connection(db, organization_id=org.id, base_path=str(tmp_path))
    config = await svc.get_or_create_config(db, org.id)
    config = await svc.update_config(
        db, config, organization_id=org.id, updated_by=None,
        mode="EXTERNAL", target_connection_id=conn.id, target_table="out.jsonl",
    )
    assert config.pending_reset is False

    config = await svc.update_config(db, config, organization_id=org.id, updated_by=None, load_mode="TRUNCATE_AND_LOAD")
    assert config.pending_reset is True
    assert config.load_mode == ProcessedDataStorageLoadMode.TRUNCATE_AND_LOAD

    config = await svc.update_config(db, config, organization_id=org.id, updated_by=None, load_mode="CREATE_IF_NOT_EXISTS")
    assert config.pending_reset is False


# ── Part 6 additions: on-demand export (export_document_datasets) ──


@pytest.mark.asyncio
async def test_export_document_datasets_returns_everything_by_default(db, make_org):
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    await svc.push_document(db, doc, organization_id=org.id)

    result = await svc.export_document_datasets(db, doc.id)

    assert set(result.keys()) == {"METADATA", "OCR", "AI_EXTRACTION", "STRUCTURED_DATA", "VALIDATION", "PROCESSING_RUN"}
    assert result["OCR"]["full_text"] == "Total: $100"


@pytest.mark.asyncio
async def test_export_document_datasets_filters_by_requested_names(db, make_org):
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    await svc.push_document(db, doc, organization_id=org.id)

    result = await svc.export_document_datasets(db, doc.id, ["ocr", "extraction"])

    assert set(result.keys()) == {"OCR", "AI_EXTRACTION"}


@pytest.mark.asyncio
async def test_export_document_datasets_rejects_unknown_dataset_name(db, make_org):
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    await svc.push_document(db, doc, organization_id=org.id)

    with pytest.raises(svc.DocumentStorageServiceError, match="Unknown dataset"):
        await svc.export_document_datasets(db, doc.id, ["not_a_real_dataset"])


@pytest.mark.asyncio
async def test_export_document_datasets_is_decoupled_from_reprocessing(db, make_org):
    """Export reflects whatever was captured the last time the document
    actually finished processing — it must NOT trigger a reprocess, and
    must keep returning the same snapshot on repeated calls."""
    org = await make_org()
    doc = await _make_document(db, organization_id=org.id)
    await svc.push_document(db, doc, organization_id=org.id)

    first = await svc.export_document_datasets(db, doc.id, ["structured_data"])
    second = await svc.export_document_datasets(db, doc.id, ["structured_data"])

    assert first == second == {"STRUCTURED_DATA": [{"total": "100.00"}]}
