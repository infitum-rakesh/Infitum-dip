"""Bulk Object Migration router — /api/bulk-migration/... (BM6).

Thin translation layer only — all real logic lives in
bulk_migration_config_service.py (BM2), bulk_migration_planner_service.py
(BM3), and bulk_migration_service.py (BM4/BM5). Literal-path routes
("/objects/bulk", "/config/import", "/config/export") are registered
before their sibling `/{id}`-shaped routes, matching this codebase's own
documented Starlette-matches-by-position convention (see api/schedules.py).
"""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.bulk_migration_run import BulkMigrationError
from app.models.user import User, UserRole
from app.schemas.bulk_migration import (
    BulkObjectActionRequest, BulkObjectActionResult, DryRunResultOut, ImportResultOut,
    ObjectCreateRequest, ObjectListOut, ObjectOut, ObjectUpdateRequest,
    PipelineCreateRequest, PipelineListOut, PipelineOut, PipelineUpdateRequest,
    RetryFailedRequest, RetryFailedResult, RunListOut, RunOut,
    TaskEventListOut, TaskListOut, ValidationResultOut,
)
from app.services import bulk_migration_config_service as cfg
from app.services import bulk_migration_planner_service as planner
from app.services import bulk_migration_service as svc

router = APIRouter(prefix="/bulk-migration", tags=["bulk-migration"])

_view = require_action("VIEW", module="BULK_MIGRATION")
_create = require_action("CREATE", module="BULK_MIGRATION")
_edit = require_action("EDIT", module="BULK_MIGRATION")
_execute = require_action("EXECUTE", module="BULK_MIGRATION")
_delete = require_action("DELETE", module="BULK_MIGRATION")


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _get_pipeline_or_404(db: AsyncSession, pipeline_id: int, user: User):
    pipeline = await cfg.get_pipeline(db, pipeline_id, _effective_org_id(user))
    if pipeline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline not found")
    return pipeline


async def _get_run_or_404(db: AsyncSession, run_id: int, user: User):
    from app.models.bulk_migration_run import BulkMigrationRun
    stmt_org = _effective_org_id(user)
    run = await db.get(BulkMigrationRun, run_id)
    if run is None or (stmt_org is not None and run.organization_id != stmt_org):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run


# ── Pipelines ────────────────────────────────────────────────────────────

