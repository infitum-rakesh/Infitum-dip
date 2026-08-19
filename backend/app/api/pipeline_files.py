"""Pipeline source files router — /api/pipeline-files/...

Real file upload for the Transform Workspace's Source Dataset "Upload
File" option — see services/pipeline_file_service.py. Foundational
module (module=None), same tier as /api/pipelines/... itself."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.models.user import User, UserRole
from app.schemas.pipeline_files import AttachToPipelineRequest, PipelineSourceFileOut, SetSheetRequest
from app.services import pipeline_file_service as svc

router = APIRouter(prefix="/pipeline-files", tags=["pipeline-files"])

_view = require_action("VIEW", module=None)
_create = require_action("CREATE", module=None)
_edit = require_action("EDIT", module=None)
_delete = require_action("DELETE", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "No file has been uploaded for this node yet.")


@router.post("/upload", response_model=PipelineSourceFileOut, status_code=status.HTTP_201_CREATED)
async def upload(
    node_id: str = Form(...),
    pipeline_id: int | None = Form(None),
    file: UploadFile = File(...),
    user: User = Depends(_create),
    db: AsyncSession = Depends(get_db),
):
    try:
        record = await svc.upload_source_file(
            db, file=file, node_id=node_id, pipeline_id=pipeline_id,
            organization_id=user.organization_id, uploaded_by_id=user.id,
        )
    except svc.PipelineFileValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return record


@router.get("/by-node/{node_id}", response_model=PipelineSourceFileOut)
async def get_by_node(node_id: str, user: User = Depends(_view), db: AsyncSession = Depends(get_db)):
    record = await svc.get_by_node(db, node_id, _effective_org_id(user))
    if record is None:
        raise _not_found()
    return record


@router.post("/{file_id}/attach", response_model=PipelineSourceFileOut)
async def attach(file_id: int, body: AttachToPipelineRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    record = await svc.get_by_id(db, file_id, _effective_org_id(user))
    if record is None:
        raise _not_found()
    return await svc.attach_to_pipeline(db, record, body.pipeline_id)


@router.put("/{file_id}/sheet", response_model=PipelineSourceFileOut)
async def set_sheet(file_id: int, body: SetSheetRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_db)):
    record = await svc.get_by_id(db, file_id, _effective_org_id(user))
    if record is None:
        raise _not_found()
    try:
        return await svc.set_sheet(db, record, body.sheet_name)
    except svc.PipelineFileValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(file_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_db)):
    record = await svc.get_by_id(db, file_id, _effective_org_id(user))
    if record is None:
        raise _not_found()
    await svc.delete_source_file(db, record)
