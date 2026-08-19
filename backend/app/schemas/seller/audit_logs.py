"""Pydantic models for /api/seller/audit-logs/* (Phase 8). Reads the
single SellerAuditLog table every prior Seller Portal phase already
writes to — no new event source, no new writes."""
from datetime import datetime

from pydantic import BaseModel


class SellerAuditEntryOut(BaseModel):
    id: int
    timestamp: datetime
    seller_user_id: int | None
    seller_user_name: str | None
    customer_id: int | None
    customer_name: str | None
    action: str
    module: str | None
    module_label: str | None
    resource: str | None
    resource_id: str | None
    ip_address: str | None
    details: str | None


class SellerAuditListOut(BaseModel):
    items: list[SellerAuditEntryOut]
    total: int
    page: int
    page_size: int
    actions: list[str]        # SELLER_AUDIT_ACTIONS, for the Action filter dropdown
    modules: list[dict]        # [{"value": "OCR", "label": "OCR"}, ...], for the Module filter dropdown


class SellerAuditDetailOut(SellerAuditEntryOut):
    before_value: dict | None
    after_value: dict | None


class SellerAuditStatsOut(BaseModel):
    total: int
    today: int
    this_week: int
    customers_touched: int
