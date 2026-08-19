"""Review Queue router — /api/review-queue/...

Human-in-the-loop layer between Document Intelligence's confidence/validation
check and the structured dataset. See services/review_queue_service.py.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_action
from app.database.connection import get_db
from app.models.user import User, UserRole
from app.schemas.review_queue import (
    AssignRequest,
    BulkApproveRequest,
    BulkAssignRequest,
    BulkRejectRequest,
    EscalateRequest,
    FieldUpdateRequest,
    RejectRequest,
    ReviewQueueCountOut,
    ReviewQueueDetailOut,
    ReviewQueueFacetsOut,
    ReviewQueueListOut,
    ReviewQueueStatsOut,
)
from app.services import review_queue_service as svc

router = APIRouter(prefix="/review-queue", tags=["review-queue"])


def _effective_org_id(user: User) -> int | None:
    if user.role == UserRole.SUPER_ADMIN:
        return None
    return user.organization_id


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Review item not found")


async def _get_or_404(db: AsyncSession, item_id: int, user: User):
    item = await svc.get_item(db, item_id, _effective_org_id(user))
    if item is None:
        raise _not_found()
    return item


# ── KPIs / count ──────────────────────────────────────────────────

@router.get("/stats", response_model=ReviewQueueStatsOut)
async def stats(user: User = Depends(require_action("VIEW")), db: AsyncSession = Depends(get_db)):
    return await svc.get_stats(db, _effective_org_id(user), user.id)


@router.get("/count", response_model=ReviewQueueCountOut)
async def count(user: User = Depends(require_action("VIEW")), db: AsyncSession = Depends(get_db)):
    """Backs the sidebar's Review Queue badge — never hardcode this number."""
    n = await svc.get_count(db, _effective_org_id(user))
    return ReviewQueueCountOut(pending=n)


@router.get("/facets", response_model=ReviewQueueFacetsOut)
async def facets(user: User = Depends(require_action("VIEW")), db: AsyncSession = Depends(get_db)):
    assignees = await svc.get_assignable_users(db, _effective_org_id(user))
    return ReviewQueueFacetsOut(assignees=assignees)


# ── List / get ────────────────────────────────────────────────────

