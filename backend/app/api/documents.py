"""Documents router — /api/documents/...

Documents is the repository/management layer; processing calls flow through
app.services.document_service (DocumentProcessingService), which is the seam
to the Document Intelligence pipeline. See that module's docstring.
"""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_organization, get_current_user, require_action
from app.database.tenant_routing import get_tenant_db
from app.models.document import DocumentStatus
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.schemas.documents import (
    BulkActionRequest,
    DocumentDetailOut,
    DocumentFacetsOut,
    DocumentListOut,
    DocumentOut,
    DocumentStatsOut,
    DocumentUpdateRequest,
    ManualExtractionRequest,
    RunExtractionRequest,
    SendToReviewRequest,
)
from app.services import document_service as svc
from app.services import document_taxonomies
from app.services import review_queue_service as review_svc
from app.services.document_service import DocumentValidationError, ManualExtractionError

router = APIRouter(prefix="/documents", tags=["documents"])


def _effective_org_id(user: User) -> int | None:
    """super_admin sees across all orgs; everyone else is scoped to their own."""
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")


async def _get_or_404(db: AsyncSession, document_id: int, user: User):
    doc = await svc.get_document(db, document_id, _effective_org_id(user))
    if doc is None:
        raise _not_found()
    return doc


def _to_out(doc, name_map: dict[int, str]) -> DocumentOut:
    out = DocumentOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


# ── KPIs / facets ─────────────────────────────────────────────────

