"""Documents router — /api/documents/...

Documents is the repository/management layer; processing calls flow through
app.services.document_service (DocumentProcessingService), which is the seam
to the Document Intelligence pipeline. See that module's docstring.
"""
import asyncio

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from fastapi.responses import Response
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_organization, get_current_user, require_action
from app.database.tenant_routing import get_tenant_db
from app.models.document import DocumentStatus
from app.models.document_batch import BatchUpload, DocumentBatch, DocumentBatchPipelineType, DuplicatePolicy
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.ocr import multilingual_ocr_service
from app.ocr.engine import OcrEngineError, OcrNotApplicableError
from app.ocr.language_errors import LanguageProcessingError
from app.schemas.document_batch import (
    BatchUploadOut,
    CompleteZipUploadOut,
    CompleteZipUploadRequest,
    DocumentBatchEventListOut,
    DocumentBatchEventOut,
    DocumentBatchFileListOut,
    DocumentBatchFileOut,
    DocumentBatchListOut,
    DocumentBatchMetricsOut,
    DocumentBatchOut,
    InitZipUploadRequest,
    RetrySelectedRequest,
)
from app.schemas.document_mapping import DocumentMappedOutputListOut, DocumentMappedOutputOut
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
from app.schemas.review_queue import ReviewItemLookupOut
from app.services import batch_upload_service as upload_svc
from app.services import document_batch_service as batch_svc
from app.services import document_mapping_service as doc_mapping_svc
from app.services import document_service as svc
from app.services import document_taxonomies
from app.services import review_queue_service as review_svc
from app.services import settings_service as settings_svc
from app.services.batch_upload_service import BatchUploadError
from app.services.document_batch_service import DocumentBatchError
from app.services.document_mapping_service import DocumentMappingError
from app.services.document_service import DocumentLanguageConfigError, DocumentValidationError, ManualExtractionError

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


class ProcessDocumentRequest(BaseModel):
    language_config: dict | None = None


def _language_config_from_form(language_mode: str | None, selected_languages: str | None, primary_language: str | None) -> dict | None:
    if not language_mode and not selected_languages and not primary_language:
        return None
    languages = [c.strip() for c in (selected_languages or "").split(",") if c.strip()]
    return {"mode": language_mode or "auto", "selected_languages": languages, "primary_language": primary_language or "auto"}


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


# ── Multilingual OCR diagnostics (Phase 11) ─────────────────────────
# Declared here, before the generic GET/{document_id} route, for the same
# route-ordering reason as /document-types above.

