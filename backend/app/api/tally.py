"""Tally Integration router — /api/tally/... (Phase A + TALLY-B).

Phase A: company discovery against an already-saved TALLY_PRIME
Connection. Everything else about the connection itself (Test Connection,
create/edit/delete) already works through the existing generic
/api/connections/... routes — nothing Tally-specific is needed there.

TALLY-B: read-only voucher/master/ledger/payment/outstanding extraction.
Runs synchronously within the request (bounded by date-range chunking +
settings.tally_max_concurrent_requests, same "hits a live source, real
timeout safety net" convention as connection_service's schema/object
browsing) rather than as a background job — Phase B's extraction runs are
modest in scope; a fire-and-forget worker-pool version is Phase J's job
(scheduled/batch reconciliation) once real usage justifies it.

TALLY-C: canonical accounting model (TallyCanonicalVoucher/Party/Ledger),
materialized automatically at the end of every extraction run — these
read routes just expose what's already been upserted; POST .../
canonical/re-materialize is the manual backfill/reprocess escape hatch.

TALLY-D: accounting mapping + voucher preview + pre-write validation.
Every route here only builds/persists a PREVIEW of what an ApInvoice
would become in Tally and validates it deterministically — nothing here
can write to Tally (see services/tally_voucher_builder.py's module
docstring). Gated on the TALLY_INTEGRATION module only, same as the rest
of this router; the invoice lookup itself is still org-scoped (see
tally_voucher_preview_service.build_preview), so no cross-tenant read is
possible even though INVOICE_INTELLIGENCE licensing isn't re-checked here.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, ConnectorUnsupportedError, ConnectorValidationError
from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.connection import Connection, ConnectionType
from app.models.tally_extraction import TallyExtractionMode, TallyExtractionType
from app.models.user import User, UserRole
from app.models.tally_reconciliation import TallyExceptionType
from app.schemas.tally import (
    TallyAccountMappingListOut,
    TallyAccountMappingPublishRequest,
    TallyCanonicalLedgerListOut,
    TallyCanonicalPartyListOut,
    TallyCanonicalVoucherListOut,
    TallyCompanyListOut,
    TallyCompanyOut,
    TallyExtractionRecordListOut,
    TallyExtractionRecordOut,
    TallyExtractionRunListOut,
    TallyExtractionRunOut,
    TallyExtractionStartRequest,
    TallyReconciliationExceptionListOut,
    TallyReconciliationExceptionOut,
    TallyReconciliationRunListOut,
    TallyReconciliationRunOut,
    TallyReconciliationStartRequest,
    TallyRematerializeResultOut,
    TallyVoucherPreviewListOut,
    TallyVoucherPreviewOut,
    TallyVoucherPreviewRequest,
    TallyVoucherSubmitForApprovalOut,
    TallyVoucherValidationResultOut,
    TallyWriteBackStartOut,
    TallyWriteBackTransactionListOut,
    TallyWriteBackTransactionOut,
    TallyWriteBackVerificationListOut,
    TallyWriteBackVerificationOut,
)
from app.services import (
    connection_service,
    tally_approval_service as approval_svc,
    tally_canonical_service as canonical_svc,
    tally_extraction_service as extraction_svc,
    tally_mapping_service,
    tally_reconciliation_service as reconciliation_svc,
    tally_service as svc,
    tally_voucher_preview_service as preview_svc,
    tally_writeback_service as writeback_svc,
)
from app.services.tally_approval_service import TallyApprovalError
from app.services.tally_extraction_service import TallyExtractionValidationError
from app.services.tally_mapping_service import TallyMappingError
from app.services.tally_reconciliation_service import TallyReconciliationError
from app.services.tally_voucher_preview_service import TallyVoucherPreviewError
from app.services.tally_writeback_service import TallyWriteBackError

router = APIRouter(prefix="/tally", tags=["tally"])

_view = require_action("VIEW", module="TALLY_INTEGRATION")
_execute = require_action("EXECUTE", module="TALLY_INTEGRATION")


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _get_connection_or_404(db: AsyncSession, connection_id: int, user: User) -> Connection:
    conn = await connection_service.get_connection(db, connection_id, _effective_org_id(user))
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    if conn.connection_type != ConnectionType.TALLY_PRIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This connection is not a TallyPrime connection.")
    return conn


@router.post("/connections/{connection_id}/discover-companies", response_model=TallyCompanyListOut)
async def discover_companies(
    connection_id: int,
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_tenant_db),
):
    conn = await _get_connection_or_404(db, connection_id, user)
    try:
        companies = await svc.discover_companies(conn)
    except ConnectorUnsupportedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))
    except ConnectorValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except ConnectorError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    return TallyCompanyListOut(items=[TallyCompanyOut(**c) for c in companies])


# ── Extraction (TALLY-B) ────────────────────────────────────────────
# Literal-path routes ("/extractions/{run_id}") are registered separately
# from "/connections/{connection_id}/extractions" — no static-vs-dynamic
# ordering conflict here since the two don't share a path prefix.

@router.post("/connections/{connection_id}/extractions", response_model=TallyExtractionRunOut, status_code=status.HTTP_201_CREATED)
async def start_extraction(
    connection_id: int,
    body: TallyExtractionStartRequest,
    user: User = Depends(_execute),
    db: AsyncSession = Depends(get_tenant_db),
):
    conn = await _get_connection_or_404(db, connection_id, user)
    try:
        extraction_type = TallyExtractionType(body.extraction_type)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown extraction_type '{body.extraction_type}'.")
    try:
        mode = TallyExtractionMode(body.mode)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown mode '{body.mode}'.")

    try:
        run = await extraction_svc.start_extraction(
            db, conn, extraction_type=extraction_type, company=body.company,
            voucher_type=body.voucher_type, party=body.party,
            from_date=body.from_date, to_date=body.to_date, mode=mode,
            actor_user_id=user.id,
        )
    except TallyExtractionValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return run


@router.get("/connections/{connection_id}/extractions", response_model=TallyExtractionRunListOut)
async def list_extractions(
    connection_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_connection_or_404(db, connection_id, user)
    items, total = await extraction_svc.list_runs(db, connection_id, _effective_org_id(user), page=page, page_size=page_size)
    return TallyExtractionRunListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/extractions/{run_id}", response_model=TallyExtractionRunOut)
async def get_extraction(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    run = await extraction_svc.get_run(db, run_id, _effective_org_id(user))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Extraction run not found")
    return run


@router.get("/extractions/{run_id}/records", response_model=TallyExtractionRecordListOut)
async def list_extraction_records(
    run_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    result = await extraction_svc.list_records(db, run_id, _effective_org_id(user), page=page, page_size=page_size)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Extraction run not found")
    items, total = result
    return TallyExtractionRecordListOut(items=items, total=total, page=page, page_size=page_size)


# ── Canonical accounting model (TALLY-C) ────────────────────────────

@router.get("/connections/{connection_id}/canonical/vouchers", response_model=TallyCanonicalVoucherListOut)
async def list_canonical_vouchers(
    connection_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_connection_or_404(db, connection_id, user)
    items, total = await canonical_svc.list_vouchers(db, connection_id, _effective_org_id(user), page=page, page_size=page_size)
    return TallyCanonicalVoucherListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/connections/{connection_id}/canonical/parties", response_model=TallyCanonicalPartyListOut)
async def list_canonical_parties(
    connection_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_connection_or_404(db, connection_id, user)
    items, total = await canonical_svc.list_parties(db, connection_id, _effective_org_id(user), page=page, page_size=page_size)
    return TallyCanonicalPartyListOut(items=items, total=total, page=page, page_size=page_size)


@router.get("/connections/{connection_id}/canonical/ledgers", response_model=TallyCanonicalLedgerListOut)
async def list_canonical_ledgers(
    connection_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_connection_or_404(db, connection_id, user)
    items, total = await canonical_svc.list_ledgers(db, connection_id, _effective_org_id(user), page=page, page_size=page_size)
    return TallyCanonicalLedgerListOut(items=items, total=total, page=page, page_size=page_size)


@router.post("/extractions/{run_id}/canonical/re-materialize", response_model=TallyRematerializeResultOut)
async def re_materialize_extraction(run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    result = await canonical_svc.re_materialize_run(db, run_id, _effective_org_id(user))
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Extraction run not found")
    return TallyRematerializeResultOut(**result)


# ── Accounting mapping + voucher preview (TALLY-D) ──────────────────

@router.get("/connections/{connection_id}/mapping", response_model=TallyAccountMappingListOut)
async def get_mapping(connection_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    conn = await _get_connection_or_404(db, connection_id, user)
    rows = await tally_mapping_service.get_current_mapping(db, conn.organization_id, conn.id)
    if not rows:
        rows = await tally_mapping_service.seed_default_mapping(db, conn.organization_id, conn.id)
    return TallyAccountMappingListOut(items=rows)


@router.put("/connections/{connection_id}/mapping", response_model=TallyAccountMappingListOut)
async def publish_mapping(
    connection_id: int, body: TallyAccountMappingPublishRequest,
    user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db),
):
    conn = await _get_connection_or_404(db, connection_id, user)
    try:
        rows = await tally_mapping_service.publish_mapping(
            db, conn.organization_id, conn.id, [r.model_dump() for r in body.rows], user.id,
        )
    except TallyMappingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return TallyAccountMappingListOut(items=rows)


def _preview_out(preview, results) -> TallyVoucherPreviewOut:
    out = TallyVoucherPreviewOut.model_validate(preview)
    out.validation_results = [TallyVoucherValidationResultOut.model_validate(r) for r in results]
    return out


@router.post("/connections/{connection_id}/vouchers/preview", response_model=TallyVoucherPreviewOut, status_code=status.HTTP_201_CREATED)
async def preview_voucher(
    connection_id: int, body: TallyVoucherPreviewRequest,
    user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db),
):
    conn = await _get_connection_or_404(db, connection_id, user)
    try:
        preview = await preview_svc.build_preview(db, conn, invoice_id=body.invoice_id, company=body.company, actor_user_id=user.id)
    except TallyVoucherPreviewError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    results = await preview_svc.list_validation_results(db, preview.id)
    return _preview_out(preview, results)


@router.get("/connections/{connection_id}/vouchers/previews", response_model=TallyVoucherPreviewListOut)
async def list_voucher_previews(
    connection_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_connection_or_404(db, connection_id, user)
    items, total = await preview_svc.list_previews(db, connection_id, _effective_org_id(user), page=page, page_size=page_size)
    out_items = []
    for p in items:
        results = await preview_svc.list_validation_results(db, p.id)
        out_items.append(_preview_out(p, results))
    return TallyVoucherPreviewListOut(items=out_items, total=total, page=page, page_size=page_size)


@router.get("/vouchers/previews/{preview_id}", response_model=TallyVoucherPreviewOut)
async def get_voucher_preview(preview_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    preview = await preview_svc.get_preview(db, preview_id, _effective_org_id(user))
    if preview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voucher preview not found")
    results = await preview_svc.list_validation_results(db, preview.id)
    return _preview_out(preview, results)


@router.post("/vouchers/previews/{preview_id}/submit-for-approval", response_model=TallyVoucherSubmitForApprovalOut, status_code=status.HTTP_201_CREATED)
async def submit_voucher_for_approval(preview_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    """TALLY-H — the write-back approval gate's only entry point. Gated
    on TALLY_INTEGRATION EXECUTE (not PAYMENT_APPROVAL) — deliberately a
    separate authorization surface from invoice payment approval, per
    the master directive's "logically separate" requirement, even though
    both reuse the same underlying engine."""
    org_id = _effective_org_id(user)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A super-admin without an organization context cannot submit a voucher for approval — this endpoint is org-scoped.")
    try:
        preview, workflow = await approval_svc.submit_for_approval(db, preview_id=preview_id, organization_id=org_id, actor_user_id=user.id)
    except TallyApprovalError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return TallyVoucherSubmitForApprovalOut(workflow_id=workflow.id, workflow_status=workflow.status.value, approval_status=preview.approval_status.value)


# ── Controlled write-back (TALLY-I) ─────────────────────────────────
# The one place in this entire integration that actually sends anything
# to Tally. Gated on TALLY_INTEGRATION EXECUTE plus every precondition
# tally_writeback_service.post_voucher() itself enforces (READY status,
# APPROVED approval, not already written/in-progress) — see that
# module's own docstring for the full idempotency/retry/circuit-breaker
# design.

@router.post("/vouchers/previews/{preview_id}/write-back", response_model=TallyWriteBackStartOut, status_code=status.HTTP_201_CREATED)
async def write_back_voucher(preview_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    preview = await preview_svc.get_preview(db, preview_id, _effective_org_id(user))
    if preview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voucher preview not found")
    try:
        txn = await writeback_svc.post_voucher(db, preview, actor_user_id=user.id)
    except TallyWriteBackError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return TallyWriteBackStartOut(transaction_id=txn.id, write_status=txn.status.value)


@router.get("/vouchers/previews/{preview_id}/write-back/transactions", response_model=TallyWriteBackTransactionListOut)
async def list_write_back_transactions(preview_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    preview = await preview_svc.get_preview(db, preview_id, _effective_org_id(user))
    if preview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voucher preview not found")
    items = await writeback_svc.list_transactions(db, preview.id)
    return TallyWriteBackTransactionListOut(items=[TallyWriteBackTransactionOut.model_validate(t) for t in items])


@router.get("/vouchers/previews/{preview_id}/write-back/verifications", response_model=TallyWriteBackVerificationListOut)
async def list_write_back_verifications(preview_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    preview = await preview_svc.get_preview(db, preview_id, _effective_org_id(user))
    if preview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voucher preview not found")
    items = await writeback_svc.list_verifications(db, preview.id)
    return TallyWriteBackVerificationListOut(items=[TallyWriteBackVerificationOut.model_validate(v) for v in items])


@router.post("/vouchers/previews/{preview_id}/write-back/verify", response_model=TallyWriteBackVerificationOut, status_code=status.HTTP_201_CREATED)
async def reverify_write_back(preview_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    """Manual re-check escape hatch — same convention as invoice
    reconciliation's own on-demand POST route."""
    preview = await preview_svc.get_preview(db, preview_id, _effective_org_id(user))
    if preview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voucher preview not found")
    try:
        verification = await writeback_svc.verify_write(db, preview)
    except TallyWriteBackError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return TallyWriteBackVerificationOut.model_validate(verification)


