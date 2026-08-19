"""Pydantic response models for GET /api/seller/dashboard."""
from datetime import datetime

from pydantic import BaseModel


class SellerKpis(BaseModel):
    total_customers: int
    active_customers: int
    trial_customers: int
    suspended_customers: int
    expired_customers: int
    active_subscriptions: int
    expiring_subscriptions: int
    monthly_revenue: float
    licensed_modules: int
    modules_total: int


class PlatformUsage(BaseModel):
    documents_processed: int
    ocr_pages: int
    ai_extraction_requests: int
    ai_credits: int | None
    transformation_records: int
    validation_records: int
    reconciliation_records: int
    pipeline_executions: int
    storage_gb: float


class CustomerGrowthPoint(BaseModel):
    month: str
    new_customers: int
    active_customers: int
    churned_customers: int


class SubscriptionOverviewPoint(BaseModel):
    status: str
    count: int


class ModuleAdoptionPoint(BaseModel):
    module: str
    customers: int


class UsageTrendPoint(BaseModel):
    date: str
    documents_processed: int
    pipeline_executions: int
    validation_records: int
    reconciliation_records: int


class RecentActivityItem(BaseModel):
    customer: str
    organization_id: int | None
    activity: str
    module: str
    timestamp: datetime
    status: str


class SellerDashboardOut(BaseModel):
    kpis: SellerKpis
    platform_usage: PlatformUsage
    customer_growth: list[CustomerGrowthPoint]
    subscription_overview: list[SubscriptionOverviewPoint]
    module_adoption: list[ModuleAdoptionPoint]
    usage_trend: list[UsageTrendPoint]
    recent_activity: list[RecentActivityItem]
