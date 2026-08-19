from pydantic import BaseModel


class DashboardMetrics(BaseModel):
    documents_processed: int
    documents_processed_trend_pct: float | None  # real day-over-day % change; None if no comparable data
    ocr_success_rate: float | None  # avg OCR confidence across documents that have gone through OCR
    validation_pass_rate: float | None  # avg rule pass rate across all validation runs
    active_incidents: int
    pipeline_runs_today: int
    pipeline_runs_today_trend_pct: float | None
    pipeline_success_rate: float | None  # % SUCCEEDED among terminal pipeline jobs


class RecentRunOut(BaseModel):
    job_id: int
    pipeline_name: str
    status: str
    records_out: int | None
    duration_sec: float | None
    trigger: str
    started_at: str | None


class DashboardOut(BaseModel):
    metrics: DashboardMetrics
    recent_runs: list[RecentRunOut]
