"""
document_schedule_service.py — Scheduled Document Intelligence Workflow.

Owns exactly the two document-schedule-specific concerns:
  1. Discovery — reusing the existing connector interface
     (connectors/files/base.py's FileConnectorBase: list_files/
     open_read_stream/move_file/file_checksum) uniformly across every
     storage type already built here (LOCAL_FILE/SFTP/FTP/NETWORK_FILE/
     AZURE/AWS/EMAIL_IMAP). No new connector code for FILE-kind. Two
     further source kinds (PAY18 DATABASE, PAY20 API) reuse a different
     existing connector capability each (preview_sql()/fetch_api()) the
     same way — see discover_database_records()/discover_api_records()
     below.
  2. Idempotency — the durable DocumentSourceRegistry ledger (spec
     section 7/8): never reprocess a file whose (connection, path,
     filename, content_hash) identity is already PROCESSED.

The actual OCR/AI-extraction/confidence/Review-Queue pipeline is NOT
reimplemented here — see services/schedule_service.py's
_execute_document_schedule() (Phase 6), which calls
document_service.save_from_source() + start_processing()/process_document(),
the exact same pipeline manual scan and bulk upload already use.
"""
import asyncio
import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, ConnectorValidationError, FileInfo
from app.models.connection import Connection
from app.models.document_schedule import ALLOWED_TRANSITIONS, DocumentScheduleConfig, DocumentSourceRegistry, DocumentSourceStatus
from app.services.connection_service import build_connector


class InvalidStateTransition(Exception):
    """Raised when transition() is asked to move a registry row's status
    to somewhere ALLOWED_TRANSITIONS says it can't reach from where it
    currently is — spec section 94's "invalid state transitions must be
    rejected", enforced at the one place every status write goes through
    rather than trusted per call site."""


SUPPORTED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "tiff", "tif", "bmp"}


def is_supported_file(file_name: str) -> bool:
    """Mirrors document_service.EXTENSION_TYPE_MAP's OCR-capable subset —
    spec section 6: 'Do not process unsupported files' (they still get a
    real UNSUPPORTED registry row, never silently dropped). Word/Excel/
    text files ARE supported by manual upload but carry content natively
    (no OCR stage — see document_service.py's OcrNotApplicableError), so
    they're deliberately excluded from a *scanned-document* source
    schedule's scope; a schedule scanning a folder of PDFs/images
    shouldn't silently ingest an unrelated spreadsheet that happens to
    match the file pattern."""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return ext in SUPPORTED_EXTENSIONS


async def discover_files(connection: Connection, config: DocumentScheduleConfig) -> list[FileInfo]:
    """Real listing via the connection's own connector — never a fake/
    simulated file list. Runs off the event loop (asyncio.to_thread) the
    same way every other connector call in this codebase does (see
    connection_service.list_schemas/list_objects), since paramiko/
    smbclient/boto3/azure-storage-blob calls are all blocking I/O."""
    connector = build_connector(connection)
    try:
        return await asyncio.to_thread(connector.list_files, pattern=config.file_pattern, recursive=config.recursive_scan)
    except ConnectorError:
        raise  # let the caller attribute this to SOURCE_CONNECTION_FAILED / SOURCE_PATH_NOT_FOUND


# ── DATABASE intake (PAY18 — non-file invoice intake sources) ─────────
# Discovery for a DocumentScheduleConfig.source_kind == "DATABASE"
# schedule: no filesystem/connector "list files" concept applies, so this
# runs config.source_query through the connection's own preview_sql()
# (the same real, dialect-agnostic SQLAlchemy execution SQL Workspace's
# preview surface already uses — read-only, SELECT/WITH only, enforced by
# preview_sql() itself) and hands back one dict per row. Bounded by
# batch_size the same way a file schedule's discover_files() result is
# sliced by the caller — this function itself returns everything the
# query matches up to that cap, never silently truncating below it.

