"""Pydantic models for /api/seller/usage/* (Phase 6)."""
from pydantic import BaseModel


class PlatformUsageKpis(BaseModel):
    documents_processed: int
    ocr_pages: int
    ai_extraction_requests: int
    ai_credits: int | None
    transformation_records: int
    validation_records: int
    reconciliation_records: int
    pipeline_executions: int
    storage_gb: float


class UsageTableRow(BaseModel):
    organization_id: int
    organization_name: str
    module: str
    module_label: str
    usage: int
    limit: int | None
    remaining: int | None
    percentage: float | None
    period: str
    status: str


class UsageTableOut(BaseModel):
    items: list[UsageTableRow]


class UsageByCustomerPoint(BaseModel):
    customer: str
    usage: int


class UsageByModulePoint(BaseModel):
    module: str
    usage: int


class DailyUsagePoint(BaseModel):
    date: str
    documents: int
    pipeline_executions: int | None = None


class MonthlyUsagePoint(BaseModel):
    month: str
    documents: int


class UsageChartsOut(BaseModel):
    usage_by_customer: list[UsageByCustomerPoint]
    usage_by_module: list[UsageByModulePoint]
    daily_usage: list[DailyUsagePoint]
    monthly_usage: list[MonthlyUsagePoint]
    top_consumers: list[UsageByCustomerPoint]
    quota_breaches: list[UsageTableRow]


class CustomerDailyUsagePoint(BaseModel):
    date: str
    documents: int


class CustomerUsageOut(BaseModel):
    organization_id: int
    organization_name: str
    modules: list[UsageTableRow]
    daily_usage: list[CustomerDailyUsagePoint]
