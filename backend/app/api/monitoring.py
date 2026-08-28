"""
Monitoring router — /api/monitoring/... Every route is gated with
require_action(..., module="OBSERVABILITY") (server-side — see core/deps.py;
the frontend never being the sole authorization layer is an explicit
requirement) and every list route is paginated. See app/monitoring/
service.py for the real logic; this module only shapes requests/responses.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_action
from app.database.tenant_routing import get_tenant_db
from app.models.user import User, UserRole
from app.monitoring import service as svc
from app.schemas.monitoring import (
    AlertEventListOut,
    AlertRuleCreateRequest,
    AlertRuleListOut,
    AlertRuleOut,
    AlertRuleUpdateRequest,
    AvailableChannelsOut,
    DataQualityOut,
    JobDetailOut,
    OverviewOut,
    PerformanceOut,
    PipelineRunsOut,
    ScheduleRunsOut,
    SlaDefinitionCreateRequest,
    SlaDefinitionListOut,
    SlaDefinitionOut,
    SlaDefinitionUpdateRequest,
    SystemHealthOut,
)

router = APIRouter(prefix="/monitoring", tags=["monitoring"])

_view = require_action("VIEW", module="OBSERVABILITY")
_create = require_action("CREATE", module="OBSERVABILITY")
_edit = require_action("EDIT", module="OBSERVABILITY")
_delete = require_action("DELETE", module="OBSERVABILITY")
_execute = require_action("EXECUTE", module="OBSERVABILITY")


def _org_id(user: User) -> int | None:
    return None if user.role == UserRole.SUPER_ADMIN else user.organization_id


@router.get("/overview", response_model=OverviewOut)
async def overview(days: int = Query(7, ge=1, le=90), user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    return await svc.get_overview(db, _org_id(user), days=days)


@router.get("/pipeline-runs", response_model=PipelineRunsOut)
async def pipeline_runs(
    pipeline_id: int | None = None, status: str | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    return await svc.get_pipeline_runs(db, _org_id(user), pipeline_id=pipeline_id, status=status, page=page, page_size=page_size)


@router.get("/job-runs/{job_id}", response_model=JobDetailOut)
async def job_run_detail(job_id: int, user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    detail = await svc.get_job_detail(db, job_id, _org_id(user))
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job run not found")
    return detail


@router.get("/schedule-runs", response_model=ScheduleRunsOut)
async def schedule_runs(
    schedule_id: int | None = None, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    return await svc.get_schedule_runs(db, _org_id(user), schedule_id=schedule_id, page=page, page_size=page_size)


@router.get("/data-quality", response_model=DataQualityOut)
async def data_quality(
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    return await svc.get_data_quality(db, _org_id(user), page=page, page_size=page_size)


@router.get("/performance", response_model=PerformanceOut)
async def performance(days: int = Query(7, ge=1, le=90), user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    return await svc.get_performance(db, _org_id(user), days=days)


@router.get("/system-health", response_model=SystemHealthOut)
async def system_health(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    return await svc.get_system_health(db)


# ── SLA Definitions ──

@router.get("/sla", response_model=SlaDefinitionListOut)
async def list_sla(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    rows = await svc.list_sla_definitions(db, _org_id(user))
    return SlaDefinitionListOut(items=rows)


@router.post("/sla", response_model=SlaDefinitionOut, status_code=status.HTTP_201_CREATED)
async def create_sla(body: SlaDefinitionCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    try:
        return await svc.create_sla_definition(db, organization_id=_org_id(user), created_by=user.id, body=body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.put("/sla/{sla_id}", response_model=SlaDefinitionOut)
async def update_sla(sla_id: int, body: SlaDefinitionUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    row = await svc.get_sla_definition(db, sla_id, _org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SLA definition not found")
    return await svc.update_sla_definition(db, row, body)


@router.delete("/sla/{sla_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sla(sla_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    row = await svc.get_sla_definition(db, sla_id, _org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SLA definition not found")
    await svc.delete_sla_definition(db, row)


# ── Alert Rules ──

@router.get("/alert-rules", response_model=AlertRuleListOut)
async def list_alert_rules(user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db)):
    rows = await svc.list_alert_rules(db, _org_id(user))
    return AlertRuleListOut(items=rows)


@router.get("/alert-rules/channels", response_model=AvailableChannelsOut)
async def available_channels(user: User = Depends(_view)):
    from app.monitoring import notification_channels
    return AvailableChannelsOut(types=notification_channels.available_channel_types())


@router.post("/alert-rules", response_model=AlertRuleOut, status_code=status.HTTP_201_CREATED)
async def create_alert_rule(body: AlertRuleCreateRequest, user: User = Depends(_create), db: AsyncSession = Depends(get_tenant_db)):
    return await svc.create_alert_rule(db, organization_id=_org_id(user), created_by=user.id, body=body)


@router.put("/alert-rules/{rule_id}", response_model=AlertRuleOut)
async def update_alert_rule(rule_id: int, body: AlertRuleUpdateRequest, user: User = Depends(_edit), db: AsyncSession = Depends(get_tenant_db)):
    row = await svc.get_alert_rule(db, rule_id, _org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert rule not found")
    return await svc.update_alert_rule(db, row, body)


@router.delete("/alert-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert_rule(rule_id: int, user: User = Depends(_delete), db: AsyncSession = Depends(get_tenant_db)):
    row = await svc.get_alert_rule(db, rule_id, _org_id(user))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert rule not found")
    await svc.delete_alert_rule(db, row)


# ── Alert Events (Alerts / Incidents tab) ──

@router.get("/alert-events", response_model=AlertEventListOut)
async def list_alert_events(
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
    user: User = Depends(_view), db: AsyncSession = Depends(get_tenant_db),
):
    return await svc.list_alert_events(db, _org_id(user), status=status_filter, page=page, page_size=page_size)


@router.post("/alert-events/{event_id}/acknowledge", response_model=None)
async def acknowledge_event(event_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    event = await svc.get_alert_event(db, event_id, _org_id(user))
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert event not found")
    row = await svc.acknowledge_alert_event(db, event, user.id)
    from app.schemas.monitoring import AlertEventOut
    return AlertEventOut.model_validate(row)


@router.post("/alert-events/{event_id}/resolve", response_model=None)
async def resolve_event(event_id: int, user: User = Depends(_execute), db: AsyncSession = Depends(get_tenant_db)):
    event = await svc.get_alert_event(db, event_id, _org_id(user))
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert event not found")
    row = await svc.resolve_alert_event(db, event, user.id)
    from app.schemas.monitoring import AlertEventOut
    return AlertEventOut.model_validate(row)
