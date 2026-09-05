"""
Lineage router — /api/lineage/... Every route require_action(...,
module="LINEAGE")'d server-side (see core/deps.py) — the frontend is
never the sole authorization layer. Graph endpoints return one pipeline's
current (or a chosen historical) version at a time, never the whole
org's lineage in one call, per the "never render the entire graph at
once" / large-scale requirement.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.lineage import service as svc
from app.schemas.lineage import (
    ColumnMappingLineageOut,
    ColumnMappingListOut,
    ExecutionLineageOut,
    ImpactOut,
    InvoiceLineageOut,
    LineageOverviewOut,
    PipelineLineageOut,
    PipelineVersionListOut,
    SearchResultsOut,
)

router = APIRouter(prefix="/lineage", tags=["lineage"])

_view = require_action("VIEW", module="LINEAGE")


def _org_id(user: User) -> int | None:
    return None if user.role == UserRole.SUPER_ADMIN else user.organization_id


@router.get("/overview", response_model=LineageOverviewOut)
async def overview(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    return await svc.get_overview(db, _org_id(user))


@router.get("/pipeline/{pipeline_id}", response_model=PipelineLineageOut)
async def pipeline_lineage(pipeline_id: int, version: int | None = Query(None), user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    result = await svc.get_pipeline_lineage(db, pipeline_id, _org_id(user), version)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline (or that version) not found")
    return result


@router.get("/pipeline/{pipeline_id}/versions", response_model=PipelineVersionListOut)
async def pipeline_versions(pipeline_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    items = await svc.list_pipeline_versions(db, pipeline_id, _org_id(user))
    return PipelineVersionListOut(items=items)


@router.get("/execution/{job_id}", response_model=ExecutionLineageOut)
async def execution_lineage(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    result = await svc.get_execution_lineage(db, job_id, _org_id(user))
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Execution not found")
    return result


@router.get("/invoices/{invoice_id}", response_model=InvoiceLineageOut)
async def invoice_lineage(invoice_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """INV22 — real field-level lineage for one invoice: Document -> OCR ->
    AI/Custom-Engine extraction -> ApInvoice header/lines -> validation ->
    ERP mapping -> ERP posting -> reconciliation, using only stages that
    actually happened for this invoice."""
    result = await svc.get_invoice_lineage(db, invoice_id, _org_id(user))
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return result


@router.get("/column-mappings", response_model=ColumnMappingListOut)
async def column_mappings(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    items = await svc.list_column_mappings(db, _org_id(user))
    return ColumnMappingListOut(items=items)


@router.get("/column-mappings/{mapping_id}", response_model=ColumnMappingLineageOut)
async def column_mapping_lineage(mapping_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    result = await svc.get_column_mapping_lineage(db, mapping_id, _org_id(user))
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Column mapping not found")
    return result


@router.get("/impact", response_model=ImpactOut)
async def impact(
    connection_id: int | None = Query(None), object_name: str | None = Query(None),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    if connection_id is None and not object_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide connection_id and/or object_name.")
    return await svc.get_impact(db, _org_id(user), connection_id, object_name)


@router.get("/search", response_model=SearchResultsOut)
async def search(q: str = Query(..., min_length=1), user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    items = await svc.search(db, _org_id(user), q)
    return SearchResultsOut(items=items)
