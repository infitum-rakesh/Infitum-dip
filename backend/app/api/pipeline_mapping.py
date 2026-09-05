"""
Pipeline <-> Column Mapping attach/upgrade router — /api/pipelines/{id}/mapping/...

Deliberately separate from api/mapping.py (own file, own router) per this
build's "don't put everything in one file" instruction, and gated by the
same "TRANSFORMATION" license module every other mapping/pipeline route
already uses (no new license module — see mapping_service.py's module
docstring for why).

Ownership split, worth stating plainly: a pipeline node's config
(`{"mappingId": ..., "mappingVersion": ...}` inside
PipelineVersion.pipeline_definition) is the single source of truth for
what a pipeline CURRENTLY runs — this router never writes that config
directly except in /upgrade (which goes through pipeline_service.save_version(),
the same immutable-versioning path every other pipeline edit uses).
/attach's job is narrower: validate the target version is actually
PUBLISHED, run a best-effort source/target consistency check, and record
a ColumnMappingUsage history row — the frontend (ColumnMappingConfig.jsx)
still writes the node's own config via the normal canvas Save/Apply flow,
exactly like it already does for every other node type's config edits.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.audit_log import AuditStatus, record_event
from app.models.user import User, UserRole
from app.schemas.pipeline_mapping import (
    AttachMappingRequest,
    AttachMappingResponse,
    ConsistencyIssue,
    MappingUsageEntryOut,
    NodeMappingRefOut,
    UpgradeMappingRequest,
    UpgradeMappingResponse,
)
from app.services import mapping_service, pipeline_service

router = APIRouter(prefix="/pipelines/{pipeline_id}/mapping", tags=["pipeline-mapping"])

_view = require_action("VIEW", module="TRANSFORMATION")
_edit = require_action("EDIT", module="TRANSFORMATION")


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _get_pipeline_or_404(db: AsyncSession, pipeline_id: int, user: User):
    pipeline = await pipeline_service.get_pipeline(db, pipeline_id, _effective_org_id(user))
    if pipeline is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline not found")
    return pipeline


async def _get_mapping_or_404(db: AsyncSession, mapping_id: int, user: User):
    row = await mapping_service.get_mapping(db, mapping_id, _effective_org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mapping not found")
    return row


def _find_node(pipeline_definition: dict, node_id: str) -> dict | None:
    for node in (pipeline_definition or {}).get("nodes", []):
        if node.get("id") == node_id:
            return node
    return None


@router.get("/{node_id}", response_model=NodeMappingRefOut)
async def get_node_mapping(pipeline_id: int, node_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """Resolves straight from the pipeline's CURRENT PipelineVersion — the
    real source of truth for what this node runs right now, never the
    (history-only) column_mapping_usage table."""
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    version = await pipeline_service.get_current_version(db, pipeline)
    node = _find_node(version.pipeline_definition if version else {}, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Node '{node_id}' not found on pipeline's current version")

    cfg = node.get("config") or {}
    mapping_id = cfg.get("mappingId")
    mapping_version = cfg.get("mappingVersion")
    if mapping_id is None or mapping_version is None:
        return NodeMappingRefOut(node_id=node_id, mapping_id=None, mapping_version=None)

    mapping = await mapping_service.get_mapping(db, mapping_id, _effective_org_id(user))
    version_row = await mapping_service.get_version(db, mapping_id, mapping_version) if mapping else None
    return NodeMappingRefOut(
        node_id=node_id, mapping_id=mapping_id, mapping_version=mapping_version,
        mapping_name=mapping.name if mapping else None,
        mapping_status=version_row.status if version_row else None,
        is_current_published=bool(mapping and mapping.current_published_version == mapping_version),
    )


@router.post("/attach", response_model=AttachMappingResponse)
async def attach_mapping(pipeline_id: int, body: AttachMappingRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    mapping = await _get_mapping_or_404(db, body.mapping_id, user)
    version = await mapping_service.get_version(db, body.mapping_id, body.mapping_version)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Mapping version {body.mapping_version} not found")
    if version.status != "PUBLISHED":
        # Re-checked server-side regardless of the config panel already
        # filtering its picker to PUBLISHED-only — "never trust frontend
        # filtering" (spec section 46/49).
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Only a PUBLISHED mapping version can be attached to a pipeline (this version is {version.status}).")

    issues: list[ConsistencyIssue] = []
    if body.pipeline_source_connection_id is not None:
        if (body.pipeline_source_connection_id, body.pipeline_source_schema, body.pipeline_source_object) != (
            version.source_connection_id, version.source_schema, version.source_object
        ):
            issues.append(ConsistencyIssue(side="source", message="Selected mapping is not compatible with this pipeline's source — its source object differs from what the mapping was built against."))
    if body.pipeline_target_connection_id is not None:
        if (body.pipeline_target_connection_id, body.pipeline_target_schema, body.pipeline_target_object) != (
            version.target_connection_id, version.target_schema, version.target_object
        ):
            issues.append(ConsistencyIssue(side="target", message="Selected mapping is not compatible with this pipeline's target — its target object differs from what the mapping was built against."))

    usage = await mapping_service.record_usage(
        db, organization_id=_effective_org_id(user), mapping_id=body.mapping_id, mapping_version=body.mapping_version,
        pipeline_id=pipeline.id, pipeline_version=pipeline.current_version, node_id=body.node_id, created_by=user.id,
    )
    await record_event(
        db, organization_id=_effective_org_id(user), actor_id=user.id, action="Column Mapping Attached to Pipeline", module="Column Mapping",
        resource=mapping.name, resource_id=f"{body.mapping_id}:v{body.mapping_version}",
        status=AuditStatus.WARNING if issues else AuditStatus.SUCCESS,
        details=f"Pipeline #{pipeline.id} node {body.node_id}" + (f" — {len(issues)} consistency issue(s)" if issues else ""),
    )
    return AttachMappingResponse(usage_id=usage.id, consistent=not issues, issues=issues)


@router.post("/upgrade", response_model=UpgradeMappingResponse)
async def upgrade_mapping(pipeline_id: int, body: UpgradeMappingRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    """Patches ONE node's config.mappingVersion and saves it as a brand
    new PipelineVersion via the existing, already-immutable
    pipeline_service.save_version() — the pipeline's version history is
    never mutated, exactly like every other Pipeline Builder edit
    (spec section 38)."""
    pipeline = await _get_pipeline_or_404(db, pipeline_id, user)
    current = await pipeline_service.get_current_version(db, pipeline)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline has no current version")

    definition = current.pipeline_definition or {}
    nodes = list(definition.get("nodes", []))
    node = _find_node(definition, body.node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Node '{body.node_id}' not found on pipeline's current version")

    cfg = node.get("config") or {}
    mapping_id = cfg.get("mappingId")
    from_version = cfg.get("mappingVersion")
    if mapping_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This node has no mapping attached yet — use Attach, not Upgrade.")

    target_version = await mapping_service.get_version(db, mapping_id, body.to_version)
    if target_version is None or target_version.status != "PUBLISHED":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Version {body.to_version} is not a PUBLISHED version of mapping {mapping_id}.")

    new_nodes = []
    for n in nodes:
        if n.get("id") == body.node_id:
            n = {**n, "config": {**cfg, "mappingVersion": body.to_version}}
        new_nodes.append(n)

    mapping = await _get_mapping_or_404(db, mapping_id, user)
    updated_pipeline, new_version = await pipeline_service.save_version(
        db, pipeline, user_id=user.id, name=None, nodes=new_nodes, edges=definition.get("edges", []),
        settings=definition.get("settings", {}),
        change_summary=f"Upgraded Column Mapping '{mapping.name}' on node {body.node_id}: v{from_version} -> v{body.to_version}",
    )
    await mapping_service.record_usage(
        db, organization_id=_effective_org_id(user), mapping_id=mapping_id, mapping_version=body.to_version,
        pipeline_id=updated_pipeline.id, pipeline_version=new_version.version_number, node_id=body.node_id, created_by=user.id,
    )
    await record_event(
        db, organization_id=_effective_org_id(user), actor_id=user.id, action="Column Mapping Upgraded on Pipeline", module="Column Mapping",
        resource=mapping.name, resource_id=f"{mapping_id}:v{from_version}->v{body.to_version}",
        status=AuditStatus.SUCCESS,
        details=f"Pipeline #{updated_pipeline.id} node {body.node_id}: v{from_version} -> v{body.to_version} (pipeline version {new_version.version_number})",
    )
    return UpgradeMappingResponse(
        pipeline_id=updated_pipeline.id, new_pipeline_version=new_version.version_number, node_id=body.node_id,
        mapping_id=mapping_id, from_version=from_version, to_version=body.to_version,
    )
