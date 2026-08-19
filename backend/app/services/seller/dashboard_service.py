"""
Seller Dashboard — real, cross-organization aggregation. Every number here
comes from an actual query against tables the rest of the platform already
writes to (Organization, Subscription, Document, PipelineRun, ValidationRun,
ReconciliationRun, TargetLoadRun, UserActivityLog); nothing is fabricated.
Where this codebase genuinely doesn't track something yet (customer churn
events, per-module usage credits), the field is reported as 0 or omitted
rather than invented — see the docstrings inline below.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.organization import LicenseStatus, Organization
from app.models.pipeline_run import PipelineRun
from app.models.reconciliation import ReconciliationRun
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.target_load_run import TargetLoadRun
from app.models.user import User
from app.models.user_activity import UserActivityLog
from app.models.validation import ValidationRun

ALL_MODULES = [
    "DOCUMENT_INTELLIGENCE", "OCR", "AI_EXTRACTION", "TRANSFORMATION", "VALIDATION",
    "RECONCILIATION", "SQL_WORKSPACE", "DATA_CATALOG", "LINEAGE", "PIPELINE_SCHEDULER",
    "OBSERVABILITY", "INCIDENTS",
]


async def _count(db: AsyncSession, stmt) -> int:
    result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    return result.scalar_one()


async def _kpis(db: AsyncSession, orgs: list[Organization], subs: list[Subscription]) -> dict:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=30)

    status_counts = Counter(o.license_status for o in orgs)
    sub_status_counts = Counter(s.status for s in subs)

    def _aware(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    expiring_soon = sum(
        1 for s in subs
        if s.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.EXPIRING)
        and _aware(s.end_date) is not None and now <= _aware(s.end_date) <= horizon
    )

    monthly_revenue = 0.0
    for s in subs:
        if s.status != SubscriptionStatus.ACTIVE:
            continue
        if s.billing_cycle.value == "ANNUAL":
            monthly_revenue += (s.amount or 0) / 12
        elif s.billing_cycle.value == "MONTHLY":
            monthly_revenue += s.amount or 0
        # CUSTOM cycle (e.g. trial) contributes 0 — no recurring revenue yet.

    licensed_module_set: set[str] = set()
    for o in orgs:
        licensed_module_set.update(o.licensed_modules or [])

    return {
        "total_customers": len(orgs),
        "active_customers": status_counts.get(LicenseStatus.ACTIVE, 0),
        "trial_customers": status_counts.get(LicenseStatus.TRIAL, 0),
        "suspended_customers": status_counts.get(LicenseStatus.SUSPENDED, 0),
        "expired_customers": status_counts.get(LicenseStatus.EXPIRED, 0),
        "active_subscriptions": sub_status_counts.get(SubscriptionStatus.ACTIVE, 0),
        "expiring_subscriptions": expiring_soon,
        "monthly_revenue": round(monthly_revenue, 2),
        "licensed_modules": len(licensed_module_set),
        "modules_total": len(ALL_MODULES),
    }


async def _platform_usage(db: AsyncSession) -> dict:
    """Cross-org totals — valid here (unlike everywhere else in the app)
    because this is the Seller's own aggregate view across all customers."""
    documents = await _count(db, select(Document))
    ocr_pages = await _count(db, select(Document).where(Document.ocr_percent.isnot(None)))
    ai_requests = await _count(db, select(Document).where(Document.extraction_percent.isnot(None)))
    pipeline_executions = await _count(db, select(PipelineRun))

    validation_records = (await db.execute(
        select(func.coalesce(func.sum(ValidationRun.total_records), 0))
    )).scalar_one()
    reconciliation_records = (await db.execute(
        select(func.coalesce(func.sum(ReconciliationRun.source_count), 0))
    )).scalar_one()
    transformation_records = (await db.execute(
        select(func.coalesce(func.sum(TargetLoadRun.records_written), 0))
    )).scalar_one()
    storage_bytes = (await db.execute(
        select(func.coalesce(func.sum(Document.size_bytes), 0))
    )).scalar_one()

    return {
        "documents_processed": documents,
        "ocr_pages": ocr_pages,  # proxy: documents with OCR applied — this codebase doesn't track per-page counts
        "ai_extraction_requests": ai_requests,
        "ai_credits": None,  # not tracked anywhere yet — reported as untracked rather than fabricated
        "transformation_records": transformation_records,
        "validation_records": validation_records,
        "reconciliation_records": reconciliation_records,
        "pipeline_executions": pipeline_executions,
        "storage_gb": round(storage_bytes / 1_000_000_000, 3),
    }


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


