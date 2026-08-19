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
from app.database.connection import get_db
from app.models.user import User, UserRole
from app.schemas.connections import ColumnOut
from app.schemas.mapping import (
    AutoMapRequest,
    AutoMapResponse,
    ColumnMappingCreateRequest,
    ColumnMappingDetailOut,
    ColumnMappingListOut,
    ColumnMappingOut,
    ColumnMappingUpdateRequest,
    MappingPreviewRequest,
    MappingPreviewResponse,
    ObjectRef,
    ValidateRequest,
    ValidateResponse,
)
from app.services import mapping_service as svc

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
    names = await svc.user_name_map(db, {i for i in (row.created_by, row.updated_by) if i})
    return {
        **{c.name: getattr(row, c.name) for c in row.__table__.columns},
        "created_by_name": names.get(row.created_by),
        "updated_by_name": names.get(row.updated_by),
    }


# ── Engine operations (must precede /{mapping_id}) ─────────────────

@router.post("/auto-map", response_model=AutoMapResponse)
async def auto_map(body: AutoMapRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_db)):
    try:
        proposals = await svc.run_auto_map(
            db, organization_id=_effective_org_id(user), source=body.source, target=body.target,
            existing=[m.model_dump() for m in body.existing_mappings],
        )
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return AutoMapResponse(proposals=proposals, ai_assisted_available=False)


@router.post("/validate", response_model=ValidateResponse)
async def validate(body: ValidateRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_db)):
    try:
        result = await svc.run_validate(
            db, organization_id=_effective_org_id(user), source=body.source, target=body.target,
            mappings=[m.model_dump() for m in body.mappings],
        )
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ValidateResponse(**result)


@router.post("/preview", response_model=MappingPreviewResponse)
async def preview(body: MappingPreviewRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_db)):
    try:
        result = await svc.run_preview(
            db, organization_id=_effective_org_id(user), source=body.source,
            mappings=[m.model_dump() for m in body.mappings], limit=body.limit,
        )
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return MappingPreviewResponse(columns=result.columns, rows=result.rows, row_count=result.row_count, truncated=result.truncated)


# ── CRUD ─────────────────────────────────────────────────────────

@router.get("", response_model=ColumnMappingListOut)
async def list_mappings(user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    rows, total = await svc.list_mappings(db, _effective_org_id(user))
    items = [await _to_out(db, r) for r in rows]
    return ColumnMappingListOut(items=items, total=total)


@router.post("", response_model=ColumnMappingOut, status_code=status.HTTP_201_CREATED)
async def create_mapping(body: ColumnMappingCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_db)):
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
async def get_mapping(mapping_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, mapping_id, user)
    out = await _to_out(db, row)

    try:
        source_cols = await svc.fetch_columns(
            db, ObjectRef(connection_id=row.source_connection_id, schema_name=row.source_schema, object_name=row.source_object),
            _effective_org_id(user),
        )
        target_cols = await svc.fetch_columns(
            db, ObjectRef(connection_id=row.target_connection_id, schema_name=row.target_schema, object_name=row.target_object),
            _effective_org_id(user),
        )
    except svc.MappingValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    out["source_columns"] = [ColumnOut(**vars(c)) for c in source_cols]
    out["target_columns"] = [ColumnOut(**vars(c)) for c in target_cols]
    return out


@router.put("/{mapping_id}", response_model=ColumnMappingOut)
async def update_mapping(mapping_id: int, body: ColumnMappingUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, mapping_id, user)
    row = await svc.update_mapping(
        db, row, name=body.name, mappings=[m.model_dump() for m in body.mappings] if body.mappings is not None else None,
        updated_by=user.id,
    )
    return await _to_out(db, row)


@router.delete("/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mapping(mapping_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_db)):
    row = await _get_or_404(db, mapping_id, user)
    await svc.delete_mapping(db, row)
