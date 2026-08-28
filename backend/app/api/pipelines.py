"""Pipelines router — /api/pipelines/...

Backs the Transform Workspace canvas: create/save-as-new-version, version
history, compare, restore. Pipeline EXECUTION stays out of scope here (the
canvas' own client-side run-simulation is unaffected); this router only
owns persistence + versioning of the {nodes, edges, settings} definition.

Foundational module (module=None) — available to any authenticated user
whose role tier permits the action, same as Connections/Schedules.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.schemas.pipelines import (
    PipelineCompareOut,
    PipelineCreateRequest,
    PipelineListOut,
    PipelineOut,
    PipelineSaveRequest,
    PipelineVersionDetailOut,
    PipelineVersionListOut,
    PipelineVersionSummaryOut,
    PipelineWithDefinitionOut,
)
from app.services import pipeline_service as svc

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

_view   = require_action("VIEW", module=None)
_create = require_action("CREATE", module=None)
_edit   = require_action("EDIT", module=None)
_delete = require_action("DELETE", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline not found")


async def _get_or_404(db: AsyncSession, pipeline_id: int, user: User):
    pipeline = await svc.get_pipeline(db, pipeline_id, _effective_org_id(user))
    if pipeline is None:
        raise _not_found()
    return pipeline


# ── List / get ───────────────────────────────────────────────────

@router.get("", response_model=PipelineListOut)
async def list_pipelines(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    items, total = await svc.list_pipelines(db, _effective_org_id(user))
    name_map = await svc.user_name_map(db, {p.created_by for p in items} | {p.updated_by for p in items})
    return PipelineListOut(items=[svc.pipeline_to_out(p, name_map) for p in items], total=total)


@router.get("/{pipeline_id}", response_model=PipelineWithDefinitionOut)
async def get_pipeline(pipeline_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_or_404(db, pipeline_id, user)
    current = await svc.get_current_version(db, pipeline)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline has no versions")
    name_map = await svc.user_name_map(db, {pipeline.created_by, pipeline.updated_by})
    defn = current.pipeline_definition or {}
    return PipelineWithDefinitionOut(
        **svc.pipeline_to_out(pipeline, name_map),
        nodes=defn.get("nodes", []), edges=defn.get("edges", []), settings=defn.get("settings", {}),
    )


# ── Create / Save (save always creates a new version) ─────────────

@router.post("", response_model=PipelineWithDefinitionOut, status_code=status.HTTP_201_CREATED)
async def create_pipeline(body: PipelineCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await svc.create_pipeline(
        db, organization_id=user.organization_id, user_id=user.id,
        name=body.name, nodes=body.nodes, edges=body.edges, settings=body.settings,
    )
    name_map = await svc.user_name_map(db, {user.id})
    return PipelineWithDefinitionOut(
        **svc.pipeline_to_out(pipeline, name_map),
        nodes=body.nodes, edges=body.edges, settings=body.settings,
    )


@router.put("/{pipeline_id}", response_model=PipelineWithDefinitionOut)
async def save_pipeline(pipeline_id: int, body: PipelineSaveRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_or_404(db, pipeline_id, user)
    pipeline, version = await svc.save_version(
        db, pipeline, user_id=user.id, name=body.name,
        nodes=body.nodes, edges=body.edges, settings=body.settings, change_summary=body.change_summary,
    )
    name_map = await svc.user_name_map(db, {pipeline.created_by, pipeline.updated_by})
    defn = version.pipeline_definition or {}
    return PipelineWithDefinitionOut(
        **svc.pipeline_to_out(pipeline, name_map),
        nodes=defn.get("nodes", []), edges=defn.get("edges", []), settings=defn.get("settings", {}),
    )


@router.delete("/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pipeline(pipeline_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_or_404(db, pipeline_id, user)
    await svc.delete_pipeline(db, pipeline)


# ── Version history ──────────────────────────────────────────────

@router.get("/{pipeline_id}/versions", response_model=PipelineVersionListOut)
async def list_versions(pipeline_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_or_404(db, pipeline_id, user)
    versions = await svc.list_versions(db, pipeline_id)
    name_map = await svc.user_name_map(db, {v.created_by for v in versions})
    items = [svc.version_to_summary(v, v.version_number == pipeline.current_version, name_map) for v in versions]
    return PipelineVersionListOut(
        pipeline_id=pipeline.id, pipeline_name=pipeline.name, current_version=pipeline.current_version,
        items=[PipelineVersionSummaryOut(**i) for i in items],
    )


@router.get("/{pipeline_id}/versions/{version_number}", response_model=PipelineVersionDetailOut)
async def get_version(pipeline_id: int, version_number: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_or_404(db, pipeline_id, user)
    version = await svc.get_version(db, pipeline_id, version_number)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found (it may have been pruned — only the last 5 versions are kept)")
    name_map = await svc.user_name_map(db, {version.created_by})
    return PipelineVersionDetailOut(**svc.version_to_detail(version, version_number == pipeline.current_version, name_map))


@router.get("/{pipeline_id}/compare", response_model=PipelineCompareOut)
async def compare_versions(
    pipeline_id: int, version_a: int, version_b: int,
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    await _get_or_404(db, pipeline_id, user)
    va = await svc.get_version(db, pipeline_id, version_a)
    vb = await svc.get_version(db, pipeline_id, version_b)
    if va is None or vb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "One or both versions not found (only the last 5 are kept)")
    diff = svc.diff_definitions(va.pipeline_definition or {}, vb.pipeline_definition or {})
    return PipelineCompareOut(pipeline_id=pipeline_id, version_a=version_a, version_b=version_b, diff=diff)


@router.post("/{pipeline_id}/versions/{version_number}/restore", response_model=PipelineWithDefinitionOut, status_code=status.HTTP_201_CREATED)
async def restore_version(pipeline_id: int, version_number: int, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_or_404(db, pipeline_id, user)
    try:
        pipeline, version = await svc.restore_version(db, pipeline, version_number, user_id=user.id)
    except svc.PipelineVersionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    name_map = await svc.user_name_map(db, {pipeline.created_by, pipeline.updated_by})
    defn = version.pipeline_definition or {}
    return PipelineWithDefinitionOut(
        **svc.pipeline_to_out(pipeline, name_map),
        nodes=defn.get("nodes", []), edges=defn.get("edges", []), settings=defn.get("settings", {}),
    )
