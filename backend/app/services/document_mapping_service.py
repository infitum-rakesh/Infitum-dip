"""
document_mapping_service — applies a PUBLISHED, document-sourced
ColumnMappingVersion (Document Mapping Profiles — see
models/column_mapping.py's module docstring) to one real Document's
extraction result.

Reuses execution/mapping_execution.py::apply_mapping_to_dataframe() —
the exact SAME engine a production pipeline's column_mapping_step node
already runs — rather than building a second execution path. The only
document-specific work here is turning a Document's extracted fields
into the pandas DataFrame that function already expects (via
document_service.build_dataset(), the one place dataset shape is
defined) and persisting the result.

Records an append-only DocumentMappedOutput row per run — "preserve
historical outputs" taken literally, see that model's own docstring.
Never raises past the caller for a real mapping/data problem (a
MappingExecutionError from the engine itself) — persists a FAILED row
instead, so a bad run is visible history, not a 500.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution import mapping_execution
from app.models.document import Document, DocumentStatus
from app.models.document_mapped_output import DocumentMappedOutput
from app.models.document_schedule import DocumentScheduleConfig, DocumentSourceRegistry, DocumentSourceStatus
from app.services import document_mapping_resolution_service, document_service, mapping_service
from app.services.document_mapping_resolution_service import ScheduledMappingConfigError


class DocumentMappingError(Exception):
    """A mapping profile cannot be applied to this document as configured
    — a bad request, not a runtime execution failure (see
    mapping_execution.MappingExecutionError for that, which this module
    catches and turns into a persisted FAILED row instead)."""


async def apply_mapping_profile_to_document(
    db: AsyncSession, *, document: Document, mapping_id: int, organization_id: int | None, created_by: int | None = None,
    schedule_id: int | None = None, schedule_run_id: int | None = None,
) -> DocumentMappedOutput:
    """schedule_id/schedule_run_id: set ONLY by the scheduler
    (services/schedule_service.py, via apply_scheduled_mapping_if_configured()
    below) — a manual "Apply Mapping Profile" click from Document Details
    leaves both None. Together they're what section 7.4 calls "every
    scheduled execution must record... Schedule ID, Run ID", snapshotted
    straight onto the resulting DocumentMappedOutput row's trigger/
    schedule_id/schedule_run_id columns — see _record_output() below."""
    mapping = await mapping_service.get_mapping(db, mapping_id, organization_id)
    if mapping is None:
        raise DocumentMappingError(f"Mapping profile {mapping_id} was not found.")
    if mapping.source_document_type is None:
        raise DocumentMappingError(
            f"Mapping profile '{mapping.name}' is not a Document Intelligence mapping profile — its source is a connection, not a document_type."
        )
    if mapping.current_published_version is None:
        raise DocumentMappingError(f"Mapping profile '{mapping.name}' has no PUBLISHED version yet — publish one first.")
    version = await mapping_service.get_version(db, mapping_id, mapping.current_published_version)
    if version is None or version.status != "PUBLISHED":
        raise DocumentMappingError(f"Mapping profile '{mapping.name}' v{mapping.current_published_version} is not PUBLISHED.")

    # Document-type consistency — a GENERIC-built profile is permissive
    # (its field set is designed to apply broadly); anything else must
    # match the document's own extracted document_type exactly. A
    # genuine remaining mismatch (e.g. an INVOICE-built profile against a
    # GENERIC-extracted document missing a referenced column) is still
    # caught by apply_mapping_to_dataframe()'s own live revalidation
    # below and surfaced as a FAILED row, not silently run anyway.
    doc_type = (document.extraction_result or {}).get("document_type")
    if doc_type and version.source_document_type not in ("GENERIC", doc_type):
        raise DocumentMappingError(
            f"This mapping profile is built for document_type '{version.source_document_type}', but this document "
            f"was extracted as '{doc_type}'."
        )

    # Lookup-based rules need a real source Connection to join against
    # (see execution/mapping_execution.py::_apply_lookup()) — genuinely
    # out of scope for a document-sourced profile in this pass. Rejected
    # up front with a clear, specific error rather than letting the
    # engine fail obscurely on a None connection_id.
    lookup_rows = [m for m in (version.mappings or []) if m.get("lookup")]
    if lookup_rows:
        targets = ", ".join(m["target_column"] for m in lookup_rows)
        raise DocumentMappingError(
            f"This mapping profile uses a lookup-based rule for target column(s) {targets} — lookups aren't "
            "supported for Document Intelligence mapping profiles yet."
        )

    fields = (document.extraction_result or {}).get("fields") or []
    if not fields:
        raise DocumentMappingError("This document has no extracted fields to map yet — run extraction first.")
    dataset = document_service.build_dataset(fields)
    df = pd.DataFrame(dataset["table"]["rows"], columns=dataset["table"]["columns"])

    try:
        mapped_df, rejects = await mapping_execution.apply_mapping_to_dataframe(
            db, df, version, organization_id=organization_id, connector_cache={},
        )
    except mapping_execution.MappingExecutionError as exc:
        return await _record_output(
            db, document=document, mapping_id=mapping.id, version=version,
            columns=[], rows=[], status="FAILED", error_message=str(exc), rejects=[], created_by=created_by,
            schedule_id=schedule_id, schedule_run_id=schedule_run_id,
        )

    columns = list(mapped_df.columns)
    rows = mapped_df.to_dict(orient="records")
    # JSON can't serialize pandas' NaN/NaT null markers — normalized to
    # None here, the one honest "genuinely absent" value the rest of
    # this project already uses everywhere else a value can be missing.
    rows = [{k: (None if pd.isna(v) else v) for k, v in row.items()} for row in rows]

    return await _record_output(
        db, document=document, mapping_id=mapping.id, version=version,
        columns=columns, rows=rows, status="SUCCESS", error_message=None, rejects=rejects, created_by=created_by,
        schedule_id=schedule_id, schedule_run_id=schedule_run_id,
    )


async def _record_output(
    db: AsyncSession, *, document: Document, mapping_id: int, version, columns: list, rows: list,
    status: str, error_message: str | None, rejects: list, created_by: int | None,
    schedule_id: int | None = None, schedule_run_id: int | None = None,
) -> DocumentMappedOutput:
    output = DocumentMappedOutput(
        organization_id=document.organization_id, document_id=document.id, mapping_id=mapping_id,
        mapping_version=version.version_number, target_format=version.target_format or "DB_TABLE",
        columns=columns, rows=rows, row_count=len(rows),
        status=status, error_message=error_message, rejected_rows=rejects,
        created_by=created_by, schedule_id=schedule_id, schedule_run_id=schedule_run_id,
        trigger="SCHEDULED" if schedule_id is not None else "MANUAL",
    )
    db.add(output)
    await db.commit()
    await db.refresh(output)
    return output


async def list_mapped_outputs(db: AsyncSession, document_id: int, organization_id: int | None) -> list[DocumentMappedOutput]:
    stmt = select(DocumentMappedOutput).where(DocumentMappedOutput.document_id == document_id)
    if organization_id is not None:
        stmt = stmt.where(DocumentMappedOutput.organization_id == organization_id)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    # (created_at, id) — id as tiebreaker, since two runs issued in quick
    # succession (e.g. in a test, or scripted back-to-back applies) can
    # share the same created_at at this column's time resolution.
    rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return rows


async def get_mapped_output(db: AsyncSession, output_id: int, organization_id: int | None) -> DocumentMappedOutput | None:
    stmt = select(DocumentMappedOutput).where(DocumentMappedOutput.id == output_id)
    if organization_id is not None:
        stmt = stmt.where(DocumentMappedOutput.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# ── Mapping Editor UI support (Phase 4 — "test against a sample
# document") ─────────────────────────────────────────────────────────

_SAMPLE_DOCUMENT_STATUSES = (DocumentStatus.COMPLETED, DocumentStatus.NEEDS_REVIEW)
_SAMPLE_DOCUMENT_SCAN_LIMIT = 200  # bounded Python-side filter — see docstring below
_SAMPLE_DOCUMENT_RESULT_LIMIT = 25


async def list_sample_documents(db: AsyncSession, *, organization_id: int | None, document_type: str) -> list[dict]:
    """Candidates for the mapping editor's "test against a sample
    document" picker — real, already-processed documents whose own
    extracted document_type matches. extraction_result is JSON, and
    this project has no existing precedent for filtering a JSON column
    at the DB layer (confirmed by inspection of document_service.py) —
    cheaper to fetch a bounded recent page and filter in Python than to
    add a new cross-dialect JSON-path query for what is just an editor
    convenience picker, not a production query path."""
    stmt = select(Document).where(Document.status.in_(_SAMPLE_DOCUMENT_STATUSES))
    if organization_id is not None:
        stmt = stmt.where(Document.organization_id == organization_id)
    stmt = stmt.order_by(Document.uploaded_at.desc()).limit(_SAMPLE_DOCUMENT_SCAN_LIMIT)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    matches = []
    for doc in rows:
        fields = (doc.extraction_result or {}).get("fields") or []
        if not fields:
            continue
        doc_type = (doc.extraction_result or {}).get("document_type")
        if doc_type and doc_type not in ("GENERIC", document_type):
            continue
        matches.append({"id": doc.id, "filename": doc.filename, "uploaded_at": doc.uploaded_at})
        if len(matches) >= _SAMPLE_DOCUMENT_RESULT_LIMIT:
            break
    return matches


async def preview_mapping_against_document(
    db: AsyncSession, *, document: Document, source_document_type: str, target_format: str | None,
    mappings: list[dict], organization_id: int | None,
) -> dict:
    """Dry-run of the exact same engine apply_mapping_profile_to_document()
    uses, against an IN-PROGRESS (possibly unsaved) mapping definition —
    the mapping editor's "Test Against Sample Document" action. Never
    persists a DocumentMappedOutput row (this is a test, not a run) and
    never requires the profile to be PUBLISHED or even saved yet, since
    the whole point is letting a user check a draft mapping before
    saving/publishing it. Builds a lightweight duck-typed stand-in for
    the ColumnMappingVersion ORM row apply_mapping_to_dataframe() expects
    (it only ever reads attributes off `version`, never persists through
    it) rather than requiring a real saved version to exist."""
    lookup_rows = [m for m in (mappings or []) if m.get("lookup")]
    if lookup_rows:
        targets = ", ".join(m["target_column"] for m in lookup_rows)
        raise DocumentMappingError(
            f"This mapping uses a lookup-based rule for target column(s) {targets} — lookups aren't "
            "supported for Document Intelligence mapping profiles yet."
        )

    fields = (document.extraction_result or {}).get("fields") or []
    if not fields:
        raise DocumentMappingError("This document has no extracted fields to map yet — run extraction first.")
    dataset = document_service.build_dataset(fields)
    df = pd.DataFrame(dataset["table"]["rows"], columns=dataset["table"]["columns"])

    pseudo_version = SimpleNamespace(
        mappings=mappings, on_conversion_failure="REJECT_RECORD", unmapped_column_policy="IGNORE",
        source_connection_id=None, source_schema=None, source_object=None, source_document_type=source_document_type,
        target_connection_id=None, target_schema=None, target_object=None, target_format=target_format or "DB_TABLE",
    )
    mapped_df, rejects = await mapping_execution.apply_mapping_to_dataframe(
        db, df, pseudo_version, organization_id=organization_id, connector_cache={},
    )
    columns = list(mapped_df.columns)
    rows = mapped_df.to_dict(orient="records")
    rows = [{k: (None if pd.isna(v) else v) for k, v in row.items()} for row in rows]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "rejected_rows": rejects}


# ── Scheduled Mapping (spec section 7) ───────────────────────────────

async def apply_scheduled_mapping_if_configured(
    db: AsyncSession, *, document: Document, schedule_id: int, schedule_run_id: int,
    schedule_mapping_id: int | None, document_type: str | None, organization_id: int | None,
) -> dict:
    """Called by services/schedule_service.py right after a scheduled
    document's extraction succeeds (status PROCESSED or REVIEW_REQUIRED
    — "mapping failure does not destroy successful extraction results"
    and extraction already succeeded in both of those outcomes, so
    mapping is attempted for either; a FAILED extraction never reaches
    this function at all, by construction of its one call site). Never
    raises — every outcome is reported back as a dict so the caller can
    update its own run-level stats and keep processing the rest of the
    batch (spec section 28/7.5: "continue processing other documents").

    Returns {"status": "applied", "mapping_status": "SUCCESS"|"FAILED"}
    when a mapping actually ran (recorded as a real, append-only
    DocumentMappedOutput row — see apply_mapping_profile_to_document());
    {"status": "no_mapping_configured"} when section 7.2's third option
    applies (nothing recorded — the original extracted JSON is the
    answer, unchanged); or {"status": "config_error", "error": str} when
    the EXPLICIT or DEFAULT mapping itself is unusable (section 7.2 —
    "mark the mapping stage as failed... according to policy"). The
    config_error case deliberately does NOT produce a DocumentMappedOutput
    row: that table's whole contract is "the exact profile version that
    was applied" (section 7.4), and there is no real version to snapshot
    when resolution itself never found one — the caller surfaces this
    via ScheduleRun.document_stats instead (see schedule_service.py).
    """
    # Idempotency (section 7.5: "retries are idempotent and do not
    # create duplicate outputs") — a crash-recovery re-entry into this
    # exact (schedule, run, document) triple must never apply twice.
    existing = await db.execute(
        select(DocumentMappedOutput).where(
            DocumentMappedOutput.schedule_id == schedule_id,
            DocumentMappedOutput.schedule_run_id == schedule_run_id,
            DocumentMappedOutput.document_id == document.id,
            DocumentMappedOutput.status == "SUCCESS",
        )
    )
    if existing.scalar_one_or_none() is not None:
        return {"status": "already_applied"}

    effective_document_type = document_type or (document.extraction_result or {}).get("document_type")
    try:
        resolved = await document_mapping_resolution_service.resolve_scheduled_mapping(
            db, organization_id=organization_id, schedule_mapping_id=schedule_mapping_id, document_type=effective_document_type,
        )
    except ScheduledMappingConfigError as exc:
        return {"status": "config_error", "error": str(exc)}

    if resolved is None:
        return {"status": "no_mapping_configured"}

    try:
        output = await apply_mapping_profile_to_document(
            db, document=document, mapping_id=resolved.mapping_id, organization_id=organization_id,
            schedule_id=schedule_id, schedule_run_id=schedule_run_id,
        )
    except DocumentMappingError as exc:
        # The resolved profile passed resolve_scheduled_mapping()'s own
        # checks but apply_mapping_profile_to_document() re-validates
        # independently (e.g. a lookup-rule row) — same "no DB row, no
        # real version to snapshot" reasoning as the config_error branch.
        return {"status": "config_error", "error": str(exc)}
    return {"status": "applied", "mapping_status": output.status}


async def retry_scheduled_mapping(db: AsyncSession, *, schedule_id: int, organization_id: int | None) -> dict:
    """Section 7.5 — "allow retry after config correction": call this once
    an admin has fixed whatever made this schedule's mapping unresolvable
    (corrected DocumentScheduleConfig.mapping_id, published a pending
    mapping version, or fixed the organisation's default for this
    document_type) to re-attempt mapping for every document this
    schedule has ever successfully extracted.

    There is deliberately no separate per-document mapping-failure ledger
    — a config_error outcome is only ever recorded as a ScheduleRun.
    document_stats counter (see apply_scheduled_mapping_if_configured's
    own docstring), not a persisted row naming which documents failed.
    Re-attempting EVERY eligible document rather than only the previously
    failed ones is therefore deliberate, not a shortcut: it is still safe
    and still satisfies "idempotent retries... no duplicate outputs"
    because apply_scheduled_mapping_if_configured()'s own idempotency
    check — keyed on (schedule_id, schedule_run_id, document_id) — skips
    anything that already has a persisted SUCCESS row for that exact
    run. The schedule_run_id used for each document is its own REAL
    DocumentSourceRegistry.processing_run_id (the run that actually
    produced it), never a synthetic value, so section 7.4's per-execution
    snapshot stays accurate even for a retry triggered well after the
    fact.
    """
    config = (
        await db.execute(select(DocumentScheduleConfig).where(DocumentScheduleConfig.schedule_id == schedule_id))
    ).scalar_one_or_none()
    if config is None:
        raise DocumentMappingError(f"Schedule {schedule_id} has no Document Source configuration.")

    registry_rows = (
        await db.execute(
            select(DocumentSourceRegistry).where(
                DocumentSourceRegistry.schedule_id == schedule_id,
                DocumentSourceRegistry.status.in_([DocumentSourceStatus.PROCESSED, DocumentSourceStatus.REVIEW_REQUIRED]),
                DocumentSourceRegistry.document_id.isnot(None),
                DocumentSourceRegistry.processing_run_id.isnot(None),
            )
        )
    ).scalars().all()

    counts = {
        "candidates": len(registry_rows), "applied_success": 0, "applied_failed": 0,
        "already_applied": 0, "config_error": 0, "no_mapping_configured": 0, "last_error": None,
    }
    for reg in registry_rows:
        doc = await db.get(Document, reg.document_id)
        if doc is None:
            continue
        outcome = await apply_scheduled_mapping_if_configured(
            db, document=doc, schedule_id=schedule_id, schedule_run_id=reg.processing_run_id,
            schedule_mapping_id=config.mapping_id, document_type=config.document_type,
            organization_id=organization_id,
        )
        status_ = outcome["status"]
        if status_ == "applied":
            counts["applied_success" if outcome["mapping_status"] == "SUCCESS" else "applied_failed"] += 1
        elif status_ == "config_error":
            counts["config_error"] += 1
            counts["last_error"] = outcome["error"]
        else:  # "already_applied" | "no_mapping_configured"
            counts[status_] += 1
    return counts
