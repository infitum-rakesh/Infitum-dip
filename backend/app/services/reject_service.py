"""
reject_service — persists and lists RejectedRecord rows (KK2/KK4). The
capture side (write_batch_with_fallback()'s per-row failures) lives in
loading/writers/base.py; this module is just the DB read/write layer both
execution/load_worker.py and execution/pipeline_job_executor.py call into
after a batch write comes back with rejects.
"""
from __future__ import annotations

import decimal
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal
from app.models.rejected_record import RejectedRecord, RejectSourceType


def _json_safe(row: dict) -> dict:
    """Row dicts here can come straight off a pandas DataFrame or a raw
    DB-driver cursor (datetime, date, Decimal, bytes, ...) — none of which
    the JSON column can serialize as-is. Real values are preserved as-is
    where JSON already supports them; anything else becomes its str()
    form rather than raising or silently dropping the field, so the
    stored row is still a faithful, readable record of what was rejected."""
    out = {}
    for k, v in row.items():
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, (datetime, date, decimal.Decimal)):
            out[k] = str(v)
        elif isinstance(v, bytes):
            out[k] = f"<{len(v)} bytes>"
        else:
            out[k] = str(v)
    return out


async def record_rejects(
    *, organization_id: int | None, source_type: RejectSourceType, source_id: int,
    node_id: str | None, target_object: str | None, rejects: list[dict],
) -> None:
    """rejects is [{"row": dict, "error": str}, ...] — write_batch_with_fallback()'s
    own return shape. No-ops on an empty list (the common case — most
    batches have zero rejects) without opening a DB session at all."""
    if not rejects:
        return
    async with AsyncSessionLocal() as db:
        db.add_all([
            RejectedRecord(
                organization_id=organization_id, source_type=source_type, source_id=source_id,
                node_id=node_id, target_object=target_object,
                row_data=_json_safe(r["row"]), error_message=str(r["error"])[:2000],
            )
            for r in rejects
        ])
        await db.commit()


async def list_rejects(
    db: AsyncSession, *, source_type: RejectSourceType, source_id: int, organization_id: int | None,
    node_id: str | None = None, limit: int = 200, offset: int = 0,
) -> tuple[list[RejectedRecord], int]:
    stmt = select(RejectedRecord).where(RejectedRecord.source_type == source_type, RejectedRecord.source_id == source_id)
    count_stmt = select(func.count()).select_from(RejectedRecord).where(
        RejectedRecord.source_type == source_type, RejectedRecord.source_id == source_id
    )
    if organization_id is not None:
        stmt = stmt.where(RejectedRecord.organization_id == organization_id)
        count_stmt = count_stmt.where(RejectedRecord.organization_id == organization_id)
    if node_id is not None:
        # PIPELINE_JOB only — narrows to the one target node's rejects when
        # a job has more than one target and the caller only wants this one.
        stmt = stmt.where(RejectedRecord.node_id == node_id)
        count_stmt = count_stmt.where(RejectedRecord.node_id == node_id)
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(RejectedRecord.id).offset(offset).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all()), total
