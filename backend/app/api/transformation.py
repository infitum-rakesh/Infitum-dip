"""Data Format Transformation Engine router — /api/transformation

Foundational, always-available utility (module=None — same convention as
Connections/Settings) rather than a licensed module: converts data
between JSON/TABLE/CSV/Excel/XML/Parquet in any direction. See models/
transformation_job.py's module docstring for the overall design.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.execution import transformation_executor
from app.models.user import User, UserRole
from app.schemas.transformation import (
    TransformationCancelOut, TransformationJobCreateRequest, TransformationJobListOut, TransformationJobOut,
)
from app.services import transformation_service as svc

router = APIRouter(prefix="/transformation", tags=["transformation"])

_view = require_action("VIEW", module=None)
_create = require_action("CREATE", module=None)
_execute = require_action("EXECUTE", module=None)
_delete = require_action("DELETE", module=None)
_ELEVATED_ROLES = (UserRole.SUPER_ADMIN, UserRole.ORG_ADMIN)


async def _to_out(db: AsyncSession, job) -> TransformationJobOut:
    name_map = await svc.user_name_map(db, {job.created_by} if job.created_by else set())
    out = TransformationJobOut.model_validate(job)
    out.created_by_name = name_map.get(job.created_by) if job.created_by else None
    return out


async def _get_job_or_404(db: AsyncSession, job_id: int, user: User):
    job = await svc.get_job(db, job_id, user.organization_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversion job not found")
    return job


@router.get("/jobs", response_model=TransformationJobListOut)
async def list_jobs(
    page: int = 1, page_size: int = 25, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    rows, total = await svc.list_jobs(db, user.organization_id, page=page, page_size=page_size)
    items = [await _to_out(db, r) for r in rows]
    return TransformationJobListOut(items=items, total=total)


@router.post("/jobs", response_model=TransformationJobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: TransformationJobCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db),
):
    try:
        job = await svc.create_job(
            db, organization_id=user.organization_id, name=body.name, user_id=user.id,
            source_kind=body.source_kind, source_format=body.source_format,
            source_uploaded_file_id=body.source_uploaded_file_id,
            source_connection_id=body.source_connection_id, source_schema=body.source_schema, source_object=body.source_object,
            target_kind=body.target_kind, target_format=body.target_format,
            target_connection_id=body.target_connection_id, target_schema=body.target_schema, target_object=body.target_object,
            format_options=body.format_options,
        )
    except svc.TransformationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    transformation_executor.start_job(job.id, job.organization_id)
    return await _to_out(db, job)


@router.get("/jobs/{job_id}", response_model=TransformationJobOut)
async def get_job(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    job = await _get_job_or_404(db, job_id, user)
    return await _to_out(db, job)


@router.post("/jobs/{job_id}/cancel", response_model=TransformationCancelOut)
async def cancel_job(job_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    job = await _get_job_or_404(db, job_id, user)
    try:
        svc.ensure_owner_or_elevated(job, user.id, user.role in _ELEVATED_ROLES)
    except svc.TransformationServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    cancelled = transformation_executor.request_cancel(job_id) if job.status.value in ("PENDING", "RUNNING") else False
    return TransformationCancelOut(
        cancelled=cancelled, message="Cancellation requested." if cancelled else "This conversion already finished — nothing to cancel.",
    )


@router.get("/jobs/{job_id}/download")
async def download_job(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    job = await _get_job_or_404(db, job_id, user)
    try:
        svc.ensure_owner_or_elevated(job, user.id, user.role in _ELEVATED_ROLES)
    except svc.TransformationServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    resolved = svc.resolve_download_path(job)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This conversion isn't downloadable — it hasn't succeeded, targets a database, or the file is no longer on disk.")
    return FileResponse(resolved, filename=resolved.name)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    job = await _get_job_or_404(db, job_id, user)
    try:
        svc.ensure_owner_or_elevated(job, user.id, user.role in _ELEVATED_ROLES)
    except svc.TransformationServiceError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if job.status.value in ("PENDING", "RUNNING"):
        transformation_executor.request_cancel(job_id)
    resolved = svc.resolve_download_path(job)
    if resolved is not None:
        resolved.unlink(missing_ok=True)
    await db.delete(job)
    await db.commit()
