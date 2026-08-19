from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Responses ────────────────────────────────────────────────────

class ScheduleOut(BaseModel):
    id: int
    name: str
    pipeline_id: str
    pipeline_name: str
    environment: str
    timezone: str
    frequency: str
    frequency_display: str  # e.g. "Every Day 02:00 AM" — computed, matches spec's example row
    start_at: datetime
    cron_expression: str | None
    status: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    retry_count: int
    retry_interval_seconds: int
    timeout_seconds: int
    concurrency: int
    notify_on_failure: bool
    notify_on_success: bool
    notification_emails: str | None
    owner_id: int | None
    owner_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ScheduleListOut(BaseModel):
    items: list[ScheduleOut]
    total: int
    page: int
    page_size: int


class ScheduleStatsOut(BaseModel):
    active: int
    paused: int
    running_today: int
    failed_today: int
    next_run_at: datetime | None
    next_run_schedule_name: str | None


class RunLogEntry(BaseModel):
    timestamp: datetime
    level: str
    message: str


class ScheduleRunOut(BaseModel):
    id: int
    schedule_id: int
    status: str
    trigger: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None
    records_processed: int
    records_failed: int
    error_message: str | None

    model_config = {"from_attributes": True}


class ScheduleRunDetailOut(ScheduleRunOut):
    logs: list[RunLogEntry]


class ScheduleRunListOut(BaseModel):
    items: list[ScheduleRunOut]
    total: int


# ── Requests ─────────────────────────────────────────────────────

class ScheduleCreateRequest(BaseModel):
    name: str
    pipeline_id: str
    pipeline_name: str
    environment: str = "DEVELOPMENT"
    timezone: str = "UTC"
    frequency: str
    start_date: str  # "YYYY-MM-DD"
    start_time: str  # "HH:MM"
    cron_expression: str | None = None
    retry_count: int = 0
    retry_interval_seconds: int = 60
    timeout_seconds: int = 3600
    concurrency: int = 1
    notify_on_failure: bool = True
    notify_on_success: bool = False
    notification_emails: str | None = None


class ScheduleUpdateRequest(BaseModel):
    name: str | None = None
    pipeline_id: str | None = None
    pipeline_name: str | None = None
    environment: str | None = None
    timezone: str | None = None
    frequency: str | None = None
    start_date: str | None = None
    start_time: str | None = None
    cron_expression: str | None = None
    retry_count: int | None = None
    retry_interval_seconds: int | None = None
    timeout_seconds: int | None = None
    concurrency: int | None = None
    notify_on_failure: bool | None = None
    notify_on_success: bool | None = None
    notification_emails: str | None = None
