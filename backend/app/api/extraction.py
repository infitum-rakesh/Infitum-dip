"""Extraction router — /api/extraction/...

Real AI-based field extraction for the Document Intelligence wizard's AI
Extraction stage — see services/extraction_service.py for what's actually
real (every call genuinely hits the selected AI Provider connection).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.connection import get_db
from app.models.document import Document
from app.models.user import User, UserRole
from app.schemas.extraction import DetectSchemaOut, DetectSchemaRequest, ExtractOut, ExtractRequest
from app.services import document_service
from app.services import extraction_service as svc

router = APIRouter(prefix="/extraction", tags=["extraction"])

_execute = require_action("EXECUTE", module=None)


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


async def _get_doc(db: AsyncSession, document_id: int, organization_id: int | None) -> Document:
    doc = await db.get(Document, document_id)
    if doc is None or (organization_id is not None and doc.organization_id != organization_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


@router.post("/detect-schema", response_model=DetectSchemaOut)
async def detect_schema(body: DetectSchemaRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_db)):
    """document_type (e.g. "INVOICE") steers this at a comprehensive
    canonical schema — see document_taxonomies.py — instead of the ad-hoc
    "guess 5-12 fields" LLM call, same as Document Details' "Run AI
    Extraction" picker (document_service.run_manual_ai_extraction). Also
    merges in field names mined from this org's own past extractions
    (discover_known_field_names) for the same reason that path does."""
    org_id = _effective_org_id(user)
    doc = await _get_doc(db, body.document_id, org_id)
    try:
        extra_names = await document_service.discover_known_field_names(db, org_id) if body.document_type else None
        fields = await svc.detect_schema(
            db, doc, body.connection_id, org_id,
            document_type=body.document_type, extra_field_names=extra_names,
        )
    except svc.ExtractionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return DetectSchemaOut(fields=fields)


@router.post("/extract", response_model=ExtractOut)
async def extract(body: ExtractRequest, user: User = Depends(_execute), db: AsyncSession = Depends(get_db)):
    doc = await _get_doc(db, body.document_id, _effective_org_id(user))
    try:
        result = await svc.extract_fields(
            db, doc, [f.model_dump() for f in body.fields], body.connection_id, _effective_org_id(user),
        )
    except svc.ExtractionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return ExtractOut(**result)
