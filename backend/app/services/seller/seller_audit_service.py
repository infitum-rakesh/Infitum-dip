"""
Seller Portal -> Seller Audit Logs service (Phase 8) — a real, read-only
filter/search/export UI over the SellerAuditLog table every prior Seller
Portal phase already writes to (Customers, Subscriptions, Purchased
Modules, License Management, Seller Users). No new event source and no
new writes — same "don't build a second source of truth" principle as
Administration -> Audit Logs' audit_log_service.py, just simpler here
since there's exactly one real source table instead of four.

No `status` field: unlike the org-level AuditLog (which distinguishes
SUCCESS/FAILURE for things like failed logins), SellerAuditLog only ever
records completed mutations — every service call that reaches
record_seller_event() already succeeded (validation errors raise before
it's called and are never persisted). Inventing a constant "SUCCESS"
status column would be decoration, not information, so this deliberately
has none — disclosed here rather than silently copying the org-level
shape.

Secrets are masked defensively on every field this service returns (see
_mask_*), mirroring audit_log_service.py's approach — required by the
"Never expose secrets" spec line regardless of whether any Seller Portal
event has ever actually contained one.
"""
import csv
import io
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.seller_audit_log import SELLER_AUDIT_ACTIONS, SellerAuditLog
from app.models.user import User
from app.services.seller.module_service import MODULE_CATALOG

_MASK = "••••••"
_SENSITIVE_KEY_RE = re.compile(r"(password|token|secret|credential|api[_-]?key)", re.IGNORECASE)
_SENSITIVE_TEXT_RE = re.compile(r"((?:password|token|secret|api[_-]?key)\s*[:=]\s*)(\S+)", re.IGNORECASE)


def _mask_text(text: str | None) -> str | None:
    if not text:
        return text
    return _SENSITIVE_TEXT_RE.sub(lambda m: m.group(1) + _MASK, text)


def _mask_value(value):
    if isinstance(value, dict):
        return {k: (_MASK if _SENSITIVE_KEY_RE.search(k) else _mask_value(v)) for k, v in value.items()}
    if isinstance(value, str):
        return _mask_text(value)
    return value


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _users_by_id(db: AsyncSession, ids: set[int]) -> dict[int, User]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    result = await db.execute(select(User).where(User.id.in_(ids)))
    return {u.id: u for u in result.scalars().all()}


async def _orgs_by_id(db: AsyncSession, ids: set[int]) -> dict[int, Organization]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    result = await db.execute(select(Organization).where(Organization.id.in_(ids)))
    return {o.id: o for o in result.scalars().all()}


async def _fetch_entries(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(SellerAuditLog).order_by(SellerAuditLog.timestamp.desc()))).scalars().all()
    users = await _users_by_id(db, {r.seller_user_id for r in rows if r.seller_user_id})
    orgs = await _orgs_by_id(db, {r.customer_id for r in rows if r.customer_id})

    entries = []
    for r in rows:
        actor = users.get(r.seller_user_id)
        org = orgs.get(r.customer_id)
        entries.append({
            "id": r.id, "timestamp": _as_utc(r.timestamp),
            "seller_user_id": r.seller_user_id, "seller_user_name": actor.full_name if actor else None,
            "customer_id": r.customer_id, "customer_name": org.name if org else None,
            "action": r.action, "module": r.module,
            "module_label": MODULE_CATALOG.get(r.module, {}).get("label") if r.module else None,
            "resource": r.resource, "resource_id": r.resource_id,
            "ip_address": r.ip_address, "details": r.details,
            "before_value": r.before_value, "after_value": r.after_value,
        })
    return entries


def _apply_filters(
    entries: list[dict], *, seller_user: str | None, customer: str | None, action: str | None,
    module: str | None, date_from: datetime | None, date_to: datetime | None, search: str | None,
) -> list[dict]:
    def keep(e):
        if seller_user and (not e["seller_user_name"] or seller_user.lower() not in e["seller_user_name"].lower()):
            return False
        if customer and (not e["customer_name"] or customer.lower() not in e["customer_name"].lower()):
            return False
        if action and e["action"] != action:
            return False
        if module and e["module"] != module:
            return False
        if date_from and e["timestamp"] < date_from:
            return False
        if date_to and e["timestamp"] > date_to:
            return False
        if search:
            haystack = " ".join(str(e.get(k) or "") for k in
                                 ("seller_user_name", "customer_name", "action", "module_label", "resource", "details")).lower()
            if search.lower() not in haystack:
                return False
        return True
    return [e for e in entries if keep(e)]


def _shape_summary(e: dict) -> dict:
    return {k: (_mask_text(v) if k == "details" else v) for k, v in e.items() if k not in ("before_value", "after_value")}


def _shape_detail(e: dict) -> dict:
    return {**_shape_summary(e), "before_value": _mask_value(e["before_value"]), "after_value": _mask_value(e["after_value"])}


def _module_options() -> list[dict]:
    return [{"value": k, "label": v["label"]} for k, v in MODULE_CATALOG.items()]


async def get_stats(db: AsyncSession) -> dict:
    entries = await _fetch_entries(db)
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    return {
        "total": len(entries),
        "today": sum(1 for e in entries if e["timestamp"] >= today_start),
        "this_week": sum(1 for e in entries if e["timestamp"] >= week_start),
        "customers_touched": len({e["customer_id"] for e in entries if e["customer_id"]}),
    }


async def list_entries(
    db: AsyncSession, *, seller_user: str | None = None, customer: str | None = None, action: str | None = None,
    module: str | None = None, date_from: datetime | None = None, date_to: datetime | None = None,
    search: str | None = None, page: int = 1, page_size: int = 20,
) -> tuple[list[dict], int]:
    entries = await _fetch_entries(db)
    entries = _apply_filters(entries, seller_user=seller_user, customer=customer, action=action, module=module, date_from=date_from, date_to=date_to, search=search)
    total = len(entries)
    start = (page - 1) * page_size
    return [_shape_summary(e) for e in entries[start:start + page_size]], total


async def get_entry(db: AsyncSession, entry_id: int) -> dict | None:
    entries = await _fetch_entries(db)
    for e in entries:
        if e["id"] == entry_id:
            return _shape_detail(e)
    return None


async def export_entries(db: AsyncSession, *, fmt: str, **filters) -> tuple[bytes, str, str]:
    """Returns (content, media_type, filename)."""
    entries = await _fetch_entries(db)
    entries = _apply_filters(entries, **filters)
    rows = [_shape_summary(e) for e in entries]

    headers = ["Timestamp", "Seller User", "Customer", "Action", "Module", "Resource", "IP Address", "Details"]

    def row_values(r):
        return [
            r["timestamp"].isoformat(), r["seller_user_name"] or "System", r["customer_name"] or "",
            r["action"], r["module_label"] or "", r["resource"] or "", r["ip_address"] or "", r["details"] or "",
        ]

    if fmt == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Seller Audit Log"
        ws.append(headers)
        for r in rows:
            ws.append(row_values(r))
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "seller-audit-log.xlsx"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    for r in rows:
        writer.writerow(row_values(r))
    return buf.getvalue().encode("utf-8"), "text/csv", "seller-audit-log.csv"


ACTIONS = SELLER_AUDIT_ACTIONS
MODULES = _module_options()
