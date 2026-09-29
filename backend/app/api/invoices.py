"""Invoice Intelligence router — /api/invoices/... (INV8).

Invoice Inbox + the core invoice-centric routes. Upload/OCR/AI-extraction/
reprocess all flow straight through the EXISTING Document Intelligence
pipeline (app.services.document_service) — this router is a thin,
invoice-shaped view over that plus the genuinely new Invoice Intelligence
layer (app.services.invoice.*). Nothing here re-implements OCR,
extraction, or Review Queue.
"""
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.document_batch import BatchUpload, DocumentBatch, DocumentBatchPipelineType, DuplicatePolicy
from app.models.invoice_intelligence.core import ApInvoice
from app.models.user import User, UserRole
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
from app.schemas.invoices import (
    ApInvoiceAuditEntryOut,
    ApInvoiceAuditEventOut,
    ApInvoiceCorrectionRequest,
    ApInvoiceDetailOut,
    ApInvoiceErpPreviewOut,
    ApInvoiceErpTransactionOut,
    ApInvoiceEvaluationMetricsOut,
    ApInvoiceEvidenceOut,
    ApInvoiceHistoryOut,
    ApInvoiceLearningRecordListOut,
    ApInvoiceLearningRecordOut,
    ApInvoiceListOut,
    ApInvoiceOut,
    ApInvoicePostRequest,
    ApInvoiceReconciliationOut,
    ApInvoiceRecurringCorrectionByFieldOut,
    ApInvoiceRecurringCorrectionByVendorOut,
    ApInvoiceStatsOut,
    ApInvoiceUploadOut,
    ApInvoiceValidationResultOut,
)
from app.services import batch_upload_service as upload_svc
from app.services import document_batch_service as batch_svc
from app.services import document_service as doc_svc
from app.services.batch_upload_service import BatchUploadError
from app.services.document_batch_service import DocumentBatchError
from app.services.document_service import DocumentLanguageConfigError, DocumentValidationError
from app.services.invoice import (
    invoice_erp_service,
    invoice_evaluation_service,
    invoice_inbox_service as svc,
    invoice_learning_service,
    invoice_reconciliation_service,
)

router = APIRouter(prefix="/invoices", tags=["invoices"])

INVOICE_MODULE = "INVOICE_INTELLIGENCE"


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")


async def _get_or_404(db: AsyncSession, invoice_id: int, user: User) -> ApInvoice:
    invoice = await svc.get_invoice(db, invoice_id, _effective_org_id(user))
    if invoice is None:
        raise _not_found()
    return invoice


async def _to_out(invoice: ApInvoice, filename: str | None = None) -> ApInvoiceOut:
    out = ApInvoiceOut.model_validate(invoice)
    out.document_filename = filename
    return out


async def _to_detail_out(db: AsyncSession, invoice: ApInvoice) -> ApInvoiceDetailOut:
    doc = await doc_svc.get_document(db, invoice.document_id, invoice.organization_id)
    lines = await svc.get_lines(db, invoice.id)
    results = await svc.get_validation_results(db, invoice.id)
    out = ApInvoiceDetailOut.model_validate(invoice)
    out.lines = lines
    out.validation_results = [ApInvoiceValidationResultOut.model_validate(r) for r in results]
    if doc:
        out.document_filename = doc.filename
        out.document_status = doc.status.value if hasattr(doc.status, "value") else doc.status
        out.document_mime_type = doc.mime_type
    return out


# ── KPIs ─────────────────────────────────────────────────────────