@router.get("/stats", response_model=DocumentStatsOut)
async def stats(
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await svc.get_stats(db, _effective_org_id(user))


@router.get("/facets", response_model=DocumentFacetsOut)
async def facets(
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await svc.get_facets(db, _effective_org_id(user))


@router.get("/document-types")
async def list_document_types(user: User = Depends(require_action("VIEW"))):
    """Canonical document types available for the "Run AI Extraction"
    document-type picker (Document Details / Review Queue) — see
    document_taxonomies.py. An empty/omitted type keeps the original
    ad-hoc schema-detection behavior. Declared here (before the generic
    GET /{document_id} below) since routes are matched in declaration
    order — after it, "/document-types" would be swallowed as
    document_id="document-types" and 422 instead of reaching this."""
    return {"document_types": document_taxonomies.available_document_types()}


# ── List / get ────────────────────────────────────────────────────

@router.get("", response_model=DocumentListOut)
async def list_documents(
    q: str = Query("", description="Search name, id, type, uploader, status, pipeline, extracted entity"),
    doc_type: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    date_preset: str | None = Query(None, description="today|yesterday|last_7_days|last_30_days|custom"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    owner: str | None = Query(None, description="current_user|organization|specific_user"),
    owner_user_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("uploaded_at"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    items, total, name_map = await svc.list_documents(
        db,
        organization_id=_effective_org_id(user),
        requesting_user_id=user.id,
        search=q,
        doc_type=doc_type,
        status_filter=status_filter,
        date_preset=date_preset,
        date_from=date_from,
        date_to=date_to,
        owner=owner,
        owner_user_id=owner_user_id,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    return DocumentListOut(items=[_to_out(d, name_map) for d in items], total=total, page=page, page_size=page_size)


@router.get("/{document_id}", response_model=DocumentDetailOut)
async def get_document(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    name_map = await svc.uploader_name_map(db, {doc.uploaded_by_id} if doc.uploaded_by_id else set())
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


# ── Upload ────────────────────────────────────────────────────────

@router.post("/upload", response_model=DocumentDetailOut, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    user: User = Depends(require_action("CREATE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    try:
        doc, _duplicate_of_id = await svc.save_upload(db, file=file, organization_id=user.organization_id, uploaded_by_id=user.id)
    except DocumentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = user.full_name
    return out


class BulkUploadItemOut(BaseModel):
    filename: str
    ok: bool
    document: DocumentDetailOut | None = None
    error: str | None = None
    # Set when this file's content (SHA-256) matches a document already in
    # the organization — informational, the upload still proceeds; the
    # frontend uses this to show "same as doc_X" instead of a silent extra row.
    duplicate_of_id: int | None = None


class BulkUploadResultOut(BaseModel):
    items: list[BulkUploadItemOut]


@router.post("/upload-bulk", response_model=BulkUploadResultOut, status_code=status.HTTP_201_CREATED)
async def upload_documents_bulk(
    files: list[UploadFile] = File(...),
    user: User = Depends(require_action("CREATE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Accepts multiple files in one request, saves each as its own real
    Document row (identical to /upload — no shortcuts), then immediately
    kicks off the same real background pipeline (start_processing()) for
    every one of them — OCR + AI Extraction + Structured Dataset all run
    for real, concurrently, without a separate manual "Process" step per
    file. Per-file failures don't abort the batch — each file gets its own
    ok/error result, matching /bulk's succeeded/failed pattern below.

    The except clause below is deliberately broader than just
    DocumentValidationError: this loop shares ONE db session/transaction
    across every file in the request (a single AsyncSession per FastAPI
    request), so a real, uncaught error on file N — e.g. a genuine SQLite
    "database is locked" collision with this org's own background pipeline
    tasks from a previous upload, still writing when this request lands —
    used to leave that session in SQLAlchemy's "pending rollback" state:
    every FOLLOWING file in the same batch then failed too, and the whole
    request 500'd, discarding the response for files that had already
    saved successfully. Observed live reproducing this ticket. Recovering
    with rollback() here keeps one bad file from taking the rest of the
    batch down with it."""
    items: list[BulkUploadItemOut] = []
    for f in files:
        try:
            doc, duplicate_of_id = await svc.save_upload(db, file=f, organization_id=user.organization_id, uploaded_by_id=user.id)
        except DocumentValidationError as exc:
            items.append(BulkUploadItemOut(filename=f.filename or "", ok=False, error=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - see docstring
            await db.rollback()
            items.append(BulkUploadItemOut(filename=f.filename or "", ok=False, error=f"Upload failed: {exc}"))
            continue
        try:
            doc = await svc.start_processing(db, doc, reprocess=False)
        except Exception as exc:  # noqa: BLE001 - the file itself DID save; only auto-starting processing failed, so report ok with a note rather than losing the created document from the response
            await db.rollback()
            out = DocumentDetailOut.model_validate(doc)
            out.uploaded_by_name = user.full_name
            items.append(BulkUploadItemOut(
                filename=f.filename or "", ok=True, document=out, duplicate_of_id=duplicate_of_id,
                error=f"Uploaded, but couldn't start processing automatically: {exc}. Use Process/Reprocess on the document.",
            ))
            continue
        out = DocumentDetailOut.model_validate(doc)
        out.uploaded_by_name = user.full_name
        items.append(BulkUploadItemOut(filename=f.filename or "", ok=True, document=out, duplicate_of_id=duplicate_of_id))
    return BulkUploadResultOut(items=items)


# ── Update / delete ───────────────────────────────────────────────

@router.patch("/{document_id}", response_model=DocumentDetailOut)
async def update_document(
    document_id: int,
    body: DocumentUpdateRequest,
    user: User = Depends(require_action("EDIT")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    doc = await svc.update_document(db, doc, filename=body.filename, folder=body.folder)
    name_map = await svc.uploader_name_map(db, {doc.uploaded_by_id} if doc.uploaded_by_id else set())
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: int,
    user: User = Depends(require_action("DELETE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    await svc.delete_document(db, doc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Processing ────────────────────────────────────────────────────

@router.post("/{document_id}/process", response_model=DocumentDetailOut)
async def process_document(
    document_id: int,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Starts real OCR + AI Extraction as a background task and returns
    immediately with the document's status flipped to SCANNING — does NOT
    await the pipeline itself (real Tesseract + up to two live LLM calls
    can genuinely take well over a minute; see document_service.py's
    "Background execution" section for why awaiting it inline broke the
    frontend's 30s client timeout). Poll GET /{document_id} until status
    lands on a terminal value (COMPLETED/NEEDS_REVIEW/FAILED)."""
    doc = await _get_or_404(db, document_id, user)
    doc = await svc.start_processing(db, doc, reprocess=False)
    name_map = await svc.uploader_name_map(db, {doc.uploaded_by_id} if doc.uploaded_by_id else set())
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


@router.post("/{document_id}/reprocess", response_model=DocumentDetailOut)
async def reprocess_document(
    document_id: int,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Same non-blocking start as /process above, but resets the timeline
    (reprocess=True) — see start_processing()."""
    doc = await _get_or_404(db, document_id, user)
    doc = await svc.start_processing(db, doc, reprocess=True)
    name_map = await svc.uploader_name_map(db, {doc.uploaded_by_id} if doc.uploaded_by_id else set())
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


@router.post("/{document_id}/send-to-review", response_model=DocumentDetailOut)
async def send_to_review(
    document_id: int,
    body: SendToReviewRequest | None = None,
    user: User = Depends(require_action("APPROVE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    doc = await svc.send_to_review(db, doc)
    await review_svc.create_manual(
        db, doc, requested_by=user,
        reviewer_id=body.reviewer_id if body else None,
        note=body.note if body else None,
    )
    name_map = await svc.uploader_name_map(db, {doc.uploaded_by_id} if doc.uploaded_by_id else set())
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


@router.post("/{document_id}/cancel-review", response_model=DocumentDetailOut)
async def cancel_review(
    document_id: int,
    user: User = Depends(require_action("APPROVE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    if doc.status != DocumentStatus.NEEDS_REVIEW:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This document is not currently in review.")
    await review_svc.cancel_for_document(db, document_id, user)
    doc = await _get_or_404(db, document_id, user)
    name_map = await svc.uploader_name_map(db, {doc.uploaded_by_id} if doc.uploaded_by_id else set())
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


# ── Pipeline output ───────────────────────────────────────────────

@router.get("/{document_id}/ocr")
async def get_ocr(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    if not doc.ocr_result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This document has not been OCR-processed yet.")
    return {"document_id": doc.id, **doc.ocr_result}


@router.get("/{document_id}/extraction")
async def get_extraction(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    if not doc.extraction_result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This document has not been through AI extraction yet.")
    return {
        "document_id": doc.id,
        "fields": doc.extraction_result.get("fields", []),
        "overall_confidence": doc.extraction_percent,
    }


@router.get("/{document_id}/dataset")
async def get_dataset(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    if not doc.dataset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No structured dataset has been generated for this document yet.")
    return {
        "document_id": doc.id,
        "dataset_schema": doc.dataset.get("schema", []),
        "table": doc.dataset.get("table", {"columns": [], "rows": []}),
        "json_view": doc.dataset.get("json", {}),
    }


@router.post("/{document_id}/run-extraction", response_model=DocumentDetailOut)
async def run_extraction(
    document_id: int,
    body: RunExtractionRequest | None = None,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Explicit, user-triggered AI Extraction — the option missing from
    the review flow ("no AI extraction option while reviewing"). Can be
    re-run any time OCR text exists, optionally with a document_type hint
    (e.g. "INVOICE") that seeds a comprehensive canonical schema instead
    of an ad-hoc LLM guess — see document_service.run_manual_ai_extraction()."""
    doc = await _get_or_404(db, document_id, user)
    try:
        doc = await svc.run_manual_ai_extraction(
            db, doc,
            document_type=(body.document_type if body else None),
            connection_id=(body.connection_id if body else None),
        )
    except ManualExtractionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name_map = await svc.uploader_name_map(db, {doc.uploaded_by_id} if doc.uploaded_by_id else set())
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


@router.post("/{document_id}/manual-extraction", response_model=DocumentDetailOut)
async def manual_extraction(
    document_id: int,
    body: ManualExtractionRequest,
    user: User = Depends(require_action("EDIT")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Lets the user build structured data by hand from the document's OCR
    text when AI Extraction found nothing (no AI connection configured, or
    the model came back empty) — see document_service.apply_manual_extraction()
    for exactly how this is stored (same shape AI Extraction produces, so
    every downstream consumer treats it identically)."""
    doc = await _get_or_404(db, document_id, user)
    try:
        doc = await svc.apply_manual_extraction(
            db, doc,
            fields_in=[f.model_dump() for f in body.fields],
            tables_in=[t.model_dump() for t in body.tables],
        )
    except svc.ManualExtractionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name_map = await svc.uploader_name_map(db, {doc.uploaded_by_id} if doc.uploaded_by_id else set())
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


# ── File access ───────────────────────────────────────────────────

@router.get("/{document_id}/file")
async def view_file(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    content = svc.read_file(doc)
    return Response(content=content, media_type=doc.mime_type or "application/octet-stream")


@router.get("/{document_id}/download")
async def download_file(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    content = svc.read_file(doc)
    return Response(
        content=content,
        media_type=doc.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'},
    )


# ── Bulk operations ───────────────────────────────────────────────

@router.post("/bulk")
async def bulk_action(
    body: BulkActionRequest,
    user: User = Depends(get_current_user),
    org: Organization | None = Depends(get_current_organization),
    db: AsyncSession = Depends(get_tenant_db),
):
    action_map = {"process": "EXECUTE", "send_to_review": "APPROVE", "delete": "DELETE"}
    required_action = action_map.get(body.action)
    if not required_action:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown bulk action '{body.action}'.")

    from app.core.deps import ROLE_ACTIONS, DOCUMENT_INTELLIGENCE_MODULE
    if user.role != UserRole.SUPER_ADMIN and required_action not in ROLE_ACTIONS.get(user.role, set()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Your role does not have permission to '{body.action}' documents.")
    if org is not None and DOCUMENT_INTELLIGENCE_MODULE not in (org.licensed_modules or []):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Document Intelligence is not licensed for your organization.")

    results = {"succeeded": [], "failed": []}
    for doc_id in body.document_ids:
        doc = await svc.get_document(db, doc_id, _effective_org_id(user))
        if doc is None:
            results["failed"].append({"id": doc_id, "reason": "not_found"})
            continue
        try:
            if body.action == "process":
                # Same non-blocking start as the single-document /process
                # route — "succeeded" here means "queued for real
                # processing", not "finished"; poll each document's status
                # to see completion (see document_service.py's
                # "Background execution" section for why this doesn't
                # await the pipeline inline).
                doc = await svc.start_processing(db, doc, reprocess=False)
            elif body.action == "send_to_review":
                doc = await svc.send_to_review(db, doc)
                await review_svc.create_manual(
                    db, doc, requested_by=user, reviewer_id=body.reviewer_id, note=body.note,
                )
            elif body.action == "delete":
                await svc.delete_document(db, doc)
            results["succeeded"].append(doc_id)
        except Exception as exc:  # noqa: BLE001
            results["failed"].append({"id": doc_id, "reason": str(exc)})

    return results