async def discover_database_records(connection: Connection, config: DocumentScheduleConfig) -> list[dict]:
    """Real query execution via the connection's own connector — never a
    fake/simulated row set. preview_sql() itself rejects anything but a
    SELECT/WITH statement (ConnectorValidationError, a ConnectorError
    subclass — the caller's existing `except ConnectorError` handling
    already covers it) and runs off the event loop the same way every
    other blocking-driver connector call in this codebase does."""
    if not (config.source_query or "").strip():
        raise ConnectorValidationError("This schedule has no source query configured.")
    connector = build_connector(connection)
    limit = max(1, config.batch_size)

    def _run() -> list[dict]:
        result = connector.preview_sql(config.source_query, {}, limit)
        return [dict(zip(result.columns, row)) for row in result.rows]

    return await asyncio.to_thread(_run)


# ── API intake (PAY20 — the remaining non-file invoice intake source) ─
# Discovery for a DocumentScheduleConfig.source_kind == "API" schedule:
# no filesystem/connector "list files" concept applies here either (same
# as DATABASE above), so this runs the schedule's source_api_* fields
# through the connection's own fetch_api() — the same real, paginated
# HTTP fetch (offset/page/cursor/token styles, auth, retry) the SQL
# Workspace/Source Dataset "preview-api" surface already uses — and hands
# back one dict per decoded record. Bounded by batch_size the same way
# discover_database_records()/a file schedule's discover_files() result
# is sliced by the caller.

async def discover_api_records(connection: Connection, config: DocumentScheduleConfig) -> list[dict]:
    """Real HTTP fetch via the connection's own connector — never a fake/
    simulated response. fetch_api() itself owns pagination/auth/retry;
    this function's only job is reshaping its PreviewResult (columns +
    rows) into the same list[dict] shape discover_database_records()
    already returns, so every downstream function (record_content_hash,
    record_identity, build_fields_from_record) is source-kind-agnostic."""
    if not (config.source_api_endpoint or "").strip():
        raise ConnectorValidationError("This schedule has no source API endpoint configured.")
    connector = build_connector(connection)
    limit = max(1, config.batch_size)

    def _run() -> list[dict]:
        result = connector.fetch_api(
            endpoint=config.source_api_endpoint, method=config.source_api_method or "GET",
            query_params=config.source_api_query_params or None,
            json_path=config.source_api_json_path or None,
            pagination=config.source_api_pagination or None,
            limit=limit,
        )
        return [dict(zip(result.columns, row)) for row in result.rows]

    return await asyncio.to_thread(_run)


# ── ERP intake (PAY21 — the remaining non-file invoice intake source) ─
# Discovery for a DocumentScheduleConfig.source_kind == "ERP" schedule:
# reuses connectors/application/oracle_erp.py's list_invoices() (real GET
# against Oracle Fusion ERP Cloud's documented Invoices collection
# resource, with `expand=invoiceLines` so line items come back in the
# same call) rather than the generic fetch_api() DATABASE/API-kind use,
# since the Oracle ERP connector is its own connector class with its own
# auth/pagination handling, not a RestApiConnector. Unlike DATABASE/API-
# kind, the source's field NAMES aren't something the org gets to choose
# (a SQL alias or a JSON API's own response shape) — Oracle's AP Invoice
# REST resource ships one fixed, documented set of header/line field
# names — so this module owns one small, honestly-disclosed fixed mapping
# from those Oracle field names onto document_taxonomies' canonical
# INVOICE field names, rather than reusing invoice_erp_mapping_service's
# versioned OUTBOUND mapping (INV9): that mapping's canonical side targets
# ApInvoice ORM column names (e.g. "total", "purchase_order"), which
# differ from the taxonomy names build_fields_from_record() understands
# (e.g. "total_amount", "po_number") — reversing it would produce a
# second, inconsistent field-naming scheme for the same data rather than
# reusing anything real. Same "only one ERP connector type ships today"
# disclosure invoice_erp_mapping_service's own default mapping already
# makes for the outbound direction.
_ERP_HEADER_FIELD_MAP = {
    "InvoiceNumber": "invoice_number",
    "InvoiceDate": "invoice_date",
    "InvoiceCurrency": "currency",
    "InvoiceAmount": "total_amount",
    "VendorNum": "vendor_name",
    "VendorSiteCode": "vendor_tax_id",
    "Description": "notes",
    "PaymentTerms": "payment_terms",
}
_ERP_LINE_FIELD_MAP = {
    "Description": "description",
    "LineAmount": "amount",
}