@router.get("/stats", response_model=ApInvoiceStatsOut)
async def stats(user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    return await svc.get_stats(db, _effective_org_id(user))


# ── Learning / evaluation (INV13) — static-prefix routes registered
# before "/{invoice_id}" so they aren't swallowed by that catch-all. ──

@router.get("/learning/metrics", response_model=ApInvoiceEvaluationMetricsOut)
async def learning_metrics(user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    return await invoice_evaluation_service.get_metrics(db, _effective_org_id(user))


@router.get("/learning/recurring-corrections/by-field", response_model=list[ApInvoiceRecurringCorrectionByFieldOut])
async def recurring_corrections_by_field(
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    return await invoice_evaluation_service.get_recurring_corrections_by_field(db, _effective_org_id(user), limit)


@router.get("/learning/recurring-corrections/by-vendor", response_model=list[ApInvoiceRecurringCorrectionByVendorOut])
async def recurring_corrections_by_vendor(
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    return await invoice_evaluation_service.get_recurring_corrections_by_vendor(db, _effective_org_id(user), limit)


@router.get("/learning/records", response_model=ApInvoiceLearningRecordListOut)
async def list_learning_records(
    field_name: str | None = Query(None), vendor_id: int | None = Query(None), approved_only: bool = Query(False),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    items, total = await invoice_learning_service.list_learning_records(
        db, _effective_org_id(user), field_name=field_name, vendor_id=vendor_id, approved_only=approved_only,
        limit=page_size, offset=(page - 1) * page_size,
    )
    return ApInvoiceLearningRecordListOut(items=items, total=total, page=page, page_size=page_size)


@router.post("/learning/records/{record_id}/approve", response_model=ApInvoiceLearningRecordOut)
async def approve_learning_record(
    record_id: int, user: User = Depends(require_action("EDIT", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db),
):
    try:
        return await invoice_learning_service.approve_for_learning(db, record_id, _effective_org_id(user))
    except invoice_learning_service.InvoiceLearningError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


# ── List / get ────────────────────────────────────────────────────

@router.get("", response_model=ApInvoiceListOut)
async def list_invoices(
    q: str = Query("", description="Search invoice number, vendor, PO"),
    status_filter: str | None = Query(None, alias="status"),
    decision: str | None = Query(None),
    vendor: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("created_at"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    try:
        items, total = await svc.list_invoices(
            db, _effective_org_id(user), q=q, status_filter=status_filter, decision_filter=decision,
            vendor=vendor, date_from=date_from, date_to=date_to, page=page, page_size=page_size,
            sort_by=sort_by, sort_dir=sort_dir,
        )
    except svc.InvoiceInboxError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    doc_ids = {i.document_id for i in items}
    filenames: dict[int, str] = {}
    if doc_ids:
        for doc_id in doc_ids:
            doc = await doc_svc.get_document(db, doc_id, _effective_org_id(user))
            if doc:
                filenames[doc_id] = doc.filename

    return ApInvoiceListOut(items=[await _to_out(i, filenames.get(i.document_id)) for i in items], total=total, page=page, page_size=page_size)


@router.get("/by-document/{document_id}", response_model=ApInvoiceDetailOut)
async def get_invoice_by_document(document_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    """Convenience lookup for the upload flow — the frontend has a
    document_id right after upload but not yet an invoice_id (the
    ApInvoice row only exists once background processing has actually
    materialized it); poll this until it 200s."""
    invoice = await svc.get_invoice_by_document(db, document_id, _effective_org_id(user))
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No invoice has been materialized for this document yet.")
    return await _to_detail_out(db, invoice)


# GET /zip-batches (no id) is a single-segment path exactly like
# GET /{invoice_id} below — declared here, before that catch-all, same
# route-ordering reason as api/documents.py's own ZB10 list route (a real
# 422 found live: Starlette matched "zip-batches" against {invoice_id}
# and tried to parse it as an int). Every other ZB10 route has 2+
# segments after the prefix so none of those can collide regardless of
# order — only this one list route needed to move.
@router.get("/zip-batches", response_model=DocumentBatchListOut)
async def list_zip_batches(
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    items, total = await batch_svc.list_batches(
        db, _effective_org_id(user), DocumentBatchPipelineType.INVOICE_INTELLIGENCE, status_filter=status_filter, page=page, page_size=page_size,
    )
    return DocumentBatchListOut(items=[DocumentBatchOut.model_validate(b) for b in items], total=total, page=page, page_size=page_size)


@router.get("/{invoice_id}", response_model=ApInvoiceDetailOut)
async def get_invoice(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    return await _to_detail_out(db, invoice)


# ── Upload / processing ───────────────────────────────────────────

@router.post("/upload", response_model=ApInvoiceUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_invoice(
    file: UploadFile = File(...),
    language_mode: str | None = Form(None),
    selected_languages: str | None = Form(None),
    primary_language: str | None = Form(None),
    user: User = Depends(require_action("CREATE", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Saves the file as a real Document (identical to /api/documents/upload
    — no shortcuts) and starts the real background OCR + AI Extraction
    pipeline pinned to the INVOICE taxonomy from the first run (see
    document_service.start_processing()'s document_type param, INV8).
    Materialization into an ApInvoice happens at the end of that pipeline
    (INV5/INV7 wiring) — poll GET /by-document/{document_id} until it 200s.

    save_upload()'s own db.commit() can genuinely collide with another
    document's background pipeline holding SQLite's single-writer lock
    (found live — see document_service.friendly_db_error's docstring);
    left uncaught this surfaces as a bare 500 with no `detail`, which the
    frontend can only render as "An unexpected error occurred." Catch it
    here and give the same honest, retryable message process_document()
    itself already gives when the identical collision hits ITS commit."""
    language_config = None
    if language_mode or selected_languages or primary_language:
        language_config = {
            "mode": language_mode or "auto",
            "selected_languages": [c.strip() for c in (selected_languages or "").split(",") if c.strip()],
            "primary_language": primary_language or "auto",
        }
    try:
        doc, duplicate_of_id = await doc_svc.save_upload(
            db, file=file, organization_id=user.organization_id, uploaded_by_id=user.id,
            language_config=language_config,
        )
    except DocumentLanguageConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except DocumentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except OperationalError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, doc_svc.friendly_db_error(exc))
    doc = await doc_svc.start_processing(db, doc, reprocess=False, document_type="INVOICE", language_config=language_config)
    return ApInvoiceUploadOut(document_id=doc.id, filename=doc.filename, status=doc.status.value, duplicate_of_id=duplicate_of_id)


@router.post("/{invoice_id}/reprocess", response_model=ApInvoiceUploadOut)
async def reprocess_invoice(invoice_id: int, user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    """Full re-run: fresh OCR + AI Extraction (still pinned to INVOICE),
    then the same materialize/validate/decide chain — same non-blocking
    background start as /api/documents/{id}/reprocess; poll GET
    /{invoice_id} for the updated result."""
    invoice = await _get_or_404(db, invoice_id, user)
    doc = await doc_svc.get_document(db, invoice.document_id, _effective_org_id(user))
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invoice's source document no longer exists.")
    doc = await doc_svc.start_processing(db, doc, reprocess=True, document_type="INVOICE")
    return ApInvoiceUploadOut(document_id=doc.id, filename=doc.filename, status=doc.status.value)


@router.post("/{invoice_id}/revalidate", response_model=ApInvoiceDetailOut)
async def revalidate_invoice(invoice_id: int, user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    """Cheap, synchronous re-run of materialize -> validate -> decide from
    the document's CURRENT (already-extracted) data — no fresh OCR/AI
    call. Useful after a rule change, or after fixing data some other way
    than the /corrections route below."""
    invoice = await _get_or_404(db, invoice_id, user)
    try:
        invoice = await svc.revalidate(db, invoice)
    except svc.InvoiceInboxError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_detail_out(db, invoice)


# ── Detail sub-resources ───────────────────────────────────────────

@router.get("/{invoice_id}/extraction")
async def get_extraction(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    doc = await doc_svc.get_document(db, invoice.document_id, _effective_org_id(user))
    if doc is None or not doc.extraction_result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invoice's document has no extraction data yet.")
    return {"document_id": doc.id, "fields": doc.extraction_result.get("fields", []), "overall_confidence": doc.extraction_percent}


@router.get("/{invoice_id}/validation", response_model=list[ApInvoiceValidationResultOut])
async def get_validation(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    results = await svc.get_validation_results(db, invoice.id)
    return [ApInvoiceValidationResultOut.model_validate(r) for r in results]


@router.get("/{invoice_id}/evidence", response_model=ApInvoiceEvidenceOut)
async def get_evidence(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    return await svc.get_evidence(db, invoice)


@router.get("/{invoice_id}/lifecycle-events", response_model=list[ApInvoiceAuditEventOut])
async def get_lifecycle_events(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    """INV13 — the narrow ApInvoiceAuditEvent trail (DECISION_MADE/ERP_*/
    RECONCILIATION_RUN/DUPLICATE_DETECTED), distinct from the broader
    /audit route above (which reads ReviewAuditLog field corrections)."""
    invoice = await _get_or_404(db, invoice_id, user)
    return await invoice_learning_service.list_events(db, invoice.id)


@router.get("/{invoice_id}/audit", response_model=list[ApInvoiceAuditEntryOut])
async def get_audit(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    return await svc.get_audit_trail(db, invoice)


@router.get("/{invoice_id}/history", response_model=ApInvoiceHistoryOut)
async def get_history(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    return await svc.get_history(db, invoice)


# ── Corrections ────────────────────────────────────────────────────

@router.post("/{invoice_id}/corrections", response_model=ApInvoiceDetailOut)
async def apply_corrections(
    invoice_id: int, body: ApInvoiceCorrectionRequest,
    user: User = Depends(require_action("EDIT", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Corrections wrapper that re-validates — routes entirely through
    review_queue_service's existing update_field()/approve() (see
    invoice_inbox_service.apply_corrections' docstring), which is what
    actually re-triggers materialize/validate/decide. Not a second,
    invoice-only correction-storage mechanism."""
    invoice = await _get_or_404(db, invoice_id, user)
    try:
        invoice = await svc.apply_corrections(db, invoice, [c.model_dump() for c in body.corrections], user)
    except svc.InvoiceInboxError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_detail_out(db, invoice)


# ── ERP posting (INV9) ───────────────────────────────────────────

@router.get("/{invoice_id}/erp/preview", response_model=ApInvoiceErpPreviewOut)
async def preview_erp_post(
    invoice_id: int, erp_connection_id: int = Query(...),
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """INV18 — real dry-run/simulation mode: builds the exact payload the
    CURRENT ERP mapping would produce for this invoice (same
    build_erp_payload() call the real background posting loop uses), but
    never opens a network connection and never writes a transaction row.
    See invoice_erp_service.preview_erp_payload's own docstring."""
    invoice = await _get_or_404(db, invoice_id, user)
    try:
        return await invoice_erp_service.preview_erp_payload(db, invoice, erp_connection_id=erp_connection_id)
    except invoice_erp_service.InvoiceErpError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/{invoice_id}/erp/post", response_model=ApInvoiceErpTransactionOut, status_code=status.HTTP_201_CREATED)
async def post_to_erp(
    invoice_id: int, body: ApInvoicePostRequest,
    user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Starts a real, idempotent, retrying ERP post as a background task —
    same non-blocking convention as document processing (a real ERP round
    trip plus exponential backoff can genuinely take well over a request
    timeout). Returns the first (PENDING) transaction row immediately;
    poll GET .../erp/transactions or GET /{invoice_id} (erp_status) for
    the outcome."""
    invoice = await _get_or_404(db, invoice_id, user)
    try:
        txn = await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=body.erp_connection_id)
    except invoice_erp_service.InvoiceErpError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ApInvoiceErpTransactionOut.model_validate(txn)


@router.get("/{invoice_id}/erp/transactions", response_model=list[ApInvoiceErpTransactionOut])
async def list_erp_transactions(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    txns = await invoice_erp_service.list_transactions(db, invoice.id)
    return [ApInvoiceErpTransactionOut.model_validate(t) for t in txns]


@router.get("/{invoice_id}/erp/status")
async def get_erp_status(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    """Real, live GET against the ERP's own status fields — not a cached
    value. Used ahead of reconciliation (INV10)."""
    invoice = await _get_or_404(db, invoice_id, user)
    try:
        return await invoice_erp_service.get_live_status(db, invoice)
    except invoice_erp_service.InvoiceErpError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/{invoice_id}/erp/cancel")
async def cancel_erp_posting(invoice_id: int, user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    try:
        return await invoice_erp_service.cancel_posted_invoice(db, invoice)
    except invoice_erp_service.InvoiceErpError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# ── Reconciliation (INV10) ─────────────────────────────────────────

@router.get("/{invoice_id}/reconciliation", response_model=list[ApInvoiceReconciliationOut])
async def list_reconciliation(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    rows = await invoice_reconciliation_service.list_reconciliations(db, invoice.id)
    return [ApInvoiceReconciliationOut.model_validate(r) for r in rows]


@router.post("/{invoice_id}/reconciliation", response_model=ApInvoiceReconciliationOut, status_code=status.HTTP_201_CREATED)
async def run_reconciliation(invoice_id: int, user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    """Manual on-demand re-check — the same real GET-and-compare that
    already runs automatically right after a successful ERP post (see
    invoice_erp_service.py). Synchronous: a single GET call is fast
    enough not to need the background-task treatment posting itself gets."""
    invoice = await _get_or_404(db, invoice_id, user)
    try:
        row = await invoice_reconciliation_service.reconcile_invoice(db, invoice)
    except invoice_reconciliation_service.InvoiceReconciliationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ApInvoiceReconciliationOut.model_validate(row)


# ── ZIP Batch Upload (ZB10) ──────────────────────────────────────────────
# Identical route set to api/documents.py's own ZB10 section, fixed to
# INVOICE_INTELLIGENCE instead — see that file's section docstring for why
# this is a second, separately-declared router rather than one shared
# parameterized one (require_action's module is baked in at decoration
# time). Both delegate into the exact same batch_upload_service.py/
# document_batch_service.py functions — no business logic duplicated here.

_ZIP_PIPELINE_TYPE = DocumentBatchPipelineType.INVOICE_INTELLIGENCE


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
    user: User = Depends(require_action("CREATE", module=INVOICE_MODULE)),
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
    user: User = Depends(require_action("CREATE", module=INVOICE_MODULE)),
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
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    upload = await _get_zip_upload_or_404(db, upload_id, user)
    return await upload_svc.get_status(db, upload)


@router.post("/zip-upload/{upload_id}/complete", response_model=CompleteZipUploadOut)
async def complete_zip_upload(
    upload_id: int,
    body: CompleteZipUploadRequest = Body(default_factory=CompleteZipUploadRequest),
    user: User = Depends(require_action("CREATE", module=INVOICE_MODULE)),
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
    user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    upload = await _get_zip_upload_or_404(db, upload_id, user)
    upload = await upload_svc.cancel_upload(db, upload)
    return BatchUploadOut.model_validate(upload)


@router.get("/zip-batches/{batch_id}", response_model=DocumentBatchOut)
async def get_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    return DocumentBatchOut.model_validate(batch)


@router.post("/zip-batches/{batch_id}/start", response_model=DocumentBatchOut)
async def start_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    await batch_svc.start_processing_for_batch(db, batch)
    return DocumentBatchOut.model_validate(batch)


@router.post("/zip-batches/{batch_id}/pause", response_model=DocumentBatchOut)
async def pause_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)),
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
    user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)),
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
    user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    batch = await batch_svc.request_cancel(db, batch)
    return DocumentBatchOut.model_validate(batch)


@router.post("/zip-batches/{batch_id}/retry-failed", response_model=DocumentBatchOut)
async def retry_failed_zip_batch(
    batch_id: int,
    user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)),
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
    user: User = Depends(require_action("EXECUTE", module=INVOICE_MODULE)),
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
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
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
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_zip_batch_or_404(db, batch_id, user)
    events, new_since_id = await batch_svc.list_batch_events(db, batch_id, since_id=since_id, limit=limit)
    return DocumentBatchEventListOut(items=[DocumentBatchEventOut.model_validate(e) for e in events], since_id=new_since_id)


@router.get("/zip-batches/{batch_id}/metrics", response_model=DocumentBatchMetricsOut)
async def get_zip_batch_metrics(
    batch_id: int,
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    batch = await _get_zip_batch_or_404(db, batch_id, user)
    return await batch_svc.get_batch_metrics(db, batch)


@router.get("/zip-batches/{batch_id}/errors", response_model=DocumentBatchFileListOut)
async def list_zip_batch_errors(
    batch_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_zip_batch_or_404(db, batch_id, user)
    items, total = await batch_svc.list_batch_errors_paginated(db, batch_id, page=page, page_size=page_size)
    return DocumentBatchFileListOut(items=[DocumentBatchFileOut.model_validate(f) for f in items], total=total, page=page, page_size=page_size)


@router.get("/zip-batches/{batch_id}/errors/download")
async def download_zip_batch_errors(
    batch_id: int,
    user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_zip_batch_or_404(db, batch_id, user)
    content, media_type, filename = await batch_svc.export_batch_errors_csv(db, batch_id)
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