# ── Reconciliation (TALLY-E) ────────────────────────────────────────
# Compares real ApInvoice rows against real TallyCanonicalVoucher/Party
# rows — see services/tally_reconciliation_service.py's module docstring
# for why this reuses app/reconciliation/engine.py rather than building a
# second comparator. Read-only: nothing here writes to Tally.

@router.post("/connections/{connection_id}/reconciliation/runs", response_model=TallyReconciliationRunOut, status_code=status.HTTP_201_CREATED)
async def start_reconciliation(
    connection_id: int, body: TallyReconciliationStartRequest,
    user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db),
):
    conn = await _get_connection_or_404(db, connection_id, user)
    try:
        run = await reconciliation_svc.run_reconciliation(
            db, conn, company=body.company,
            tolerance=body.tolerance if body.tolerance is not None else reconciliation_svc.DEFAULT_TOLERANCE,
            created_by=user.id,
        )
    except TallyReconciliationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return TallyReconciliationRunOut.model_validate(run)


@router.get("/connections/{connection_id}/reconciliation/runs", response_model=TallyReconciliationRunListOut)
async def list_reconciliation_runs(
    connection_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_connection_or_404(db, connection_id, user)
    items, total = await reconciliation_svc.list_runs(db, _effective_org_id(user), connection_id, page=page, page_size=page_size)
    return TallyReconciliationRunListOut(items=[TallyReconciliationRunOut.model_validate(r) for r in items], total=total)


@router.get("/reconciliation/runs/{run_id}", response_model=TallyReconciliationRunOut)
async def get_reconciliation_run(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    run = await reconciliation_svc.get_run(db, run_id, _effective_org_id(user))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reconciliation run not found")
    return TallyReconciliationRunOut.model_validate(run)


@router.get("/reconciliation/runs/{run_id}/exceptions", response_model=TallyReconciliationExceptionListOut)
async def list_reconciliation_exceptions(
    run_id: int,
    exception_type: TallyExceptionType | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    user: User = Depends(_view),
    db: AsyncSession = Depends(get_tenant_db),
):
    run = await reconciliation_svc.get_run(db, run_id, _effective_org_id(user))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reconciliation run not found")
    items, total = await reconciliation_svc.list_exceptions(
        db, run_id, _effective_org_id(user), exception_type=exception_type, page=page, page_size=page_size,
    )
    return TallyReconciliationExceptionListOut(items=[TallyReconciliationExceptionOut.model_validate(e) for e in items], total=total)
