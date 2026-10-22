"""Mapping router — /api/mappings/...

Object & Column Mapping (Phase 4). Auto Map / Validate / Preview operate
on an in-progress mapping definition passed in the request body — they
work before a mapping is ever saved, matching the "Auto Map / Validate /
Preview" buttons on the full-screen editor (spec section 34). Only
Create/Update actually persists a ColumnMapping row.

NOTE: literal-path routes (/auto-map, /validate, /preview) are registered
before /{mapping_id} for the same reason as connections.py's /stats —
Starlette tries routes in registration order, and /{mapping_id} would
otherwise capture them first and 422 on int coercion.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.execution import mapping_execution as mapping_execution_module
from app.models.user import User, UserRole
from app.schemas.connections import ColumnOut
from app.schemas.document_mapping import (
    DocumentMappingDefaultListOut,
    DocumentMappingDefaultOut,
    DocumentSourceColumnOut,
    DocumentSourceColumnsOut,
    PreviewAgainstDocumentRequest,
    PreviewAgainstDocumentResponse,
    RetryScheduledMappingResponse,
    SampleDocumentListOut,
    SampleDocumentOut,
    ScheduleUsingMappingListOut,
    ScheduleUsingMappingOut,
    SetDocumentMappingDefaultRequest,
)
from app.schemas.mapping import (
    AutoMapRequest,
    AutoMapResponse,
    ColumnMappingCreateRequest,
    ColumnMappingDetailOut,
    ColumnMappingListOut,
    ColumnMappingOut,
    ColumnMappingUpdateRequest,
    ColumnMappingVersionListOut,
    ColumnMappingVersionOut,
    CreateVersionRequest,
    MappingPreviewRequest,
    MappingPreviewResponse,
    MappingUsageListOut,
    MappingUsageOut,
    ObjectRef,
    PublishRequest,
    RetireRequest,
    StalenessOut,
    ValidateRequest,
    ValidateResponse,
)
from app.services import document_mapping_resolution_service
from app.services import document_mapping_service as doc_mapping_svc
from app.services import document_service
from app.services import document_taxonomies
from app.services import mapping_service as svc
from app.services.document_mapping_service import DocumentMappingError

router = APIRouter(prefix="/mappings", tags=["mapping"])

_view = require_action("VIEW", module="TRANSFORMATION")
_create = require_action("CREATE", module="TRANSFORMATION")
_edit = require_action("EDIT", module="TRANSFORMATION")
_delete = require_action("DELETE", module="TRANSFORMATION")
_execute = require_action("EXECUTE", module="TRANSFORMATION")


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _to_out(db: AsyncSession, row) -> dict:
    """CM2 — `mappings`/`status` now resolve through the effective
    (current published, else latest) ColumnMappingVersion rather than the
    header row's own (retired-for-writes) columns, so existing consumers
    of ColumnMappingOut (ObjectColumnMapping.jsx, load_executor.py's
    predecessor logic) keep seeing a single coherent "current mapping"
    without needing to know versioning exists."""
    names = await svc.user_name_map(db, {i for i in (row.created_by, row.updated_by) if i})
    effective = await svc.get_effective_mappings(db, row)
    out = {
        **{c.name: getattr(row, c.name) for c in row.__table__.columns},
        "created_by_name": names.get(row.created_by),
        "updated_by_name": names.get(row.updated_by),
    }
    if effective is not None:
        out["mappings"] = effective.mappings
        out["status"] = effective.status
    return out


# ── Engine operations (must precede /{mapping_id}) ─────────────────

@router.post("/auto-map", response_model=AutoMapResponse)
async def auto_map(body: AutoMapRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    try:
        proposals = await svc.run_auto_map(
            db, organization_id=_effective_org_id(user), source=body.source, target=body.target,
            existing=[m.model_dump() for m in body.existing_mappings],
        )
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return AutoMapResponse(proposals=proposals, ai_assisted_available=False)


@router.post("/validate", response_model=ValidateResponse)
async def validate(body: ValidateRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    try:
        result = await svc.run_validate(
            db, organization_id=_effective_org_id(user), source=body.source, target=body.target,
            mappings=[m.model_dump() for m in body.mappings],
        )
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ValidateResponse(**result)


@router.post("/preview", response_model=MappingPreviewResponse)
async def preview(body: MappingPreviewRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    try:
        result = await svc.run_preview(
            db, organization_id=_effective_org_id(user), source=body.source,
            mappings=[m.model_dump() for m in body.mappings], limit=body.limit,
        )
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return MappingPreviewResponse(columns=result.columns, rows=result.rows, row_count=result.row_count, truncated=result.truncated)


# ── Document Mapping Profiles helpers (must precede /{mapping_id}) ──

@router.get("/document-types", response_model=list[str])
async def list_document_source_types(user: User = Depends(_view)):
    """Picker data for building a document-sourced mapping profile's
    source ObjectRef(document_type=...) — the same taxonomy
    extraction/review already use, not a separate list."""
    return document_taxonomies.available_document_types()


@router.get("/document-source-columns", response_model=DocumentSourceColumnsOut)
async def get_document_source_columns(document_type: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """Picker data for the source side of a document-sourced mapping
    profile — same columns fetch_columns() would return for
    ObjectRef(document_type=...), exposed directly so the UI can build
    that ObjectRef without first creating a mapping."""
    try:
        cols = await svc.fetch_columns(db, ObjectRef(document_type=document_type), _effective_org_id(user))
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return DocumentSourceColumnsOut(
        document_type=document_type,
        columns=[DocumentSourceColumnOut(name=c.name, data_type=c.data_type) for c in cols],
    )


@router.get("/invoice-schema-columns", response_model=list[ColumnOut])
async def get_invoice_schema_columns(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """Picker data for the target side of a mapping profile whose target
    is the real ApInvoice schema (ObjectRef(target_format="INVOICE_SCHEMA"))."""
    cols = await svc.fetch_columns(db, ObjectRef(target_format="INVOICE_SCHEMA"), _effective_org_id(user))
    return [ColumnOut(**vars(c)) for c in cols]


@router.get("/sample-documents", response_model=SampleDocumentListOut)
async def get_sample_documents(document_type: str, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """Mapping editor's "Test Against Sample Document" picker — real,
    already-processed documents of this type the user can run an
    in-progress mapping definition against before ever saving it."""
    docs = await doc_mapping_svc.list_sample_documents(db, organization_id=_effective_org_id(user), document_type=document_type)
    return SampleDocumentListOut(items=[SampleDocumentOut(**d) for d in docs])


@router.post("/preview-document", response_model=PreviewAgainstDocumentResponse)
async def preview_mapping_against_document(body: PreviewAgainstDocumentRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    """Mapping editor's "Test Against Sample Document" run action — a
    dry-run of the real engine against an in-progress (possibly unsaved)
    mapping definition. Never persists a DocumentMappedOutput row (see
    document_mapping_service.preview_mapping_against_document's own
    docstring) — apply-mapping/{mapping_id} on api/documents.py is the
    real, persisted, feature-flag-gated action."""
    document = await document_service.get_document(db, body.document_id, _effective_org_id(user))
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    try:
        result = await doc_mapping_svc.preview_mapping_against_document(
            db, document=document, source_document_type=body.source_document_type, target_format=body.target_format,
            mappings=body.mappings, organization_id=_effective_org_id(user),
        )
    except DocumentMappingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except mapping_execution_module.MappingExecutionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return PreviewAgainstDocumentResponse(**result)


# ── Scheduled Mapping — org defaults + usage (spec section 7.3; must
# precede /{mapping_id}) ─────────────────────────────────────────────

@router.get("/defaults", response_model=DocumentMappingDefaultListOut)
async def list_mapping_defaults(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    rows = await document_mapping_resolution_service.list_defaults(db, _effective_org_id(user))
    names = await svc.mapping_name_map(db, {r.mapping_id for r in rows}) if rows else {}
    return DocumentMappingDefaultListOut(items=[
        DocumentMappingDefaultOut(**{c.name: getattr(r, c.name) for c in r.__table__.columns}, mapping_name=names.get(r.mapping_id))
        for r in rows
    ])


@router.put("/defaults", response_model=DocumentMappingDefaultOut)
async def set_mapping_default(body: SetDocumentMappingDefaultRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    try:
        row = await document_mapping_resolution_service.set_default(
            db, organization_id=_effective_org_id(user), document_type=body.document_type,
            mapping_id=body.mapping_id, actor_id=user.id,
        )
    except document_mapping_resolution_service.DefaultMappingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    mapping = await svc.get_mapping(db, row.mapping_id, _effective_org_id(user))
    return DocumentMappingDefaultOut(
        **{c.name: getattr(row, c.name) for c in row.__table__.columns},
        mapping_name=mapping.name if mapping else None,
    )


@router.delete("/defaults/{document_type}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_mapping_default(document_type: str, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    await document_mapping_resolution_service.clear_default(db, organization_id=_effective_org_id(user), document_type=document_type, actor_id=user.id)


# ── CRUD ─────────────────────────────────────────────────────────

@router.get("", response_model=ColumnMappingListOut)
async def list_mappings(status: str | None = None, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    rows, total = await svc.list_mappings(db, _effective_org_id(user), published_only=(status == "PUBLISHED"))
    items = [await _to_out(db, r) for r in rows]
    return ColumnMappingListOut(items=items, total=total)


@router.post("", response_model=ColumnMappingOut, status_code=status.HTTP_201_CREATED)
async def create_mapping(body: ColumnMappingCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    try:
        row = await svc.create_mapping(
            db, organization_id=user.organization_id, name=body.name, source=body.source, target=body.target,
            mappings=[m.model_dump() for m in body.mappings], created_by=user.id,
        )
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, row)


async def _get_or_404(db: AsyncSession, mapping_id: int, user: User):
    row = await svc.get_mapping(db, mapping_id, _effective_org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mapping not found")
    return row


@router.get("/{mapping_id}", response_model=ColumnMappingDetailOut)
async def get_mapping(mapping_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_or_404(db, mapping_id, user)
    out = await _to_out(db, row)

    try:
        source_cols = await svc.fetch_columns(
            db, ObjectRef(
                connection_id=row.source_connection_id, schema_name=row.source_schema, object_name=row.source_object,
                document_type=row.source_document_type,
            ),
            _effective_org_id(user),
        )
        target_cols = await svc.fetch_columns(
            db, ObjectRef(
                connection_id=row.target_connection_id, schema_name=row.target_schema, object_name=row.target_object,
                target_format=row.target_format,
            ),
            _effective_org_id(user),
        )
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    out["source_columns"] = [ColumnOut(**vars(c)) for c in source_cols]
    out["target_columns"] = [ColumnOut(**vars(c)) for c in target_cols]
    return out


@router.put("/{mapping_id}", response_model=ColumnMappingOut)
async def update_mapping(mapping_id: int, body: ColumnMappingUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    """Header-only edit (name) — CM2. Mapping-row edits go through
    POST /mappings/{id}/versions (create_new_version) instead; a PUT here
    with mappings in the body would silently be a no-op under the old
    field, so the field itself is gone from ColumnMappingUpdateRequest."""
    row = await _get_or_404(db, mapping_id, user)
    row = await svc.update_mapping(db, row, name=body.name, updated_by=user.id)
    return await _to_out(db, row)


@router.delete("/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mapping(mapping_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_or_404(db, mapping_id, user)
    await svc.delete_mapping(db, row, actor_id=user.id)


# ── Versioning / publish lifecycle (CM2/CM3) ────────────────────────

async def _version_to_out(db: AsyncSession, version) -> dict:
    names = await svc.user_name_map(db, {version.created_by} if version.created_by else set())
    return {
        **{c.name: getattr(version, c.name) for c in version.__table__.columns},
        "created_by_name": names.get(version.created_by),
    }


async def _get_version_or_404(db: AsyncSession, mapping_id: int, version_number: int, user: User):
    await _get_or_404(db, mapping_id, user)  # 404s + org-scopes the parent first
    version = await svc.get_version(db, mapping_id, version_number)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Version {version_number} not found")
    return version


@router.get("/{mapping_id}/versions", response_model=ColumnMappingVersionListOut)
async def list_versions(mapping_id: int, status: str | None = None, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    await _get_or_404(db, mapping_id, user)
    versions = await svc.list_versions(db, mapping_id)
    if status:
        versions = [v for v in versions if v.status == status]
    items = [await _version_to_out(db, v) for v in versions]
    return ColumnMappingVersionListOut(items=items, total=len(items))


@router.get("/{mapping_id}/versions/{version_number}", response_model=ColumnMappingVersionOut)
async def get_version(mapping_id: int, version_number: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    version = await _get_version_or_404(db, mapping_id, version_number, user)
    return await _version_to_out(db, version)


@router.post("/{mapping_id}/versions", response_model=ColumnMappingVersionOut, status_code=status.HTTP_201_CREATED)
async def create_version(mapping_id: int, body: CreateVersionRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_or_404(db, mapping_id, user)
    version = await svc.create_new_version(
        db, row, mappings=[m.model_dump() for m in body.mappings], change_description=body.change_description,
        unmapped_column_policy=body.unmapped_column_policy, batch_size=body.batch_size,
        on_conversion_failure=body.on_conversion_failure, created_by=user.id,
    )
    return await _version_to_out(db, version)


@router.post("/{mapping_id}/versions/{version_number}/validate", response_model=ValidateResponse)
async def validate_version(mapping_id: int, version_number: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    version = await _get_version_or_404(db, mapping_id, version_number, user)
    try:
        result = await svc.run_validate_version(db, organization_id=_effective_org_id(user), version=version, actor_id=user.id)
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ValidateResponse(**result)


@router.post("/{mapping_id}/versions/{version_number}/publish", response_model=ColumnMappingVersionOut)
async def publish_version(mapping_id: int, version_number: int, body: PublishRequest = PublishRequest(), user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_or_404(db, mapping_id, user)
    version = await _get_version_or_404(db, mapping_id, version_number, user)
    try:
        version = await svc.publish_version(db, mapping=row, version=version, actor_id=user.id)
    except svc.MappingLifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _version_to_out(db, version)


@router.post("/{mapping_id}/versions/{version_number}/retire", response_model=ColumnMappingVersionOut)
async def retire_version(mapping_id: int, version_number: int, body: RetireRequest = RetireRequest(), user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_or_404(db, mapping_id, user)
    version = await _get_version_or_404(db, mapping_id, version_number, user)
    try:
        version = await svc.retire_version(db, mapping=row, version=version, actor_id=user.id, confirm=body.confirm)
    except svc.MappingLifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _version_to_out(db, version)


@router.get("/{mapping_id}/usage", response_model=MappingUsageListOut)
async def get_usage(mapping_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """De-duplicated to the LATEST usage row per distinct pipeline — "used
    by N pipelines" (spec section 37), not a raw event log (see
    list_usage()'s own docstring for the full history that's collapsed
    here)."""
    await _get_or_404(db, mapping_id, user)
    all_usage = await svc.list_usage(db, mapping_id)
    latest_per_pipeline: dict[int, object] = {}
    for u in all_usage:  # already sorted newest-first
        latest_per_pipeline.setdefault(u.pipeline_id, u)
    rows = list(latest_per_pipeline.values())
    names = await svc.pipeline_name_map(db, {r.pipeline_id for r in rows})
    items = [
        MappingUsageOut(
            id=r.id, mapping_id=r.mapping_id, mapping_version=r.mapping_version,
            pipeline_id=r.pipeline_id, pipeline_name=names.get(r.pipeline_id),
            pipeline_version=r.pipeline_version, node_id=r.node_id, environment=r.environment, created_at=r.created_at,
        )
        for r in rows
    ]
    return MappingUsageListOut(items=items, total=len(items))


@router.get("/{mapping_id}/schedules", response_model=ScheduleUsingMappingListOut)
async def get_schedules_using_mapping(mapping_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    """Section 7.3 — "view which schedules use each profile"."""
    await _get_or_404(db, mapping_id, user)
    usages = await document_mapping_resolution_service.list_schedules_using_mapping(db, mapping_id=mapping_id, organization_id=_effective_org_id(user))
    return ScheduleUsingMappingListOut(items=[ScheduleUsingMappingOut(**u) for u in usages])


@router.get("/{mapping_id}/staleness", response_model=StalenessOut)
async def get_staleness(mapping_id: int, version: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    version_row = await _get_version_or_404(db, mapping_id, version, user)
    try:
        result = await svc.check_staleness(db, organization_id=_effective_org_id(user), version=version_row)
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return StalenessOut(**result)
