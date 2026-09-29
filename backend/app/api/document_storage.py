"""Processed Data Storage router — /api/document-storage

Administration-level configuration for where Document Intelligence's
processed output (OCR text, extraction fields, dataset, raw AI response)
gets pushed after each document finishes — see services/
document_storage_service.py's module docstring. Same single ADMIN-tier
gating convention as api/settings.py.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User
from app.schemas.document_storage import (
    ProcessedDataStorageConfigOut,
    ProcessedDataStorageConfigUpdateRequest,
    TestConnectionResultOut,
)
from app.services import document_storage_service as svc

router = APIRouter(prefix="/document-storage", tags=["document-storage"])

_admin = require_action("ADMIN", module=None)


@router.get("/config", response_model=ProcessedDataStorageConfigOut)
async def get_config(user: User = Depends(_admin), db: AsyncSession = Depends(get_tenant_db)):
    row = await svc.get_or_create_config(db, user.organization_id)
    return row


@router.put("/config", response_model=ProcessedDataStorageConfigOut)
async def update_config(
    body: ProcessedDataStorageConfigUpdateRequest, user: User = Depends(_admin), db: AsyncSession = Depends(get_tenant_db),
):
    row = await svc.get_or_create_config(db, user.organization_id)
    target_connection_id = body.target_connection_id if body.target_connection_id is not None \
        else (None if body.clear_target_connection else ...)
    target_schema = body.target_schema if body.target_schema is not None \
        else (None if body.clear_target_schema else ...)
    try:
        row = await svc.update_config(
            db, row, organization_id=user.organization_id, updated_by=user.id,
            mode=body.mode, storage_type=body.storage_type, load_mode=body.load_mode, file_format=body.file_format,
            target_connection_id=target_connection_id, target_schema=target_schema,
            target_table=body.target_table, include_ocr_text=body.include_ocr_text,
            include_extraction_fields=body.include_extraction_fields, include_dataset=body.include_dataset,
            include_ai_raw_response=body.include_ai_raw_response,
        )
    except svc.DocumentStorageServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return row


@router.post("/config/test-connection", response_model=TestConnectionResultOut)
async def test_connection(user: User = Depends(_admin), db: AsyncSession = Depends(get_tenant_db)):
    row = await svc.get_or_create_config(db, user.organization_id)
    try:
        result = await svc.test_target_connection(db, row, user.organization_id)
    except svc.DocumentStorageServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return result