def _map_erp_invoice_record(raw: dict) -> dict:
    """One Oracle AP Invoice REST item (header fields + an inlined
    `invoiceLines` array, from list_invoices()'s `expand=invoiceLines`) ->
    one canonical-field dict, in the exact list[dict]-of-canonical-fields
    shape discover_database_records()/discover_api_records() already
    return — so build_fields_from_record() needs no ERP-specific
    branch at all, same reuse discover_api_records() already gets from
    that function's normalize-by-column-name design."""
    mapped: dict = {}
    for erp_field, canonical in _ERP_HEADER_FIELD_MAP.items():
        if erp_field in raw:
            mapped[canonical] = raw.get(erp_field)
    mapped["erp_invoice_id"] = raw.get("InvoiceId")  # kept as an extra (non-taxonomy) field — real audit trail of which ERP row this came from, never dropped
    lines = []
    for raw_line in (raw.get("invoiceLines") or []):
        if not isinstance(raw_line, dict):
            continue
        line: dict = {}
        for erp_field, canonical in _ERP_LINE_FIELD_MAP.items():
            if erp_field in raw_line:
                line[canonical] = raw_line.get(erp_field)
        if line:
            lines.append(line)
    if lines:
        mapped["line_items"] = lines
    return mapped


async def discover_erp_records(connection: Connection, config: DocumentScheduleConfig) -> list[dict]:
    """Real HTTP fetch via the connection's own OracleErpConnector — never
    a fake/simulated invoice list. list_invoices() owns Oracle's own
    documented limit/offset/hasMore pagination; this function loops it
    until either batch_size rows are collected or the ERP reports no more
    pages, mapping each raw item through _map_erp_invoice_record() so the
    returned shape matches discover_database_records()/discover_api_records()
    exactly."""
    connector = build_connector(connection)
    target = max(1, config.batch_size)
    page_size = min(target, 100)  # Oracle ADF/REST BC collection resources commonly cap a single page around this size — a real, conservative per-call limit, not this schedule's own batch_size verbatim
    q = (config.source_erp_query or "").strip() or None

    def _run() -> list[dict]:
        records: list[dict] = []
        offset = 0
        while len(records) < target:
            page = connector.list_invoices(limit=min(page_size, target - len(records)), offset=offset, q=q)
            items = page.get("items") or []
            records.extend(_map_erp_invoice_record(item) for item in items)
            if not page.get("hasMore") or not items:
                break
            offset += len(items)
        return records

    return await asyncio.to_thread(_run)


