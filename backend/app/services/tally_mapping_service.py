"""
tally_mapping_service.py — canonical ApInvoice field -> Tally voucher
slot mapping (TALLY-D). Same versioned-row convention as
invoice_erp_mapping_service.py: get_current_mapping/seed_default_mapping/
publish_mapping, `is_current` flips on publish, never mutated in place.

Default mapping targets a vanilla TallyPrime install's default ledger
names ("Purchase Accounts", "CGST", ...) — a real install almost always
renames or reorganizes these, which is exactly why this is a real,
editable, versioned mapping rather than a hardcoded constant.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tally_mapping import TallyAccountMapping

_DEFAULT_MAPPING = [
    {"canonical_field": "invoice_number", "tally_field": "narration", "transform_config": {"type": "passthrough"}},
    # Kept as the raw datetime (passthrough), not pre-formatted — the
    # builder stores it as a real date on TallyVoucherPreview.voucher_date
    # and only formats to Tally's YYYYMMDD wire form when serializing XML.
    {"canonical_field": "invoice_date", "tally_field": "voucher_date", "transform_config": {"type": "passthrough", "required": True}},
    {"canonical_field": "vendor_name", "tally_field": "party_ledger", "transform_config": {"type": "passthrough", "required": True}},
    # ApInvoice.voucher_type is an enum (PURCHASE/CREDIT_NOTE/DEBIT_NOTE/
    # PAYMENT) — this maps it to the real Tally voucher TYPE NAME, which a
    # real install may have renamed (hence a real, editable lookup here,
    # not a hardcoded str(enum)).
    {
        "canonical_field": "voucher_type", "tally_field": "voucher_type_name",
        "transform_config": {
            "type": "voucher_type_lookup", "required": True,
            "map": {"PURCHASE": "Purchase", "CREDIT_NOTE": "Credit Note", "DEBIT_NOTE": "Debit Note", "PAYMENT": "Payment"},
        },
    },
    {"canonical_field": "__fixed__purchase_ledger", "tally_field": "purchase_ledger_name", "default_value": "Purchase Accounts", "transform_config": {"type": "fixed", "required": True}},
    {"canonical_field": "__fixed__cgst_ledger", "tally_field": "cgst_ledger_name", "default_value": "CGST", "transform_config": {"type": "fixed"}},
    {"canonical_field": "__fixed__sgst_ledger", "tally_field": "sgst_ledger_name", "default_value": "SGST", "transform_config": {"type": "fixed"}},
    {"canonical_field": "__fixed__igst_ledger", "tally_field": "igst_ledger_name", "default_value": "IGST", "transform_config": {"type": "fixed"}},
    {"canonical_field": "__fixed__cess_ledger", "tally_field": "cess_ledger_name", "default_value": "Cess", "transform_config": {"type": "fixed"}},
    {"canonical_field": "__fixed__tds_ledger", "tally_field": "tds_ledger_name", "default_value": "TDS Payable", "transform_config": {"type": "fixed"}},
]


class TallyMappingError(Exception):
    pass


async def get_current_mapping(db: AsyncSession, organization_id: int | None, connection_id: int) -> list[TallyAccountMapping]:
    result = await db.execute(
        select(TallyAccountMapping).where(
            TallyAccountMapping.organization_id == organization_id,
            TallyAccountMapping.connection_id == connection_id,
            TallyAccountMapping.is_current.is_(True),
        ).order_by(TallyAccountMapping.canonical_field.asc())
    )
    return list(result.scalars().all())


async def seed_default_mapping(db: AsyncSession, organization_id: int | None, connection_id: int) -> list[TallyAccountMapping]:
    """Idempotent — a no-op if this (org, connection) already has a
    current mapping (seeded or since edited/published)."""
    existing = await get_current_mapping(db, organization_id, connection_id)
    if existing:
        return existing
    rows = [
        TallyAccountMapping(organization_id=organization_id, connection_id=connection_id, version=1, is_current=True, **m)
        for m in _DEFAULT_MAPPING
    ]
    db.add_all(rows)
    await db.commit()
    for r in rows:
        await db.refresh(r)
    return rows


async def publish_mapping(
    db: AsyncSession, organization_id: int | None, connection_id: int, rows_in: list[dict], created_by: int | None,
) -> list[TallyAccountMapping]:
    """Publishes a brand-new version: current rows flip to is_current=False,
    a fresh batch is inserted at version = max+1. Never mutates a
    published row in place — a TallyVoucherPreview keeps its
    mapping_version, so a later mapping edit never silently changes what
    a past preview's build meant."""
    if not rows_in:
        raise TallyMappingError("A mapping must have at least one field.")

    current = await get_current_mapping(db, organization_id, connection_id)
    next_version = (max((r.version for r in current), default=0)) + 1
    for r in current:
        r.is_current = False

    new_rows = [
        TallyAccountMapping(
            organization_id=organization_id, connection_id=connection_id, version=next_version, is_current=True,
            canonical_field=r["canonical_field"], tally_field=r["tally_field"], is_line_mapping=r.get("is_line_mapping", False),
            default_value=r.get("default_value"), transform_config=r.get("transform_config", {}), created_by=created_by,
        )
        for r in rows_in
    ]
    db.add_all(new_rows)
    await db.commit()
    for r in new_rows:
        await db.refresh(r)
    return new_rows


def apply_transform(raw, transform_config: dict, default_value: str | None):
    ttype = (transform_config or {}).get("type", "passthrough")
    if ttype == "fixed":
        return default_value
    if ttype == "voucher_type_lookup":
        key = raw.value if hasattr(raw, "value") else raw
        return (transform_config.get("map") or {}).get(str(key), default_value)
    if raw in (None, ""):
        return default_value
    if ttype == "date_format" and isinstance(raw, datetime):
        return raw.strftime(transform_config.get("format", "%Y-%m-%d"))
    return raw
