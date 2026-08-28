"""Validation router — /api/validation/... (Phase 7).

Real data-quality checking: a rule set's real aggregate SQL runs against
the live object via validation/engine.py — nothing here is mocked. See
that module's docstring for how one query covers every enabled rule.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.schemas.validation import (
    FailedRecordsOut,
    RuleSetCreateRequest,
    RuleSetListOut,
    RuleSetOut,
    RuleSetUpdateRequest,
    ValidationRunOut,
)
from app.services import validation_service as svc

router = APIRouter(prefix="/validation", tags=["validation"])

_view = require_action("VIEW", module="VALIDATION")
_create = require_action("CREATE", module="VALIDATION")
_edit = require_action("EDIT", module="VALIDATION")
_delete = require_action("DELETE", module="VALIDATION")
_execute = require_action("EXECUTE", module="VALIDATION")


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


async def _get_rule_set_or_404(db: AsyncSession, rule_set_id: int, user: User):
    row = await svc.get_rule_set(db, rule_set_id, _effective_org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule set not found")
    return row


# ── Rule set CRUD ────────────────────────────────────────────────

@router.get("/rulesets", response_model=RuleSetListOut)
async def list_rule_sets(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    rows, total = await svc.list_rule_sets(db, _effective_org_id(user))
    items = [await _to_out(db, r) for r in rows]
    return RuleSetListOut(items=items, total=total)


@router.post("/rulesets", response_model=RuleSetOut, status_code=status.HTTP_201_CREATED)
async def create_rule_set(body: RuleSetCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    try:
        row = await svc.create_rule_set(
            db, organization_id=user.organization_id, name=body.name, connection_id=body.connection_id,
            schema_name=body.schema_name, object_name=body.object_name, uploaded_file_id=body.uploaded_file_id,
            rules=body.rules, created_by=user.id,
        )
    except svc.ValidationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, row)


@router.get("/rulesets/{rule_set_id}", response_model=RuleSetOut)
async def get_rule_set(rule_set_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_rule_set_or_404(db, rule_set_id, user)
    return await _to_out(db, row)


@router.put("/rulesets/{rule_set_id}", response_model=RuleSetOut)
async def update_rule_set(rule_set_id: int, body: RuleSetUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_rule_set_or_404(db, rule_set_id, user)
    try:
        row = await svc.update_rule_set(db, row, name=body.name, rules=body.rules, updated_by=user.id)
    except svc.ValidationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _to_out(db, row)


@router.delete("/rulesets/{rule_set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule_set(rule_set_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    row = await _get_rule_set_or_404(db, rule_set_id, user)
    await svc.delete_rule_set(db, row)


# ── Run ──────────────────────────────────────────────────────────

@router.post("/rulesets/{rule_set_id}/run", response_model=ValidationRunOut, status_code=status.HTTP_201_CREATED)
async def run_rule_set(rule_set_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    rule_set = await _get_rule_set_or_404(db, rule_set_id, user)
    if not rule_set.rules:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This rule set has no rules to run — add at least one first.")
    try:
        run = await svc.run_rule_set(db, organization_id=user.organization_id, rule_set=rule_set, created_by=user.id)
    except svc.ValidationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    results = await svc.list_results(db, run.id)
    out = ValidationRunOut.model_validate(run, from_attributes=True).model_dump()
    out["results"] = results
    return out


@router.get("/rulesets/{rule_set_id}/runs", response_model=list[ValidationRunOut])
async def list_runs(rule_set_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    runs, _ = await svc.list_runs(db, rule_set_id, _effective_org_id(user))
    out = []
    for run in runs:
        results = await svc.list_results(db, run.id)
        d = ValidationRunOut.model_validate(run, from_attributes=True).model_dump()
        d["results"] = results
        out.append(d)
    return out


async def _get_run_or_404(db: AsyncSession, run_id: int, user: User):
    run = await svc.get_run(db, run_id, _effective_org_id(user))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Validation run not found")
    return run


@router.get("/runs/{run_id}", response_model=ValidationRunOut)
async def get_run(run_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    run = await _get_run_or_404(db, run_id, user)
    results = await svc.list_results(db, run.id)
    out = ValidationRunOut.model_validate(run, from_attributes=True).model_dump()
    out["results"] = results
    return out


@router.get("/runs/{run_id}/rules/{rule_id}/failed-records", response_model=FailedRecordsOut)
async def get_failed_records(
    run_id: int, rule_id: str, limit: int = 50,
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    run = await _get_run_or_404(db, run_id, user)
    results = await svc.list_results(db, run.id)
    rule_result = next((r for r in results if r.rule_id == rule_id), None)
    if rule_result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule result not found on this run")
    rule_set = await svc.get_rule_set(db, run.rule_set_id, _effective_org_id(user))
    if rule_set is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule set not found")
    try:
        preview = await svc.get_failed_records(db, rule_result=rule_result, rule_set=rule_set, organization_id=_effective_org_id(user), limit=limit)
    except svc.ValidationServiceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return FailedRecordsOut(columns=preview.columns, rows=preview.rows, truncated=preview.truncated)
