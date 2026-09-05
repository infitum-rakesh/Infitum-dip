"""Invoice Intelligence router — /api/invoices/... (INV8).

Invoice Inbox + the core invoice-centric routes. Upload/OCR/AI-extraction/
reprocess all flow straight through the EXISTING Document Intelligence
pipeline (app.services.document_service) — this router is a thin,
invoice-shaped view over that plus the genuinely new Invoice Intelligence
layer (app.services.invoice.*). Nothing here re-implements OCR,
extraction, or Review Queue.
"""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.invoice_intelligence.core import ApInvoice
from app.models.user import User, UserRole
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
from app.services import document_service as doc_svc
from app.services.document_service import DocumentValidationError
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


@router.get("/{invoice_id}", response_model=ApInvoiceDetailOut)
async def get_invoice(invoice_id: int, user: User = Depends(require_action("VIEW", module=INVOICE_MODULE)), db: AsyncSession = Depends(get_tenant_db)):
    invoice = await _get_or_404(db, invoice_id, user)
    return await _to_detail_out(db, invoice)


# ── Upload / processing ───────────────────────────────────────────

@router.post("/upload", response_model=ApInvoiceUploadOut, status_code=status.HTTP_201_CREATED)
async def upload_invoice(
    file: UploadFile = File(...),
    user: User = Depends(require_action("CREATE", module=INVOICE_MODULE)),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Saves the file as a real Document (identical to /api/documents/upload
    — no shortcuts) and starts the real background OCR + AI Extraction
    pipeline pinned to the INVOICE taxonomy from the first run (see
    document_service.start_processing()'s document_type param, INV8).
    Materialization into an ApInvoice happens at the end of that pipeline
    (INV5/INV7 wiring) — poll GET /by-document/{document_id} until it 200s."""
    try:
        doc, duplicate_of_id = await doc_svc.save_upload(db, file=file, organization_id=user.organization_id, uploaded_by_id=user.id)
    except DocumentValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    doc = await doc_svc.start_processing(db, doc, reprocess=False, document_type="INVOICE")
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