@router.get("", response_model=ReviewQueueListOut)
async def list_items(
    q: str = Query("", description="Search document, id, issue, field, value, status, assignee"),
    status_filter: str | None = Query(None, alias="status"),
    priority: str | None = Query(None),
    review_type: str | None = Query(None),
    assigned: str | None = Query(None, description="unassigned|me|specific_user"),
    assigned_user_id: int | None = Query(None),
    document_type: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort_by: str = Query("created_at"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    user: User = Depends(require_action("VIEW")),
    db: AsyncSession = Depends(get_db),
):
    entries, total = await svc.list_items(
        db,
        organization_id=_effective_org_id(user),
        requesting_user_id=user.id,
        search=q, status_filter=status_filter, priority_filter=priority,
        review_type_filter=review_type, assigned=assigned, assigned_user_id=assigned_user_id,
        document_type_filter=document_type, page=page, page_size=page_size,
        sort_by=sort_by, sort_dir=sort_dir,
    )
    return ReviewQueueListOut(items=[svc.to_list_out(e) for e in entries], total=total, page=page, page_size=page_size)



# ── Bulk operations ───────────────────────────────────────────────
# NOTE: these literal-path routes (/bulk/assign etc.) must be registered
# BEFORE the parameterized /{review_id}/... routes below. Starlette matches
# routes in registration order using positional segments, not types — a
# request to POST /bulk/reject would otherwise match /{review_id}/reject
# first (with "bulk" captured as review_id) and 422 on int coercion.

@router.post("/bulk/assign")
async def bulk_assign(body: BulkAssignRequest, user: User = Depends(require_action("EDIT")), db: AsyncSession = Depends(get_db)):
    results = {"succeeded": [], "failed": []}
    for review_id in body.review_ids:
        item = await svc.get_item(db, review_id, _effective_org_id(user))
        if item is None:
            results["failed"].append({"id": review_id, "reason": "not_found"})
            continue
        await svc.assign(db, item, body.user_id, user)
        results["succeeded"].append(review_id)
    return results


@router.post("/bulk/approve")
async def bulk_approve(body: BulkApproveRequest, user: User = Depends(require_action("APPROVE")), db: AsyncSession = Depends(get_db)):
    results = {"succeeded": [], "failed": []}
    for review_id in body.review_ids:
        item = await svc.get_item(db, review_id, _effective_org_id(user))
        if item is None:
            results["failed"].append({"id": review_id, "reason": "not_found"})
            continue
        await svc.approve(db, item, user)
        results["succeeded"].append(review_id)
    return results


@router.post("/bulk/reject")
async def bulk_reject(body: BulkRejectRequest, user: User = Depends(require_action("APPROVE")), db: AsyncSession = Depends(get_db)):
    if not body.reason or not body.reason.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A rejection reason is required.")
    results = {"succeeded": [], "failed": []}
    for review_id in body.review_ids:
        item = await svc.get_item(db, review_id, _effective_org_id(user))
        if item is None:
            results["failed"].append({"id": review_id, "reason": "not_found"})
            continue
        await svc.reject(db, item, body.reason, user)
        results["succeeded"].append(review_id)
    return results


# ── Get single item ───────────────────────────────────────────────

@router.get("/{review_id}", response_model=ReviewQueueDetailOut)
async def get_item(review_id: int, user: User = Depends(require_action("VIEW")), db: AsyncSession = Depends(get_db)):
    item = await _get_or_404(db, review_id, user)
    return await svc.get_detail(db, item)


# ── Actions ───────────────────────────────────────────────────────

@router.post("/{review_id}/assign", response_model=ReviewQueueDetailOut)
async def assign_item(review_id: int, body: AssignRequest, user: User = Depends(require_action("EDIT")), db: AsyncSession = Depends(get_db)):
    item = await _get_or_404(db, review_id, user)
    item = await svc.assign(db, item, body.user_id, user)
    return await svc.get_detail(db, item)


@router.patch("/{review_id}/fields", response_model=ReviewQueueDetailOut)
async def update_fields(review_id: int, body: FieldUpdateRequest, user: User = Depends(require_action("EDIT")), db: AsyncSession = Depends(get_db)):
    item = await _get_or_404(db, review_id, user)
    item = await svc.update_field(db, item, body.field_name, body.corrected_value, body.reason, user)
    return await svc.get_detail(db, item)


@router.post("/{review_id}/approve", response_model=ReviewQueueDetailOut)
async def approve_item(review_id: int, user: User = Depends(require_action("APPROVE")), db: AsyncSession = Depends(get_db)):
    item = await _get_or_404(db, review_id, user)
    item = await svc.approve(db, item, user)
    return await svc.get_detail(db, item)


@router.post("/{review_id}/reject", response_model=ReviewQueueDetailOut)
async def reject_item(review_id: int, body: RejectRequest, user: User = Depends(require_action("APPROVE")), db: AsyncSession = Depends(get_db)):
    if not body.reason or not body.reason.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A rejection reason is required.")
    item = await _get_or_404(db, review_id, user)
    item = await svc.reject(db, item, body.reason, user)
    return await svc.get_detail(db, item)


@router.post("/{review_id}/escalate", response_model=ReviewQueueDetailOut)
async def escalate_item(review_id: int, body: EscalateRequest, user: User = Depends(require_action("APPROVE")), db: AsyncSession = Depends(get_db)):
    item = await _get_or_404(db, review_id, user)
    item = await svc.escalate(db, item, body.reason, body.assign_to_user_id, user)
    return await svc.get_detail(db, item)
