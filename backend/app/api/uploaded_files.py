"""Uploaded dataset files router — /api/uploaded-files/...

Real file upload for Validation's object picker and Reconciliation's
Source/Target pickers' "Upload File" option — see
services/uploaded_file_service.py. Foundational module (module=None),
same tier as /api/pipeline-files/... — actual USE of an uploaded file is
already gated by validation/reconciliation's own CREATE/EXECUTE checks,
this just owns the upload+storage step both share.
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.schemas.uploaded_file import UploadedFileColumnsOut, UploadedFileOut
from app.services import uploaded_file_service as svc

router = APIRouter(prefix="/uploaded-files", tags=["uploaded-files"])

_view = require_action("VIEW", module=None)
_create = require_action("CREATE", module=None)
_delete = require_action("DELETE", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Uploaded file not found.")


@router.post("/upload", response_model=UploadedFileOut, status_code=status.HTTP_201_CREATED)
async def upload(file: UploadFile = File(...), user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    try:
        return await svc.upload_dataset_file(db, file=file, organization_id=user.organization_id, uploaded_by_id=user.id)
    except svc.UploadedFileValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/{file_id}", response_model=UploadedFileOut)
async def get_file(file_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    record = await svc.get_by_id(db, file_id, _effective_org_id(user))
    if record is None:
        raise _not_found()
    return record


@router.get("/{file_id}/columns", response_model=UploadedFileColumnsOut)
async def get_columns(file_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    record = await svc.get_by_id(db, file_id, _effective_org_id(user))
    if record is None:
        raise _not_found()
    try:
        columns = await svc.get_columns(record)
    except svc.UploadedFileValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return UploadedFileColumnsOut(columns=columns)


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(file_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    record = await svc.get_by_id(db, file_id, _effective_org_id(user))
    if record is None:
        raise _not_found()
    await svc.delete_uploaded_file(db, record)