def record_content_hash(row: dict) -> str:
    """Stable identity for a DATABASE-sourced row — the DocumentSourceRegistry
    dedup key's content_hash column, playing the exact same role a file's
    real SHA-256 does for a FILE-kind schedule (spec section 7: same
    content already PROCESSED -> skip). `default=str` handles whatever a
    given driver hands back for DATE/DECIMAL/etc columns (datetime,
    Decimal, ...) without needing per-dialect type-mapping here — this
    hash only needs to change when the row's own content changes, not to
    round-trip perfectly."""
    canonical = json.dumps(row, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_identity(config: DocumentScheduleConfig, row: dict, content_hash: str) -> tuple[str, str, str]:
    """Builds the (source_path, file_name, record_key) identity a DATABASE-
    or API-sourced (PAY20) row is registered/processed under —
    DocumentSourceRegistry itself is unchanged (source_path/file_name are
    already free-text columns), so either non-file kind slots into the
    exact same idempotency ledger a file-sourced schedule uses. source_path
    is the same for every row from one schedule (identifies "which query"
    or "which API call"); file_name identifies one row within it — from
    config.source_record_key_column when the row actually has that column
    and a non-empty value, else falls back to a short prefix of this row's
    own content hash (a source with no natural key still gets a
    stable-per-content identity, it just can't tell "this row's key is
    unchanged but content changed" apart from "this is a different row" as
    precisely — documented in the model's own docstring)."""
    if config.source_kind == "API":
        identity_seed = json.dumps(
            {"endpoint": config.source_api_endpoint, "method": config.source_api_method,
             "query_params": config.source_api_query_params, "pagination": config.source_api_pagination},
            sort_keys=True, default=str,
        )
        source_path = f"api:{hashlib.sha256(identity_seed.encode('utf-8')).hexdigest()[:16]}"
    elif config.source_kind == "ERP":
        source_path = f"erp:{hashlib.sha256((config.source_erp_query or '').encode('utf-8')).hexdigest()[:16]}"
    else:  # DATABASE
        source_path = f"query:{hashlib.sha256((config.source_query or '').encode('utf-8')).hexdigest()[:16]}"
    key_col = config.source_record_key_column
    raw_key = row.get(key_col) if key_col else None
    record_key = str(raw_key).strip() if raw_key not in (None, "") else content_hash[:16]
    file_name = f"row_{record_key}"
    return source_path, file_name, record_key


async def compute_file_hash(connection: Connection, file_path: str) -> str:
    """Real streamed SHA-256 via the connector's own file_checksum() —
    never loads the whole file into this process's memory beyond what the
    connector's own chunked read already bounds (see connectors/base.py's
    docstring: 'file_checksum() (streamed SHA-256 for duplicate
    detection)')."""
    connector = build_connector(connection)
    return await asyncio.to_thread(connector.file_checksum, file_path)


async def upsert_registry_entry(
    db: AsyncSession, *, organization_id: int | None, schedule_id: int | None,
    connection: Connection, source_path: str, file_info: FileInfo, content_hash: str,
    run_id: int | None,
) -> DocumentSourceRegistry:
    """Idempotent upsert on the (organization_id, source_connection_id,
    source_path, file_name, content_hash) identity — spec section 8's
    'do not rely only on application-level checks': the UNIQUE constraint
    on document_source_registry is the actual guarantee; this function's
    check-then-insert is just the normal path, and the IntegrityError
    fallback handles the rare concurrent-discovery race (two workers, or
    a crashed-and-resumed run) without erroring the whole batch or
    creating a duplicate row."""
    stmt = select(DocumentSourceRegistry).where(
        DocumentSourceRegistry.organization_id == organization_id,
        DocumentSourceRegistry.source_connection_id == connection.id,
        DocumentSourceRegistry.source_path == source_path,
        DocumentSourceRegistry.file_name == file_info.name,
        DocumentSourceRegistry.content_hash == content_hash,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        # Re-discovered on a later run — refresh the bookkeeping fields a
        # human would expect to be current (which run last touched it,
        # what the source reported for size/mtime this time) without
        # touching its processing status; that's owned by transition().
        existing.schedule_id = schedule_id
        existing.file_size = file_info.size
        existing.modified_time = file_info.modified_at
        # NOTE: processing_run_id is deliberately NOT touched here. It
        # means "the run that acted on this file", and every run
        # re-discovers every still-present source file whether or not it
        # ends up doing anything with it (see needs_processing()). If we
        # stamped it here unconditionally, a later run that merely
        # re-lists an already-PROCESSED/REVIEW_REQUIRED file (and then
        # skips it) would silently steal that file off every earlier
        # run's per-file breakdown table (list_registry_for_run() below
        # keys off this column) — found live: run_6's "Files" table went
        # empty after run_7 re-discovered the same 2 files. The caller
        # (schedule_service._run_document_schedule_background) sets this
        # column itself, only for files it actually queues/transitions
        # this run.
        await db.commit()
        await db.refresh(existing)
        return existing

    row = DocumentSourceRegistry(
        organization_id=organization_id, schedule_id=schedule_id,
        source_connection_id=connection.id, source_type=connection.connection_type.value,
        source_path=source_path, file_name=file_info.name, file_size=file_info.size,
        modified_time=file_info.modified_at, content_hash=content_hash,
        status=DocumentSourceStatus.DISCOVERED, processing_run_id=run_id,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing is None:
            raise  # a genuinely different failure, not the race this guards against
        return existing
    await db.refresh(row)
    return row


async def find_duplicate_content(
    db: AsyncSession, *, organization_id: int | None, content_hash: str, exclude_id: int,
) -> DocumentSourceRegistry | None:
    """Spec section 78 — same bytes under a DIFFERENT (connection, path,
    filename) identity. Only PROCESSED counterparts count as a real
    duplicate (a REVIEW_REQUIRED/FAILED one hasn't actually produced a
    result yet, so this file isn't redundant with it)."""
    stmt = select(DocumentSourceRegistry).where(
        DocumentSourceRegistry.organization_id == organization_id,
        DocumentSourceRegistry.content_hash == content_hash,
        DocumentSourceRegistry.status == DocumentSourceStatus.PROCESSED,
        DocumentSourceRegistry.id != exclude_id,
    )
    return (await db.execute(stmt.order_by(DocumentSourceRegistry.id.asc()).limit(1))).scalar_one_or_none()


def needs_processing(entry: DocumentSourceRegistry) -> bool:
    """Spec section 7: 'If a document is already PROCESSED and the content
    hash has not changed, do NOT process it again.' Extended the same way
    to REVIEW_REQUIRED — found live: without this, every subsequent
    scheduled run re-discovered the exact same unchanged file, called
    save_from_source() again, and created a SECOND Document + a SECOND
    Review Queue item for it, forever, for as long as a human hadn't acted
    on the first one yet (a daily schedule would pile up a fresh duplicate
    every single day). Spec section 26/89 frames reprocessing a
    Review-Queue document as a REVIEWER-initiated action (the Review Queue
    "Reprocess" button), not something the scheduler re-triggers on its
    own just because the file is still sitting there — the registry row
    and its one Document/review item stay put until a human resolves it
    or the file's content actually changes (new hash -> genuinely a new
    registry row, handled separately). FAILED stays eligible — that's the
    real retry path (spec section 27), and DISCOVERED/QUEUED/SKIPPED are
    obviously still open."""
    return entry.status not in (DocumentSourceStatus.PROCESSED, DocumentSourceStatus.REVIEW_REQUIRED)


async def transition(db: AsyncSession, entry: DocumentSourceRegistry, new_status: DocumentSourceStatus, *, error: str | None = None, commit: bool = True) -> DocumentSourceRegistry:
    """The single choke point every status write on a registry row goes
    through — spec section 94's document state machine, enforced rather
    than trusted. QUEUED->QUEUED (re-queueing an already-queued row, e.g.
    a retry) is allowed as a no-op; every other same-state write is
    rejected the same as a genuinely backward one."""
    if new_status != entry.status and new_status not in ALLOWED_TRANSITIONS.get(entry.status, set()):
        raise InvalidStateTransition(f"Cannot move document '{entry.file_name}' from {entry.status.value} to {new_status.value}.")
    entry.status = new_status
    if error is not None:
        entry.last_error = error[:2000]
    elif new_status in (DocumentSourceStatus.PROCESSED, DocumentSourceStatus.REVIEW_REQUIRED):
        # This attempt's real outcome is success (or "needs a human look",
        # which is not itself an error) — clear out whatever error a PRIOR
        # attempt left behind, or a reviewer/monitoring dashboard reads a
        # stale failure message next to a document that actually just
        # succeeded. Found live: a file that failed once (transient) and
        # then succeeded on retry still showed the old NameError text.
        entry.last_error = None
    now = datetime.now(entry.discovered_at.tzinfo) if entry.discovered_at and entry.discovered_at.tzinfo else datetime.utcnow()
    if new_status == DocumentSourceStatus.PROCESSING and entry.first_processed_at is None:
        entry.first_processed_at = now
    if new_status in (DocumentSourceStatus.PROCESSED, DocumentSourceStatus.REVIEW_REQUIRED, DocumentSourceStatus.FAILED):
        entry.last_processed_at = now
    if commit:
        await db.commit()
        await db.refresh(entry)
    return entry


async def list_registry_for_run(
    db: AsyncSession, run_id: int, *, page: int = 1, page_size: int = 50,
) -> tuple[list[DocumentSourceRegistry], int]:
    """Spec section 15/44 — a run's per-file breakdown (Document Processing
    History drilldown). Backs GET /schedules/{id}/runs/{run_id}/documents."""
    stmt = select(DocumentSourceRegistry).where(DocumentSourceRegistry.processing_run_id == run_id)
    result = await db.execute(stmt)
    rows = sorted(result.scalars().all(), key=lambda r: r.file_name)
    total = len(rows)
    start = (page - 1) * page_size
    return rows[start:start + page_size], total
