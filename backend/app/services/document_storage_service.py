"""
document_storage_service — configurable "Processed Data Storage"
destination for Document Intelligence output. See models/document_storage.py's
module docstring for how this differs from Bring-Your-Own-Database.

get_or_create_config()/update_config() are a plain settings CRUD, same
shape as settings_service.py's OrganizationSettings. push_document() is
the real work: flattens one Document's processed output into a single
record and writes it to the org's configured Connection via the SAME
Connection/connector/BulkWriter machinery the rest of this codebase
already uses for real ETL (loading/writers/registry.py already has a
real, registered BulkWriter for every one of Postgres/MySQL/SQL Server/
Oracle/Snowflake — see that file's own comments on Snowflake's
disclosed-unverified status) plus a small local-file destination via
LocalFileConnector.append_jsonl().

push_document() NEVER raises past itself — see its own docstring. A
broken/misconfigured external destination must never fail the document's
own Document Intelligence processing, which already fully succeeded by
the time this runs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorUnsupportedError, scrub_secrets
from app.loading.writers.base import normalize_identifier, write_batch_with_fallback
from app.loading.writers.registry import get_bulk_writer
from app.models.connection import Connection
from app.models.document import Document
from app.models.document_storage import (
    DocumentDatasetType,
    DocumentProcessedDataset,
    ProcessedDataStorageConfig,
    ProcessedDataStorageFileFormat,
    ProcessedDataStorageLoadMode,
    ProcessedDataStorageMode,
    ProcessedDataStoragePushStatus,
    ProcessedDataStorageType,
)
from app.services import connection_service

logger = logging.getLogger(__name__)


class DocumentStorageServiceError(Exception):
    """Bad request — e.g. a target connection that doesn't exist or isn't a supported type."""


async def get_or_create_config(db: AsyncSession, organization_id: int | None) -> ProcessedDataStorageConfig:
    stmt = select(ProcessedDataStorageConfig)
    stmt = stmt.where(ProcessedDataStorageConfig.organization_id.is_(None)) if organization_id is None \
        else stmt.where(ProcessedDataStorageConfig.organization_id == organization_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row
    row = ProcessedDataStorageConfig(organization_id=organization_id)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_config(
    db: AsyncSession, row: ProcessedDataStorageConfig, *, organization_id: int | None, updated_by: int | None,
    mode: str | None = None, storage_type: str | None = None, load_mode: str | None = None,
    file_format: str | None = None, target_connection_id: int | None = ..., target_schema: str | None = ...,
    target_table: str | None = None, include_ocr_text: bool | None = None, include_extraction_fields: bool | None = None,
    include_dataset: bool | None = None, include_ai_raw_response: bool | None = None,
) -> ProcessedDataStorageConfig:
    """target_connection_id/target_schema default to Ellipsis (not None)
    so "explicitly clear this field" (None) is distinguishable from
    "caller didn't send this field at all" (unset) — same convention
    reconciliation_service.update_job() uses `is not None` for, except
    here None IS a valid, meaningful value (no target connection / no
    schema) so a plain default of None would make clearing the field
    impossible to distinguish from leaving it untouched."""
    if mode is not None:
        new_mode = ProcessedDataStorageMode(mode)
        if new_mode == ProcessedDataStorageMode.EXTERNAL:
            conn_id = row.target_connection_id if target_connection_id is ... else target_connection_id
            if not conn_id:
                raise DocumentStorageServiceError("Select a target connection before enabling External storage.")
        row.mode = new_mode
    if storage_type is not None:
        row.storage_type = ProcessedDataStorageType(storage_type)
    if target_connection_id is not ...:
        if target_connection_id is not None:
            conn = await connection_service.get_connection(db, target_connection_id, organization_id)
            if conn is None:
                raise DocumentStorageServiceError(f"Connection {target_connection_id} was not found.")
            conn_type = conn.connection_type.value
            if storage_type is not None:
                # Caller explicitly chose a Storage Type this call — cross-
                # check it against the connection's real type so a
                # FILE-storage config can't silently point at a Postgres
                # connection or vice versa (the push would otherwise fail
                # deep inside _push_to_database()/_push_to_file() with a far
                # less clear error).
                effective_type = row.storage_type
                if effective_type == ProcessedDataStorageType.DATABASE and conn_type not in _DATABASE_TYPES:
                    raise DocumentStorageServiceError(
                        f"Storage Type is Database, but connection '{conn.name}' is a {conn_type.replace('_', ' ').title()} "
                        f"connection — pick a Postgres/MySQL/SQL Server/Oracle/Snowflake connection instead."
                    )
                if effective_type == ProcessedDataStorageType.FILE and conn_type not in _FILE_TYPES:
                    raise DocumentStorageServiceError(
                        f"Storage Type is File, but connection '{conn.name}' is a {conn_type.replace('_', ' ').title()} "
                        f"connection — pick a Local File connection instead."
                    )
            else:
                # Caller didn't specify Storage Type this call (e.g. the
                # pre-Part-5 update_config() callers, and the frontend's
                # existing "just pick a connection" flow) — infer it from
                # the connection itself rather than blocking on the row's
                # untouched DATABASE default. A connection type this feature
                # doesn't support at all (e.g. an AI_PROVIDER connection)
                # deliberately falls through unvalidated here: push_document()
                # already reports that case clearly via its own
                # _DATABASE_TYPES/_FILE_TYPES dispatch (ConnectorUnsupportedError).
                if conn_type in _DATABASE_TYPES:
                    row.storage_type = ProcessedDataStorageType.DATABASE
                elif conn_type in _FILE_TYPES:
                    row.storage_type = ProcessedDataStorageType.FILE
        row.target_connection_id = target_connection_id
    if target_schema is not ...:
        row.target_schema = target_schema
    if target_table is not None:
        cleaned = target_table.strip()
        if not cleaned:
            raise DocumentStorageServiceError("Target table cannot be blank.")
        if cleaned != row.target_table:
            row.pending_reset = row.load_mode in (ProcessedDataStorageLoadMode.TRUNCATE_AND_LOAD, ProcessedDataStorageLoadMode.DROP_AND_RECREATE)
        row.target_table = cleaned
    if load_mode is not None:
        new_load_mode = ProcessedDataStorageLoadMode(load_mode)
        # Re-selecting (or re-saving) a destructive mode arms a ONE-TIME
        # reset on the next push — see model docstring/pending_reset and
        # _apply_load_mode_reset() below. A non-destructive mode disarms it.
        row.pending_reset = new_load_mode in (ProcessedDataStorageLoadMode.TRUNCATE_AND_LOAD, ProcessedDataStorageLoadMode.DROP_AND_RECREATE)
        row.load_mode = new_load_mode
    if file_format is not None:
        row.file_format = ProcessedDataStorageFileFormat(file_format)
    if include_ocr_text is not None:
        row.include_ocr_text = include_ocr_text
    if include_extraction_fields is not None:
        row.include_extraction_fields = include_extraction_fields
    if include_dataset is not None:
        row.include_dataset = include_dataset
    if include_ai_raw_response is not None:
        row.include_ai_raw_response = include_ai_raw_response
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)
    return row


_EXPORT_DATASET_ALIASES = {
    "metadata": DocumentDatasetType.METADATA,
    "ocr": DocumentDatasetType.OCR,
    "extraction": DocumentDatasetType.AI_EXTRACTION,
    "ai_extraction": DocumentDatasetType.AI_EXTRACTION,
    "structured_data": DocumentDatasetType.STRUCTURED_DATA,
    "dataset": DocumentDatasetType.STRUCTURED_DATA,
    "validation": DocumentDatasetType.VALIDATION,
    "processing_run": DocumentDatasetType.PROCESSING_RUN,
}


async def export_document_datasets(db: AsyncSession, document_id: int, dataset_names: list[str] | None = None) -> dict:
    """spec item #6's on-demand export (api/documents.py's POST
    /{document_id}/export, destination="inline") — reads from the
    ALREADY-PERSISTED DocumentProcessedDataset rows store_local_datasets()
    writes, never triggers a reprocess. This is what "decoupled from
    reprocessing" actually means: callers get whatever was captured the
    last time the document finished processing, on demand, as many times
    as asked, independent of that processing run's own lifecycle.

    dataset_names=None returns every dataset type this document has a row
    for; otherwise a subset of _EXPORT_DATASET_ALIASES' keys. Raises
    DocumentStorageServiceError for an unrecognized name."""
    if dataset_names is None:
        wanted_types = None
    else:
        wanted_types = []
        for name in dataset_names:
            dataset_type = _EXPORT_DATASET_ALIASES.get(name.strip().lower())
            if dataset_type is None:
                raise DocumentStorageServiceError(f"Unknown dataset '{name}' — choose from {sorted(_EXPORT_DATASET_ALIASES)}.")
            wanted_types.append(dataset_type)

    stmt = select(DocumentProcessedDataset).where(DocumentProcessedDataset.document_id == document_id)
    if wanted_types is not None:
        stmt = stmt.where(DocumentProcessedDataset.dataset_type.in_([t.value for t in wanted_types]))
    rows = (await db.execute(stmt)).scalars().all()
    return {row.dataset_type: row.payload for row in rows}


async def test_target_connection(db: AsyncSession, row: ProcessedDataStorageConfig, organization_id: int | None) -> dict:
    if not row.target_connection_id:
        raise DocumentStorageServiceError("No target connection is configured.")
    conn = await connection_service.get_connection(db, row.target_connection_id, organization_id)
    if conn is None:
        raise DocumentStorageServiceError(f"Connection {row.target_connection_id} was not found.")
    return await connection_service.test_connection(db, conn)


# ── Push ─────────────────────────────────────────────────────────

def _flatten_document(doc: Document, config: ProcessedDataStorageConfig) -> dict:
    """One row's worth of a document's processed output, gated by the
    config's include_* flags. JSON sub-structures are serialized to text
    (json.dumps) rather than passed through as nested dicts — every
    target here (relational DB columns, one JSONL line) wants a flat
    record, and a raw dict value would either break DataFrame->SQL typing
    or double-encode when the JSONL writer itself calls json.dumps again."""
    record: dict = {
        "document_id": doc.id,
        "organization_id": doc.organization_id,
        "filename": doc.filename,
        "doc_type": doc.doc_type.value if doc.doc_type else None,
        "status": doc.status.value if doc.status else None,
        "confidence": doc.confidence,
        "processed_at": (doc.processing_completed_at or datetime.now(timezone.utc)).isoformat(),
        "pushed_at": datetime.now(timezone.utc).isoformat(),
    }
    if config.include_ocr_text:
        record["ocr_text"] = (doc.ocr_result or {}).get("full_text")
        record["ocr_confidence"] = (doc.ocr_result or {}).get("confidence")
    if config.include_extraction_fields:
        fields = (doc.extraction_result or {}).get("fields")
        record["extraction_fields"] = json.dumps(fields, default=str) if fields is not None else None
    if config.include_dataset:
        record["dataset"] = json.dumps(doc.dataset, default=str) if doc.dataset is not None else None
    if config.include_ai_raw_response:
        record["ai_raw_response"] = json.dumps(doc.ai_raw_response, default=str) if doc.ai_raw_response is not None else None
    return record


def _apply_load_mode_reset(connector, connection_type: str, config: ProcessedDataStorageConfig) -> None:
    """Runs the ONE-TIME truncate/drop a TRUNCATE_AND_LOAD/DROP_AND_RECREATE
    load_mode arms (see model docstring/pending_reset) — called with
    pending_reset already confirmed True by the caller, right before the
    normal ensure_table()/write path runs for this push. Uses the
    connector's own real SQLAlchemy engine directly (every _DATABASE_TYPES
    connector is a SqlAlchemyDatabaseConnector) since BaseBulkWriter has no
    TRUNCATE/DROP primitive of its own — deliberately not added there, to
    avoid handing every OTHER BulkWriter caller (Run Load, Pipeline
    targets) a destructive capability they never asked for.

    DROP TABLE IF EXISTS is valid, portable SQL across Postgres/MySQL/SQL
    Server/Snowflake; Oracle lacks the IF EXISTS clause, so a missing
    table there raises ORA-00942, caught and treated as "nothing to drop"
    rather than a real failure — the table not existing yet is exactly the
    state DROP_AND_RECREATE wants to reach anyway."""
    from sqlalchemy import text as sa_text

    # Must match the SAME lowercase-normalized name ensure_table()/
    # write_batch_with_fallback() actually created/write to (see
    # normalize_identifier()'s own docstring in loading/writers/base.py) —
    # otherwise DROP/TRUNCATE would target a same-looking but differently-
    # cased identifier the DB treats as a distinct table.
    norm_schema = normalize_identifier(config.target_schema) if config.target_schema else None
    norm_table = normalize_identifier(config.target_table)
    table_ident = connector.quote_object(norm_table, norm_schema)
    engine = connector.engine(pool_size=1)
    with engine.begin() as cxn:
        if config.load_mode == ProcessedDataStorageLoadMode.DROP_AND_RECREATE:
            try:
                cxn.execute(sa_text(f"DROP TABLE IF EXISTS {table_ident}"))
            except Exception:
                try:
                    cxn.execute(sa_text(f"DROP TABLE {table_ident}"))
                except Exception:
                    pass  # table genuinely didn't exist (e.g. Oracle's ORA-00942) — already the desired end state
        elif config.load_mode == ProcessedDataStorageLoadMode.TRUNCATE_AND_LOAD:
            try:
                cxn.execute(sa_text(f"TRUNCATE TABLE {table_ident}"))
            except Exception:
                pass  # table doesn't exist yet on a first-ever push — ensure_table() below creates it fresh, nothing to truncate


def _push_to_database(connector, connection_type: str, config: ProcessedDataStorageConfig, record: dict) -> None:
    if config.pending_reset and config.load_mode in (
        ProcessedDataStorageLoadMode.TRUNCATE_AND_LOAD, ProcessedDataStorageLoadMode.DROP_AND_RECREATE,
    ):
        _apply_load_mode_reset(connector, connection_type, config)
        config.pending_reset = False

    writer = get_bulk_writer(connection_type, connector)
    columns = list(record.keys())
    df = pd.DataFrame([record], columns=columns)
    writer.ensure_table(config.target_schema, config.target_table, df, key_columns=["document_id"])
    if config.load_mode == ProcessedDataStorageLoadMode.APPEND:
        # Real, plain INSERT — no upsert-by-key at all, so a document
        # pushed twice (e.g. reprocessed) lands as two distinct rows. No
        # ensure_unique_key() call either: a unique index on document_id
        # would make a second insert for the same document fail outright,
        # defeating the whole point of an append-only history mode.
        write_batch_with_fallback(writer, config.target_schema, config.target_table, columns, [record], key_columns=None)
    else:
        # CREATE_IF_NOT_EXISTS (the original, still-default behavior) and
        # the settle-state after a one-time TRUNCATE_AND_LOAD/
        # DROP_AND_RECREATE reset both upsert by document_id — the
        # existing real, tested path.
        writer.ensure_unique_key(config.target_schema, config.target_table, ["document_id"])
        write_batch_with_fallback(writer, config.target_schema, config.target_table, columns, [record], key_columns=["document_id"])


def _push_to_file(connector, connection_type: str, config: ProcessedDataStorageConfig, record: dict) -> None:
    if config.file_format == ProcessedDataStorageFileFormat.CSV:
        if not hasattr(connector, "append_csv_row"):
            raise ConnectorUnsupportedError(f"{connection_type} isn't a supported Processed Data Storage CSV destination.")
        connector.append_csv_row(config.target_table, record)
    elif config.file_format == ProcessedDataStorageFileFormat.PARQUET:
        if not hasattr(connector, "write_parquet_record"):
            raise ConnectorUnsupportedError(f"{connection_type} isn't a supported Processed Data Storage Parquet destination.")
        connector.write_parquet_record(config.target_table, record)
    elif config.file_format == ProcessedDataStorageFileFormat.XML:
        if not hasattr(connector, "append_xml_record"):
            raise ConnectorUnsupportedError(f"{connection_type} isn't a supported Processed Data Storage XML destination.")
        connector.append_xml_record(config.target_table, record)
    else:
        if not hasattr(connector, "append_jsonl"):
            raise ConnectorUnsupportedError(f"{connection_type} isn't a supported Processed Data Storage file destination.")
        connector.append_jsonl(config.target_table, record)


# Connection categories this feature can write to — every DATABASE type
# with a real registered BulkWriter (loading/writers/registry.py), plus
# LOCAL_FILE for the "files" destination. Anything else (an API/CLOUD/
# APPLICATION connection) is rejected up front with a clear reason rather
# than failing deep inside build_connector()/get_bulk_writer().
_DATABASE_TYPES = {"POSTGRESQL", "MYSQL", "SQL_SERVER", "ORACLE", "SNOWFLAKE"}
_FILE_TYPES = {"LOCAL_FILE"}

# _flatten_document()'s record keys, mapped onto the six logical datasets
# spec item #5 wants stored separately — used by store_local_datasets()
# below. Every key not listed here (document_id/organization_id/filename/
# doc_type/status/confidence/timestamps) is genuinely cross-cutting
# METADATA, not a raw/transformed payload of its own.
_DATASET_FIELD_MAP = {
    DocumentDatasetType.OCR: ("ocr_text", "ocr_confidence"),
    DocumentDatasetType.AI_EXTRACTION: ("extraction_fields",),
    DocumentDatasetType.STRUCTURED_DATA: ("dataset",),
}


async def store_local_datasets(db: AsyncSession, doc: Document, *, organization_id: int | None) -> None:
    """The always-on "storage_type=SQLITE" path (spec item #5's default
    requirement — "the system must work out-of-the-box using SQLite") —
    called unconditionally from push_document() regardless of `mode`, with
    NO Connection/EXTERNAL configuration required. Normalizes this
    document's processed output into DocumentProcessedDataset rows, one
    per logical dataset type, each written ONLY by upserting that type's
    OWN row (matched on UNIQUE(document_id, dataset_type)) — so storing a
    fresh STRUCTURED_DATA row never reads or touches the OCR row, which is
    what genuinely satisfies "do NOT overwrite raw OCR with transformed
    data": they are different rows, never merged.

    Never raises past itself, same contract as the EXTERNAL push below —
    this local-storage step must not retroactively break a document's
    already-succeeded processing result."""
    try:
        metadata_payload = {
            "filename": doc.filename, "doc_type": doc.doc_type.value if doc.doc_type else None,
            "status": doc.status.value if doc.status else None, "confidence": doc.confidence,
            "pipeline": doc.pipeline, "uploaded_by_id": doc.uploaded_by_id, "folder": doc.folder,
            "mime_type": doc.mime_type, "size_bytes": doc.size_bytes, "page_count": doc.page_count,
        }
        ocr_payload = dict(doc.ocr_result) if doc.ocr_result else None
        extraction_payload = (doc.extraction_result or {}).get("fields") if doc.extraction_result else None
        structured_payload = doc.dataset if doc.dataset is not None else None
        validation_payload = doc.timeline if isinstance(doc.timeline, (dict, list)) else None
        run_payload = {
            "ocr_percent": doc.ocr_percent, "extraction_percent": doc.extraction_percent,
            "processing_started_at": doc.processing_started_at.isoformat() if doc.processing_started_at else None,
            "processing_completed_at": doc.processing_completed_at.isoformat() if doc.processing_completed_at else None,
            "processing_time_seconds": doc.processing_time_seconds, "error_message": doc.error_message,
            "extraction_model": doc.extraction_model,
        }
        datasets: list[tuple[DocumentDatasetType, dict | list | None]] = [
            (DocumentDatasetType.METADATA, metadata_payload),
            (DocumentDatasetType.OCR, ocr_payload),
            (DocumentDatasetType.AI_EXTRACTION, extraction_payload),
            (DocumentDatasetType.STRUCTURED_DATA, structured_payload),
            (DocumentDatasetType.VALIDATION, validation_payload),
            (DocumentDatasetType.PROCESSING_RUN, run_payload),
        ]
        for dataset_type, payload in datasets:
            if payload is None:
                continue  # nothing produced yet for this type (e.g. OCR hasn't run) — leave no row rather than a fake empty one
            stmt = sqlite_insert(DocumentProcessedDataset).values(
                organization_id=organization_id, document_id=doc.id, dataset_type=dataset_type.value, payload=payload,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["document_id", "dataset_type"],
                set_={"payload": stmt.excluded.payload, "organization_id": stmt.excluded.organization_id},
            )
            await db.execute(stmt)
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - local-storage failure must never break the document's own already-succeeded result
        await db.rollback()
        logger.warning("store_local_datasets failed for document %s (org %s): %s", doc.id, organization_id, exc)


def _effective_config(config: ProcessedDataStorageConfig, override: dict | None) -> ProcessedDataStorageConfig:
    """Builds a TRANSIENT (never db.add()-ed) ProcessedDataStorageConfig-
    shaped object for a per-batch storage override (Part 21 — ZIP Batch
    Upload's own Storage config), so push_document() can reuse
    _push_to_database()/_push_to_file() unchanged instead of duplicating
    their logic. Never attached to the session, so setting attributes on
    it and later calling db.commit() cannot accidentally persist a batch-
    derived config as a new row, or leak into the real org-wide row.

    `override` carries only the fields DocumentBatch actually lets a
    batch override (see models/document_batch.py) — storage_type is the
    on/off switch: override=None or override["storage_type"] is None means
    "no override", returning `config` (the real org row) unchanged so the
    ordinary org-wide behavior applies exactly as before this feature
    existed. A non-None storage_type means the batch's own upload dialog
    picked an explicit destination for its documents: mode is derived as
    EXTERNAL when storage_type != SQLITE (a target_connection_id is then
    required, same as the org-wide config's own update_config() contract),
    else DEFAULT (an explicit per-batch opt-OUT of external push,
    regardless of what the org's own default is). include_* flags aren't
    overridable per batch — always inherited from the org config, same as
    every other cross-cutting "what to include" preference."""
    if not override or not override.get("storage_type"):
        return config
    storage_type = ProcessedDataStorageType(override["storage_type"])
    mode = ProcessedDataStorageMode.EXTERNAL if storage_type != ProcessedDataStorageType.SQLITE else ProcessedDataStorageMode.DEFAULT
    return ProcessedDataStorageConfig(
        organization_id=config.organization_id,
        mode=mode,
        storage_type=storage_type,
        load_mode=ProcessedDataStorageLoadMode(override["load_mode"]) if override.get("load_mode") else config.load_mode,
        file_format=ProcessedDataStorageFileFormat(override["file_format"]) if override.get("file_format") else config.file_format,
        target_connection_id=override.get("target_connection_id"),
        target_schema=override.get("target_schema"),
        target_table=override.get("target_table") or config.target_table,
        include_ocr_text=config.include_ocr_text,
        include_extraction_fields=config.include_extraction_fields,
        include_dataset=config.include_dataset,
        include_ai_raw_response=config.include_ai_raw_response,
        pending_reset=False,
    )


async def push_document(db: AsyncSession, doc: Document, *, organization_id: int | None, override: dict | None = None) -> None:
    """Called once, right after a document finishes Document Intelligence
    processing (COMPLETED or NEEDS_REVIEW — see document_service.py).
    ALWAYS normalizes this document's output into local
    DocumentProcessedDataset rows first (store_local_datasets() — the
    zero-config storage_type=SQLITE path, see that function's own
    docstring), then additionally pushes a flattened copy to an EXTERNAL
    Connection if mode=EXTERNAL and a target_connection_id is configured
    — the original, opt-in behavior, extended with storage_type/load_mode/
    file_format. ANY EXTERNAL-push failure (bad connection, unreachable
    target, unsupported type) is caught, scrubbed of secrets, and recorded
    on the config row (last_status=FAILED, last_error) — visible to an
    admin via GET /document-storage/config — but never re-raised, so
    document processing's own already-succeeded result is never
    retroactively broken by an export step failing.

    override (Part 21 — ZIP Batch Upload's per-batch Storage config, see
    document_batch_service.py) is a dict built from a DocumentBatch row's
    own storage_* columns, or None for every other caller (manual upload,
    Document Schedule, Pipeline Builder). When set, _effective_config()
    builds a transient, batch-scoped config and this push writes against
    THAT destination instead of the org-wide one — and, since the
    transient object is never the real `config` row, the audit columns
    (last_status/last_error/last_pushed_*) intentionally stay on the org
    row untouched for an overridden push. Same never-raises contract
    either way (a batch's own push failure is only logged here, exactly
    like the org-wide case) — this function has no way to know whether its
    caller is prepared to catch an exception, and document_service.py's
    process_document() calls it unconditionally, uncaught."""
    config = await get_or_create_config(db, organization_id)
    await store_local_datasets(db, doc, organization_id=organization_id)
    effective = _effective_config(config, override)
    is_override = effective is not config
    if effective.mode != ProcessedDataStorageMode.EXTERNAL or not effective.target_connection_id:
        return

    connector_secrets: dict = {}
    try:
        conn = await connection_service.get_connection(db, effective.target_connection_id, organization_id)
        if conn is None:
            raise DocumentStorageServiceError(f"Target connection {effective.target_connection_id} no longer exists or no longer belongs to this organization.")
        connection_type = conn.connection_type.value
        record = _flatten_document(doc, effective)
        if connection_type in _DATABASE_TYPES:
            connector = connection_service.build_connector(conn)
            connector_secrets = connector.secrets
            _push_to_database(connector, connection_type, effective, record)
        elif connection_type in _FILE_TYPES:
            connector = connection_service.build_connector(conn)
            connector_secrets = connector.secrets
            _push_to_file(connector, connection_type, effective, record)
        else:
            raise ConnectorUnsupportedError(
                f"{connection_type.replace('_', ' ').title()} isn't a supported Processed Data Storage destination "
                f"— use a database connection (Postgres, MySQL, SQL Server, Oracle, Snowflake) or a Local File connection."
            )
        if is_override:
            return
        config.last_status = ProcessedDataStoragePushStatus.SUCCESS
        config.last_error = None
    except Exception as exc:
        message = scrub_secrets(str(exc), connector_secrets)
        logger.warning("Processed Data Storage push failed for document %s (org %s)%s: %s", doc.id, organization_id, " [batch override]" if is_override else "", message)
        if is_override:
            return  # never raises past itself — see docstring; the org's own config row is left untouched for an overridden push
        config.last_status = ProcessedDataStoragePushStatus.FAILED
        config.last_error = message
    config.last_pushed_document_id = doc.id
    config.last_pushed_at = datetime.now(timezone.utc)
    await db.commit()
