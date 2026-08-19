"""
Seller Portal -> Usage (Phase 6) — platform-wide usage monitoring. Reuses
dashboard_service's already-built _platform_usage() for the global KPI row
(same real Document/PipelineRun/ValidationRun/ReconciliationRun/
TargetLoadRun queries, unscoped) and extends the same query shapes
per-organization for the Usage Table / charts / drilldown.

Real per-module usage only exists for modules this codebase actually
counts something for (see _MODULE_USAGE_QUERY below) — DATA_CATALOG,
SQL_WORKSPACE, LINEAGE, OBSERVABILITY, INCIDENTS have no real usage
counter anywhere in the platform yet, so their usage is reported as None
(untracked) rather than fabricated, same convention as
dashboard_service's ai_credits.

Environment filter (spec section 4 "FILTERS"): NOT implemented as a real
filter. Grepping this codebase, only Connection/Incident/Schedule carry
an `environment` column — Document/PipelineRun/ValidationRun/
ReconciliationRun/TargetLoadRun (everything usage is actually measured
from) do not. Wiring a filter that silently does nothing would be worse
than omitting it, so the API simply doesn't accept one; see Seller
Portal memory / Phase 6 summary for the disclosure to the user.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer_module import CustomerModule
from app.models.document import Document
from app.models.organization import Organization
from app.models.pipeline_run import PipelineRun
from app.models.reconciliation import ReconciliationRun
from app.models.target_load_run import TargetLoadRun
from app.models.validation import ValidationRun
from app.services.seller.dashboard_service import _platform_usage
from app.services.seller.module_service import MODULE_CATALOG

# module -> async fn(db, organization_id) -> int | None. None entries
# (not present here) fall through to "untracked" in _real_usage_by_module.
async def _doc_intel_usage(db, org_id):
    return (await db.execute(select(func.count()).select_from(select(Document).where(Document.organization_id == org_id).subquery()))).scalar_one()


async def _ocr_usage(db, org_id):
    return (await db.execute(select(func.count()).select_from(select(Document).where(Document.organization_id == org_id, Document.ocr_percent.isnot(None)).subquery()))).scalar_one()


async def _ai_extraction_usage(db, org_id):
    return (await db.execute(select(func.count()).select_from(select(Document).where(Document.organization_id == org_id, Document.extraction_percent.isnot(None)).subquery()))).scalar_one()


async def _transformation_usage(db, org_id):
    return (await db.execute(select(func.coalesce(func.sum(TargetLoadRun.records_written), 0)).where(TargetLoadRun.organization_id == org_id))).scalar_one()


async def _validation_usage(db, org_id):
    return (await db.execute(select(func.coalesce(func.sum(ValidationRun.total_records), 0)).where(ValidationRun.organization_id == org_id))).scalar_one()


async def _reconciliation_usage(db, org_id):
    return (await db.execute(select(func.coalesce(func.sum(ReconciliationRun.source_count), 0)).where(ReconciliationRun.organization_id == org_id))).scalar_one()


async def _pipeline_usage(db, org_id):
    return (await db.execute(select(func.count()).select_from(select(PipelineRun).where(PipelineRun.organization_id == org_id).subquery()))).scalar_one()


_MODULE_USAGE_FN = {
    "DOCUMENT_INTELLIGENCE": _doc_intel_usage,
    "OCR": _ocr_usage,
    "AI_EXTRACTION": _ai_extraction_usage,
    "TRANSFORMATION": _transformation_usage,
    "VALIDATION": _validation_usage,
    "RECONCILIATION": _reconciliation_usage,
    "PIPELINE_SCHEDULER": _pipeline_usage,
}


async def _real_usage_by_module(db: AsyncSession, organization_id: int) -> dict[str, int | None]:
    result = {}
    for module in MODULE_CATALOG:
        fn = _MODULE_USAGE_FN.get(module)
        result[module] = await fn(db, organization_id) if fn else None
    return result


def _limit_for(row: CustomerModule | None, module: str) -> int | None:
    if row is None:
        return None
    if module == "AI_EXTRACTION":
        return row.monthly_credits
    if module in ("DOCUMENT_INTELLIGENCE", "OCR"):
        return row.max_documents
    if module in ("VALIDATION", "RECONCILIATION"):
        return row.max_records if row.max_records is not None else row.max_documents
    return row.monthly_credits if row.monthly_credits is not None else row.max_documents


def _status_for(percentage: float | None) -> str:
    if percentage is None:
        return "HEALTHY"  # unlimited — never breaches
    if percentage >= 100:
        return "EXCEEDED"
    if percentage >= 90:
        return "NEAR_LIMIT"
    if percentage >= 70:
        return "WARNING"
    return "HEALTHY"


async def get_platform_kpis(db: AsyncSession) -> dict:
    return await _platform_usage(db)


async def get_usage_table(
    db: AsyncSession, *, customer_id: int | None = None, module: str | None = None, plan: str | None = None,
) -> list[dict]:
    stmt = select(Organization)
    if customer_id is not None:
        stmt = stmt.where(Organization.id == customer_id)
    if plan:
        stmt = stmt.where(Organization.plan == plan)
    orgs = (await db.execute(stmt)).scalars().all()

    cm_result = await db.execute(select(CustomerModule))
    cm_by_org_module: dict[tuple[int, str], CustomerModule] = {(r.organization_id, r.module): r for r in cm_result.scalars().all()}

    period = datetime.now(timezone.utc).strftime("%B %Y")
    items = []
    for org in orgs:
        usage_by_module = await _real_usage_by_module(db, org.id)
        for mod, usage in usage_by_module.items():
            if module and mod != module:
                continue
            if usage is None:
                continue  # untracked module — nothing real to show as a row
            row = cm_by_org_module.get((org.id, mod))
            limit = _limit_for(row, mod)
            remaining = max(0, limit - usage) if limit is not None else None
            percentage = round((usage / limit) * 100, 1) if limit else None
            items.append({
                "organization_id": org.id, "organization_name": org.name,
                "module": mod, "module_label": MODULE_CATALOG[mod]["label"],
                "usage": usage, "limit": limit, "remaining": remaining, "percentage": percentage,
                "period": period, "status": _status_for(percentage),
            })
    return items


async def get_charts(db: AsyncSession) -> dict:
    table = await get_usage_table(db)

    by_customer: dict[str, int] = defaultdict(int)
    by_module: dict[str, int] = defaultdict(int)
    for row in table:
        by_customer[row["organization_name"]] += row["usage"]
        by_module[row["module_label"]] += row["usage"]

    usage_by_customer = sorted([{"customer": k, "usage": v} for k, v in by_customer.items()], key=lambda r: r["usage"], reverse=True)
    usage_by_module = sorted([{"module": k, "usage": v} for k, v in by_module.items()], key=lambda r: r["usage"], reverse=True)
    top_consumers = usage_by_customer[:10]
    quota_breaches = [r for r in table if r["status"] == "EXCEEDED"]

    now = datetime.now(timezone.utc)
    daily = []
    for i in range(13, -1, -1):
        day_start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        docs = (await db.execute(select(func.count()).select_from(
            select(Document).where(Document.uploaded_at >= day_start, Document.uploaded_at < day_end).subquery()
        ))).scalar_one()
        runs = (await db.execute(select(func.count()).select_from(
            select(PipelineRun).where(PipelineRun.created_at >= day_start, PipelineRun.created_at < day_end).subquery()
        ))).scalar_one()
        daily.append({"date": day_start.strftime("%Y-%m-%d"), "documents": docs, "pipeline_executions": runs})

    months = []
    cursor = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for i in range(5, -1, -1):
        year, mon = cursor.year, cursor.month - i
        while mon <= 0:
            mon += 12
            year -= 1
        months.append((year, mon))
    monthly_series = []
    for year, mon in months:
        start = datetime(year, mon, 1, tzinfo=timezone.utc)
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if mon == 12 else datetime(year, mon + 1, 1, tzinfo=timezone.utc)
        docs = (await db.execute(select(func.count()).select_from(
            select(Document).where(Document.uploaded_at >= start, Document.uploaded_at < end).subquery()
        ))).scalar_one()
        monthly_series.append({"month": f"{year:04d}-{mon:02d}", "documents": docs})

    return {
        "usage_by_customer": usage_by_customer, "usage_by_module": usage_by_module,
        "daily_usage": daily, "monthly_usage": monthly_series,
        "top_consumers": top_consumers, "quota_breaches": quota_breaches,
    }


async def get_customer_usage(db: AsyncSession, organization_id: int) -> dict:
    org = await db.get(Organization, organization_id)
    if org is None:
        return None
    modules = await get_usage_table(db, customer_id=organization_id)

    now = datetime.now(timezone.utc)
    daily = []
    for i in range(13, -1, -1):
        day_start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        docs = (await db.execute(select(func.count()).select_from(
            select(Document).where(Document.organization_id == organization_id, Document.uploaded_at >= day_start, Document.uploaded_at < day_end).subquery()
        ))).scalar_one()
        daily.append({"date": day_start.strftime("%Y-%m-%d"), "documents": docs})

    return {"organization_id": org.id, "organization_name": org.name, "modules": modules, "daily_usage": daily}