@router.get("/pipelines", response_model=PipelineListOut)
async def list_pipelines(
    q: str = Query("", alias="search"), status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    result = await cfg.list_pipelines(db, _effective_org_id(user), search=q, status=status_filter, page=page, page_size=page_size)
    items = [await cfg.to_pipeline_out(db, p) for p in result["items"]]
    return {**result, "items": items}


@router.post("/pipelines", response_model=PipelineOut, status_code=status.HTTP_201_CREATED)
async def create_pipeline(body: PipelineCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    try:
        pipeline = await cfg.create_pipeline(db, organization_id=_effective_org_id(user), created_by=user.id, **body.model_dump())
    except cfg.BulkMigrationConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await cfg.to_pipeline_out(db, pipeline)


@router.get("/pipelines/{pipeline_id}", response_model=PipelineOut)
async def get_pipeline(pipeline_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    return await cfg.to_pipeline_out(db, pipeline)


@router.patch("/pipelines/{pipeline_id}", response_model=PipelineOut)
async def update_pipeline(pipeline_id: int, body: PipelineUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    pipeline = await cfg.update_pipeline(db, pipeline, updated_by=user.id, **body.model_dump(exclude_unset=True))
    return await cfg.to_pipeline_out(db, pipeline)


@router.delete("/pipelines/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline(pipeline_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    await cfg.delete_pipeline(db, pipeline, actor_id=user.id)


# ── Objects ──────────────────────────────────────────────────────────────

@router.get("/pipelines/{pipeline_id}/objects", response_model=ObjectListOut)
async def list_objects(
    pipeline_id: int, q: str = Query("", alias="search"), enabled: bool | None = Query(None),
    object_type: str | None = Query(None), load_type: str | None = Query(None),
    dependency_group: str | None = Query(None), page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    await _get_pipeline_or_404(db, pipeline_id, user)
    result = await cfg.list_objects(
        db, pipeline_id, search=q, enabled=enabled, object_type=object_type, load_type=load_type,
        dependency_group=dependency_group, page=page, page_size=page_size,
    )
    return {**result, "items": [cfg.to_object_out(o) for o in result["items"]]}


@router.post("/pipelines/{pipeline_id}/objects", response_model=ObjectOut, status_code=status.HTTP_201_CREATED)
async def create_object(pipeline_id: int, body: ObjectCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    try:
        obj = await cfg.create_object(db, pipeline, actor_id=user.id, **body.model_dump())
    except cfg.BulkMigrationConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return cfg.to_object_out(obj)


@router.post("/pipelines/{pipeline_id}/objects/bulk", response_model=BulkObjectActionResult)
async def bulk_update_objects(pipeline_id: int, body: BulkObjectActionRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    affected = await cfg.bulk_update_objects(db, pipeline, body.object_ids, body.patch, actor_id=user.id)
    return {"affected": affected}


@router.delete("/pipelines/{pipeline_id}/objects/bulk", response_model=BulkObjectActionResult)
async def bulk_delete_objects(pipeline_id: int, body: BulkObjectActionRequest, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    affected = await cfg.bulk_delete_objects(db, pipeline, body.object_ids, actor_id=user.id)
    return {"affected": affected}


@router.patch("/pipelines/{pipeline_id}/objects/{object_id}", response_model=ObjectOut)
async def update_object(pipeline_id: int, object_id: int, body: ObjectUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    obj = await cfg.get_object(db, object_id, pipeline_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Object not found")
    try:
        obj = await cfg.update_object(db, pipeline, obj, actor_id=user.id, **body.model_dump(exclude_unset=True))
    except cfg.BulkMigrationConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return cfg.to_object_out(obj)


@router.delete("/pipelines/{pipeline_id}/objects/{object_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object(pipeline_id: int, object_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    obj = await cfg.get_object(db, object_id, pipeline_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Object not found")
    await cfg.delete_object(db, pipeline, obj, actor_id=user.id)


# ── Config import / export ───────────────────────────────────────────────

@router.post("/pipelines/{pipeline_id}/config/import", response_model=ImportResultOut)
async def import_config(
    pipeline_id: int, file: UploadFile = File(...), dry_run: bool = Query(False),
    user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db),
):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    content = await file.read()
    try:
        result = await cfg.import_objects(db, pipeline, content, file.filename or "objects.csv", dry_run=dry_run, actor_id=user.id)
    except cfg.BulkMigrationConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return result


@router.get("/pipelines/{pipeline_id}/config/export")
async def export_config(pipeline_id: int, format: str = Query("csv"), user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    await _get_pipeline_or_404(db, pipeline_id, user)
    content, content_type, filename = await cfg.export_objects(db, pipeline_id, format)
    return Response(content=content, media_type=content_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── Validate / dry-run / run ─────────────────────────────────────────────

@router.post("/pipelines/{pipeline_id}/validate", response_model=ValidationResultOut)
async def validate_pipeline(pipeline_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    return await planner.validate_pipeline(db, pipeline)


@router.post("/pipelines/{pipeline_id}/dry-run", response_model=DryRunResultOut)
async def dry_run_pipeline(pipeline_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    return await planner.dry_run(db, pipeline)


@router.post("/pipelines/{pipeline_id}/run", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def run_pipeline(pipeline_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    try:
        run = await svc.start_run(db, pipeline, trigger_type="MANUAL", created_by=user.id)
    except (svc.BulkMigrationServiceError, BulkMigrationError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return svc.to_run_out(run)


# ── Runs ─────────────────────────────────────────────────────────────────

@router.get("/runs", response_model=RunListOut)
async def list_runs(
    pipeline_id: int | None = Query(None), status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    from sqlalchemy import func, select
    from app.models.bulk_migration_run import BulkMigrationRun

    stmt = select(BulkMigrationRun)
    org_id = _effective_org_id(user)
    if org_id is not None:
        stmt = stmt.where(BulkMigrationRun.organization_id == org_id)
    if pipeline_id is not None:
        stmt = stmt.where(BulkMigrationRun.pipeline_id == pipeline_id)
    if status_filter:
        stmt = stmt.where(BulkMigrationRun.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(BulkMigrationRun.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    runs = (await db.execute(stmt)).scalars().all()
    return {
        "items": [svc.to_run_out(r) for r in runs], "total": total, "page": page, "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    return svc.to_run_out(run)


@router.get("/runs/{run_id}/tasks", response_model=TaskListOut)
async def list_tasks(
    run_id: int, status_filter: str | None = Query(None, alias="status"), dependency_level: int | None = Query(None),
    page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    from sqlalchemy import func, select
    from app.models.bulk_migration_run import BulkMigrationTask

    await _get_run_or_404(db, run_id, user)
    stmt = select(BulkMigrationTask).where(BulkMigrationTask.run_id == run_id)
    if status_filter:
        stmt = stmt.where(BulkMigrationTask.status == status_filter)
    if dependency_level is not None:
        stmt = stmt.where(BulkMigrationTask.dependency_level == dependency_level)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    stmt = stmt.order_by(BulkMigrationTask.dependency_level.asc(), BulkMigrationTask.id.asc()).offset((page - 1) * page_size).limit(page_size)
    tasks = (await db.execute(stmt)).scalars().all()
    return {
        "items": [svc.to_task_out(t) for t in tasks], "total": total, "page": page, "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/runs/{run_id}/events", response_model=TaskEventListOut)
async def list_events(run_id: int, since_id: int = Query(0), limit: int = Query(200, ge=1, le=1000), user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    from sqlalchemy import select
    from app.models.bulk_migration_run import BulkMigrationTaskEvent

    await _get_run_or_404(db, run_id, user)
    stmt = (
        select(BulkMigrationTaskEvent)
        .where(BulkMigrationTaskEvent.run_id == run_id, BulkMigrationTaskEvent.id > since_id)
        .order_by(BulkMigrationTaskEvent.id.asc()).limit(limit)
    )
    events = (await db.execute(stmt)).scalars().all()
    new_since_id = events[-1].id if events else since_id
    return {"items": [svc.to_event_out(e) for e in events], "since_id": new_since_id}


@router.post("/runs/{run_id}/pause", response_model=RunOut)
async def pause_run(run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    try:
        run = await svc.request_pause(db, run)
    except svc.BulkMigrationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return svc.to_run_out(run)


@router.post("/runs/{run_id}/resume", response_model=RunOut)
async def resume_run(run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    try:
        run = await svc.resume_run(db, run)
    except svc.BulkMigrationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return svc.to_run_out(run)


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    try:
        run = await svc.request_cancel(db, run)
    except svc.BulkMigrationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return svc.to_run_out(run)


@router.post("/runs/{run_id}/retry-failed", response_model=RetryFailedResult)
async def retry_failed(run_id: int, body: RetryFailedRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    try:
        requeued = await svc.retry_failed(db, run, include_successful=body.include_successful)
    except svc.BulkMigrationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"requeued": requeued}