@router.get("/ocr-health")
async def ocr_health(
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Real Tesseract readiness check for Administration > Document
    Intelligence > Languages — reports, for every language the org has
    actually opted into (Settings' supported_languages), whether that
    language's real .traineddata file is installed on THIS server right
    now (a live subprocess call, not a cached/assumed value — see
    multilingual_ocr_service.build_health_report's docstring). Lets an
    admin catch "I turned on Kannada but the server doesn't have the
    Kannada language pack" before it surfaces as a real document failure."""
    org_settings = await settings_svc.get_or_create(db, user.organization_id) if user.organization_id else None
    di_settings = settings_svc.effective_document_intelligence_settings(org_settings)
    supported_languages = di_settings.get("supported_languages") or ["en"]
    return multilingual_ocr_service.build_health_report(supported_languages)


@router.post("/ocr-test")
async def ocr_test(
    file: UploadFile = File(...),
    supported_languages: str | None = Form(None, description="Comma-separated ISO codes, e.g. \"en,kn\" — defaults to the org's own Settings value"),
    primary_language: str | None = Form(None, description="Pin one language, or omit/\"auto\" to use the org's own Settings value"),
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Admin OCR sandbox — runs the real multilingual Tesseract pipeline
    (the exact same run_multilingual_ocr() + evaluate_ocr_quality() calls
    document_service.py's real pipeline uses) against an uploaded file
    WITHOUT creating a Document row, so an admin can validate a language
    configuration change against a real sample before rolling it out.
    Disclosed scope: this only exercises the local Tesseract engine — if
    the org's OCR Provider setting is a cloud provider (AWS Textract/
    Google Vision/Azure Form Recognizer), this sandbox does not call that
    provider; it tests the language config Tesseract would use if the
    provider setting were Tesseract."""
    file_bytes = await file.read()
    doc_type = svc.guess_doc_type(file.filename or "")

    org_settings = await settings_svc.get_or_create(db, user.organization_id) if user.organization_id else None
    di_settings = settings_svc.effective_document_intelligence_settings(org_settings)
    langs = (
        [c.strip() for c in supported_languages.split(",") if c.strip()]
        if supported_languages else (di_settings.get("supported_languages") or ["en"])
    )
    primary = primary_language if primary_language else (di_settings.get("primary_language") or "auto")

    try:
        ocr_result = await asyncio.to_thread(
            multilingual_ocr_service.run_multilingual_ocr, file_bytes, doc_type,
            supported_languages=langs, primary_language=primary,
        )
    except (OcrEngineError, OcrNotApplicableError, LanguageProcessingError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    quality_flags = multilingual_ocr_service.evaluate_ocr_quality(
        ocr_result,
        ocr_confidence_threshold=di_settings.get("ocr_confidence_threshold", 85),
        language_mismatch_threshold=di_settings.get("language_mismatch_threshold", 80),
        supported_languages=langs, primary_language=primary,
    )
    return {
        "filename": file.filename,
        "doc_type": doc_type.value,
        "full_text": ocr_result["full_text"],
        "confidence": ocr_result["confidence"],
        "page_count": ocr_result.get("page_count"),
        "language": ocr_result.get("language"),
        "quality_flags": quality_flags,
    }


# ── ZIP Batch Upload (ZB10) — list route only ───────────────────────
# GET /zip-batches (no id) is a single-segment path exactly like
# GET /{document_id} below — declared here, before that catch-all, for
# the identical route-ordering reason as /document-types and /ocr-health
# above (a real 422 found live: Starlette matched "zip-batches" against
# {document_id} and tried to parse it as an int). Every OTHER ZB10 route
# has 2+ segments after the prefix (/zip-upload/..., /zip-batches/{id},
# /zip-batches/{id}/...) so none of those can collide with a single-
# segment catch-all regardless of declaration order — only this one list
# route needed to move. See the full ZB10 section further down in this
# file for the rest of the batch-upload/batch-management routes.

@router.get("/zip-batches", response_model=DocumentBatchListOut)
async def list_zip_batches(
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    items, total = await batch_svc.list_batches(
        db, _effective_org_id(user), DocumentBatchPipelineType.DOCUMENT_INTELLIGENCE, status_filter=status_filter, page=page, page_size=page_size,
    )
    return DocumentBatchListOut(items=[DocumentBatchOut.model_validate(b) for b in items], total=total, page=page, page_size=page_size)


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
    language_mode: str | None = Form(None),
    selected_languages: str | None = Form(None),
    primary_language: str | None = Form(None),
    user: User = Depends(require_action("CREATE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    try:
        doc, _duplicate_of_id = await svc.save_upload(
            db, file=file, organization_id=user.organization_id, uploaded_by_id=user.id,
            language_config=_language_config_from_form(language_mode, selected_languages, primary_language),
        )
    except DocumentLanguageConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except DocumentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except OperationalError as exc:
        # save_upload()'s own db.commit() can genuinely collide with another
        # document's background pipeline holding SQLite's single-writer
        # lock (found live via /api/invoices/upload — same save_upload()
        # call). Left uncaught this surfaces as a bare 500 with no
        # `detail`; give the same honest, retryable message
        # process_document() itself already gives for the identical
        # collision — see document_service.friendly_db_error's docstring.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, svc.friendly_db_error(exc))
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
    body: ProcessDocumentRequest | None = Body(None),
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
    try:
        doc = await svc.start_processing(db, doc, reprocess=False, language_config=(body.language_config if body else None))
    except DocumentLanguageConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name_map = await svc.uploader_name_map(db, {doc.uploaded_by_id} if doc.uploaded_by_id else set())
    out = DocumentDetailOut.model_validate(doc)
    out.uploaded_by_name = name_map.get(doc.uploaded_by_id) if doc.uploaded_by_id else None
    return out


@router.post("/{document_id}/reprocess", response_model=DocumentDetailOut)
async def reprocess_document(
    document_id: int,
    body: ProcessDocumentRequest | None = Body(None),
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Same non-blocking start as /process above, but resets the timeline
    (reprocess=True) — see start_processing()."""
    doc = await _get_or_404(db, document_id, user)
    try:
        doc = await svc.start_processing(db, doc, reprocess=True, language_config=(body.language_config if body else None))
    except DocumentLanguageConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
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


@router.get("/{document_id}/review-item", response_model=ReviewItemLookupOut)
async def get_review_item(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Real bug found live (user-reported): the Document Details page's
    "Send to Review"/"Cancel Review" pair had no matching "Review" action
    — a NEEDS_REVIEW document gave the reviewer no way to actually reach
    the Review Queue item to correct the data. This is what the new
    "Review" button (DocumentDetails.jsx) calls to get the review_id it
    then navigates to (/doc-intel/review/{review_id})."""
    await _get_or_404(db, document_id, user)  # org-scoped existence/access check, same gate cancel-review uses
    review_id = await review_svc.get_open_item_id_for_document(db, document_id)
    return ReviewItemLookupOut(review_id=review_id)


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


@router.get("/{document_id}/ai-raw-response")
async def get_ai_raw_response(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Verbatim raw AI model payload from extraction (Document.ai_raw_response
    — persisted since INV3 for downstream consumers like Invoice
    Intelligence's canonical materialization) but never exposed to a
    client until now: a real gap found live during Processed Data Storage
    research — every other processed-output field already had a
    dedicated GET route (/ocr, /extraction, /dataset above), this one
    didn't."""
    doc = await _get_or_404(db, document_id, user)
    if not doc.ai_raw_response:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No raw AI response has been recorded for this document yet.")
    return {"document_id": doc.id, "ai_raw_response": doc.ai_raw_response}


@router.get("/{document_id}/metadata")
async def get_metadata(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Document-level metadata only — filename/type/status/size/pipeline
    timestamps, no OCR/extraction/dataset payload. A real gap found during
    Processed Data Storage spec review (item #6): every processed-output
    field had its own GET route, but a consumer that only wants to know
    WHAT a document is (for a catalog listing, a webhook payload preview,
    etc.) had to fetch the full /ocr or /dataset body to get there."""
    doc = await _get_or_404(db, document_id, user)
    return {
        "document_id": doc.id,
        "filename": doc.filename,
        "doc_type": doc.doc_type.value if doc.doc_type else None,
        "status": doc.status.value if doc.status else None,
        "confidence": doc.confidence,
        "pipeline": doc.pipeline,
        "folder": doc.folder,
        "mime_type": doc.mime_type,
        "size_bytes": doc.size_bytes,
        "page_count": doc.page_count,
        "uploaded_by_id": doc.uploaded_by_id,
        "processing_started_at": doc.processing_started_at,
        "processing_completed_at": doc.processing_completed_at,
        "processing_time_seconds": doc.processing_time_seconds,
    }


class DocumentExportRequest(BaseModel):
    """spec item #6's on-demand export — reads from the ALREADY-PERSISTED
    DocumentProcessedDataset rows (see models/document_storage.py), never
    triggers a reprocess, which is the "decoupled from reprocessing"
    requirement: this returns/pushes whatever was captured the last time
    the document actually finished processing, on demand, as many times
    as asked, independent of that processing run's own lifecycle."""
    datasets: list[str] | None = None  # None = every dataset type this document has; else a subset of _EXPORT_DATASET_ALIASES keys
    destination: str = "inline"  # "inline" (return the JSON body directly) | "connection" (re-push via the org's configured Processed Data Storage target)


@router.post("/{document_id}/export")
async def export_document(
    document_id: int,
    body: DocumentExportRequest = Body(default=DocumentExportRequest()),
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)

    if body.destination == "connection":
        from app.services import document_storage_service as storage_svc

        config = await storage_svc.get_or_create_config(db, user.organization_id)
        if config.mode.value != "EXTERNAL" or not config.target_connection_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "No External Processed Data Storage destination is configured for this organization — "
                "configure one under Settings > Processed Data Storage, or use destination=\"inline\".",
            )
        await storage_svc.push_document(db, doc, organization_id=user.organization_id)
        await db.refresh(config)
        return {
            "document_id": doc.id, "destination": "connection", "status": config.last_status.value,
            "error": config.last_error,
        }

    if body.destination != "inline":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown destination '{body.destination}' — use \"inline\" or \"connection\".")

    from app.services import document_storage_service as storage_svc

    try:
        datasets = await storage_svc.export_document_datasets(db, document_id, body.datasets)
    except storage_svc.DocumentStorageServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    return {"document_id": doc.id, "exported_at": doc.processing_completed_at, "datasets": datasets}


@router.get("/{document_id}/pipeline")
async def get_linked_pipeline(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Open Pipeline bug fix — lets the frontend check for an existing
    pipeline before building a new one from this document's dataset, so
    re-clicking "Open Pipeline" reopens the same pipeline rather than
    creating a duplicate every time. See pipeline_service.create_pipeline()'s
    document_id param for where that pipeline gets linked on first Save."""
    await _get_or_404(db, document_id, user)
    from app.services import pipeline_service
    pipeline = await pipeline_service.get_pipeline_for_document(db, document_id, _effective_org_id(user))
    return {"pipeline_id": pipeline.id if pipeline else None}


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
            acting_user_id=user.id,
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


# ── Document Mapping Profiles (MP8) ──────────────────────────────
# Applying a PUBLISHED, document-sourced mapping profile to one real
# document — definition/versioning itself lives entirely under
# /mappings (see api/mapping.py + models/column_mapping.py's module
# docstring for why no second system exists here).

@router.post("/{document_id}/apply-mapping/{mapping_id}", response_model=DocumentMappedOutputOut, status_code=status.HTTP_201_CREATED)
async def apply_mapping(
    document_id: int,
    mapping_id: int,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    doc = await _get_or_404(db, document_id, user)
    org_settings = await settings_svc.get_or_create(db, user.organization_id) if user.organization_id else None
    di_settings = settings_svc.effective_document_intelligence_settings(org_settings)
    if not di_settings.get("mapping_profiles_enabled"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Mapping Profiles are not enabled for this organization — turn on "
            "\"Mapping profiles\" under Administration > Settings > Document Intelligence first.",
        )
    try:
        output = await doc_mapping_svc.apply_mapping_profile_to_document(
            db, document=doc, mapping_id=mapping_id, organization_id=_effective_org_id(user), created_by=user.id,
        )
    except DocumentMappingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return output


@router.get("/{document_id}/mapped-outputs", response_model=DocumentMappedOutputListOut)
async def list_mapped_outputs(
    document_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_or_404(db, document_id, user)
    items = await doc_mapping_svc.list_mapped_outputs(db, document_id, _effective_org_id(user))
    return DocumentMappedOutputListOut(items=items, total=len(items))


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

    # Real bug found live: an empty document_ids list used to sail straight
    # through this route and the for-loop below simply never ran, returning
    # HTTP 200 with {"succeeded": [], "failed": []} — a silent no-op even
    # for a direct API call (not just the now-disabled frontend button).
    # Structured error detail (not just a plain string) mirrors the
    # {error, message} shape ApprovalRules.jsx/MyApprovals.jsx's
    # apiErrorMessage() already knows how to unwrap (err.response.data.
    # detail.message), so this doesn't invent a new response convention.
    if not body.document_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "NO_JOB_SELECTED", "message": "Please select at least one job to process."},
        )

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


# ── ZIP Batch Upload (ZB10) ──────────────────────────────────────────────
# Thin pass-through routes over batch_upload_service.py/document_batch_
# service.py — no business logic here; see those modules (ZB1-ZB9) for the
# real chunked-upload/extraction/worker-pool implementation. Fixed to the
# DOCUMENT_INTELLIGENCE pipeline_type/module for this router; an identical
# route set exists in api/invoices.py fixed to INVOICE_INTELLIGENCE
# instead — require_action's module argument is baked in at decoration
# time (see core/deps.py), so one shared parameterized router can't branch
# on a path variable the way these two fixed routers can.

_ZIP_PIPELINE_TYPE = DocumentBatchPipelineType.DOCUMENT_INTELLIGENCE


def _not_found_zip_batch() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Batch not found")


def _not_found_zip_upload() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Upload not found")


async def _get_zip_batch_or_404(db: AsyncSession, batch_id: int, user: User) -> DocumentBatch:
    batch = await batch_svc.get_batch(db, batch_id, _effective_org_id(user))
    if batch is None or batch.pipeline_type != _ZIP_PIPELINE_TYPE:
        raise _not_found_zip_batch()
    return batch


async def _get_zip_upload_or_404(db: AsyncSession, upload_id: int, user: User) -> BatchUpload:
    upload = await upload_svc.get_upload(db, upload_id, _effective_org_id(user))
    if upload is None:
        raise _not_found_zip_upload()
    return upload


@router.post("/zip-upload/init", response_model=BatchUploadOut, status_code=status.HTTP_201_CREATED)
async def init_zip_upload(
    body: InitZipUploadRequest,
    user: User = Depends(require_action("CREATE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    try:
        upload = await upload_svc.init_upload(
            db, organization_id=user.organization_id, filename=body.filename, size_bytes=body.size_bytes,
            chunk_size=body.chunk_size, total_chunks=body.total_chunks, uploaded_by=user.id,
        )
    except BatchUploadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return BatchUploadOut.model_validate(upload)


@router.post("/zip-upload/{upload_id}/chunk", response_model=BatchUploadOut)
async def upload_zip_chunk(
    upload_id: int,
    chunk_number: int = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(require_action("CREATE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    upload = await _get_zip_upload_or_404(db, upload_id, user)
    data = await file.read()
    try:
        upload = await upload_svc.receive_chunk(db, upload=upload, chunk_number=chunk_number, data=data)
    except BatchUploadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return BatchUploadOut.model_validate(upload)


@router.get("/zip-upload/{upload_id}/status")
async def get_zip_upload_status(
    upload_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    upload = await _get_zip_upload_or_404(db, upload_id, user)
    return await upload_svc.get_status(db, upload)


@router.post("/zip-upload/{upload_id}/complete", response_model=CompleteZipUploadOut)
async def complete_zip_upload(
    upload_id: int,
    body: CompleteZipUploadRequest = Body(default_factory=CompleteZipUploadRequest),
    user: User = Depends(require_action("CREATE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    upload = await _get_zip_upload_or_404(db, upload_id, user)
    try:
        upload, is_duplicate, duplicate_of_upload_id = await upload_svc.complete_upload(
            db, upload=upload, expected_sha256=body.expected_sha256,
        )
    except BatchUploadError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    try:
        duplicate_policy = DuplicatePolicy(body.duplicate_policy)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown duplicate_policy '{body.duplicate_policy}'.")

    try:
        batch = await batch_svc.create_batch_from_upload(
            db, upload=upload, pipeline_type=_ZIP_PIPELINE_TYPE, duplicate_policy=duplicate_policy,
            auto_process=body.auto_process, created_by=user.id,
            storage_type=body.storage_type, storage_target_connection_id=body.storage_target_connection_id,
            storage_target_schema=body.storage_target_schema, storage_target_table=body.storage_target_table,
            storage_load_mode=body.storage_load_mode, storage_file_format=body.storage_file_format,
        )
    except Exception as exc:  # noqa: BLE001 - create_batch_from_upload() intentionally re-raises the
        # original ZipSecurityError (after marking the batch FAILED — see that function's own
        # docstring) for corrupted/oversized/malicious archives; the failed DocumentBatch row is
        # the durable record of what happened, so surface the real message as a 400 here rather
        # than a bare, unexplained 500.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return CompleteZipUploadOut(
        upload=BatchUploadOut.model_validate(upload), is_duplicate_upload=is_duplicate,
        duplicate_of_upload_id=duplicate_of_upload_id, batch=DocumentBatchOut.model_validate(batch),
    )


@router.post("/zip-upload/{upload_id}/cancel", response_model=BatchUploadOut)
async def cancel_zip_upload(
    upload_id: int,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    upload = await _get_zip_upload_or_404(db, upload_id, user)
    upload = await upload_svc.cancel_upload(db, upload)
    return BatchUploadOut.model_validate(upload)


@router.get("/zip-batches/{batch_id}", response_model=DocumentBatchOut)
async def get_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    return DocumentBatchOut.model_validate(batch)


@router.post("/zip-batches/{batch_id}/start", response_model=DocumentBatchOut)
async def start_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    """No-op (idempotent) unless the batch is genuinely QUEUED and waiting
    on this manual trigger — see start_processing_for_batch()'s docstring."""
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    await batch_svc.start_processing_for_batch(db, batch)
    return DocumentBatchOut.model_validate(batch)


@router.post("/zip-batches/{batch_id}/pause", response_model=DocumentBatchOut)
async def pause_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    try:
        batch = await batch_svc.request_pause(db, batch)
    except DocumentBatchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return DocumentBatchOut.model_validate(batch)


@router.post("/zip-batches/{batch_id}/resume", response_model=DocumentBatchOut)
async def resume_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    try:
        batch = await batch_svc.resume_batch(db, batch)
    except DocumentBatchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return DocumentBatchOut.model_validate(batch)


@router.post("/zip-batches/{batch_id}/cancel", response_model=DocumentBatchOut)
async def cancel_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    batch = await batch_svc.request_cancel(db, batch)
    return DocumentBatchOut.model_validate(batch)


@router.post("/zip-batches/{batch_id}/retry-failed", response_model=DocumentBatchOut)
async def retry_failed_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    try:
        batch = await batch_svc.retry_failed(db, batch)
    except DocumentBatchError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return DocumentBatchOut.model_validate(batch)


@router.post("/zip-batches/{batch_id}/retry-selected", response_model=DocumentBatchOut)
async def retry_selected_zip_batch(
    batch_id: int,
    body: RetrySelectedRequest,
    user: User = Depends(require_action("EXECUTE")),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    batch = await batch_svc.retry_selected(db, batch, body.file_ids)
    return DocumentBatchOut.model_validate(batch)


@router.get("/zip-batches/{batch_id}/files", response_model=DocumentBatchFileListOut)
async def list_zip_batch_files(
    batch_id: int,
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_zip_batch_or_404(db, batch_id, user)
    items, total = await batch_svc.list_batch_files_paginated(db, batch_id, status_filter=status_filter, page=page, page_size=page_size)
    return DocumentBatchFileListOut(items=[DocumentBatchFileOut.model_validate(f) for f in items], total=total, page=page, page_size=page_size)


@router.get("/zip-batches/{batch_id}/events", response_model=DocumentBatchEventListOut)
async def list_zip_batch_events(
    batch_id: int,
    since_id: int = Query(0),
    limit: int = Query(200, ge=1, le=1000),
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_zip_batch_or_404(db, batch_id, user)
    events, new_since_id = await batch_svc.list_batch_events(db, batch_id, since_id=since_id, limit=limit)
    return DocumentBatchEventListOut(items=[DocumentBatchEventOut.model_validate(e) for e in events], since_id=new_since_id)


@router.get("/zip-batches/{batch_id}/metrics", response_model=DocumentBatchMetricsOut)
async def get_zip_batch_metrics(
    batch_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    return await batch_svc.get_batch_metrics(db, batch)


@router.get("/zip-batches/{batch_id}/errors", response_model=DocumentBatchFileListOut)
async def list_zip_batch_errors(
    batch_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_zip_batch_or_404(db, batch_id, user)
    items, total = await batch_svc.list_batch_errors_paginated(db, batch_id, page=page, page_size=page_size)
    return DocumentBatchFileListOut(items=[DocumentBatchFileOut.model_validate(f) for f in items], total=total, page=page, page_size=page_size)


@router.get("/zip-batches/{batch_id}/errors/download")
async def download_zip_batch_errors(
    batch_id: int,
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_zip_batch_or_404(db, batch_id, user)
    content, media_type, filename = await batch_svc.export_batch_errors_csv(db, batch_id)
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