async def _customer_growth(db: AsyncSession, orgs: list[Organization]) -> list[dict]:
    """New customers per month (real, from Organization.created_at) for the
    trailing 6 months. 'Active' = active as-of-now among orgs created by
    that month (best-effort — historical status isn't tracked). 'Churned'
    has no backing event source in this codebase yet, so it's always 0
    rather than invented."""
    now = datetime.now(timezone.utc)
    months = []
    cursor = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for i in range(5, -1, -1):
        # step back i months from the current month, calendar-safe
        year = cursor.year
        month = cursor.month - i
        while month <= 0:
            month += 12
            year -= 1
        months.append(f"{year:04d}-{month:02d}")

    def _created_month(o: Organization) -> str:
        dt = o.created_at if o.created_at.tzinfo else o.created_at.replace(tzinfo=timezone.utc)
        return _month_key(dt)

    new_by_month = Counter(_created_month(o) for o in orgs)
    series = []
    cumulative_active = 0
    for m in months:
        new_count = new_by_month.get(m, 0)
        active_created_by_m = sum(
            1 for o in orgs
            if _created_month(o) <= m and o.license_status not in (LicenseStatus.SUSPENDED, LicenseStatus.EXPIRED)
        )
        series.append({"month": m, "new_customers": new_count, "active_customers": active_created_by_m, "churned_customers": 0})
    return series


def _subscription_overview(subs: list[Subscription]) -> list[dict]:
    counts = Counter(s.status.value for s in subs)
    return [{"status": status.value, "count": counts.get(status.value, 0)} for status in SubscriptionStatus]


def _module_adoption(orgs: list[Organization]) -> list[dict]:
    counts = Counter()
    for o in orgs:
        for m in (o.licensed_modules or []):
            counts[m] += 1
    return sorted(
        [{"module": m, "customers": counts.get(m, 0)} for m in ALL_MODULES],
        key=lambda r: r["customers"], reverse=True,
    )


async def _usage_trend(db: AsyncSession, days: int = 14) -> list[dict]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    async def _daily_counts(model, ts_column) -> dict[str, int]:
        result = await db.execute(
            select(ts_column).where(ts_column >= since)
        )
        counter: dict[str, int] = defaultdict(int)
        for (ts,) in result.all():
            if ts is None:
                continue
            aware = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            counter[aware.strftime("%Y-%m-%d")] += 1
        return counter

    docs_by_day = await _daily_counts(Document, Document.uploaded_at)
    pipelines_by_day = await _daily_counts(PipelineRun, PipelineRun.created_at)
    validations_by_day = await _daily_counts(ValidationRun, ValidationRun.created_at)
    reconciliations_by_day = await _daily_counts(ReconciliationRun, ReconciliationRun.created_at)

    series = []
    for i in range(days - 1, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        series.append({
            "date": day,
            "documents_processed": docs_by_day.get(day, 0),
            "pipeline_executions": pipelines_by_day.get(day, 0),
            "validation_records": validations_by_day.get(day, 0),
            "reconciliation_records": reconciliations_by_day.get(day, 0),
        })
    return series


_ACTIVITY_LABELS = {
    "LOGIN": "Signed in",
    "LOGIN_FAILED": "Failed sign-in attempt",
    "INVITED": "User invited",
    "UPDATED": "Profile updated",
    "ROLE_CHANGED": "Role changed",
    "DISABLED": "User disabled",
    "ENABLED": "User enabled",
    "PASSWORD_RESET": "Password reset",
    "DELETED": "User deleted",
}


async def _recent_activity(db: AsyncSession, limit: int = 10) -> list[dict]:
    """Real cross-org feed from UserActivityLog (the only table currently
    written to on every request that touches a user) joined out to the
    owning Organization. AuditLog (models/audit_log.py) will add
    License/Module-change events starting Phase 4 — record_event() isn't
    called from anywhere yet, so it contributes nothing today rather than
    being faked here."""
    stmt = (
        select(UserActivityLog, User.full_name, User.organization_id, Organization.name)
        .join(User, User.id == UserActivityLog.user_id)
        .outerjoin(Organization, Organization.id == User.organization_id)
        .order_by(UserActivityLog.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()
    items = []
    for log, user_name, org_id, org_name in rows:
        items.append({
            "customer": org_name or "(no organization)",
            "organization_id": org_id,
            "activity": f"{_ACTIVITY_LABELS.get(log.event_type.value, log.event_type.value)} — {user_name}",
            "module": "Account",
            "timestamp": log.created_at if log.created_at.tzinfo else log.created_at.replace(tzinfo=timezone.utc),
            "status": "Failed" if log.event_type.value == "LOGIN_FAILED" else "Success",
        })
    return items


async def build_dashboard(db: AsyncSession) -> dict:
    orgs = (await db.execute(select(Organization))).scalars().all()
    subs = (await db.execute(select(Subscription))).scalars().all()

    kpis = await _kpis(db, orgs, subs)
    usage = await _platform_usage(db)
    customer_growth = await _customer_growth(db, orgs)
    subscription_overview = _subscription_overview(subs)
    module_adoption = _module_adoption(orgs)
    usage_trend = await _usage_trend(db)
    recent_activity = await _recent_activity(db)

    return {
        "kpis": kpis,
        "platform_usage": usage,
        "customer_growth": customer_growth,
        "subscription_overview": subscription_overview,
        "module_adoption": module_adoption,
        "usage_trend": usage_trend,
        "recent_activity": recent_activity,
    }
