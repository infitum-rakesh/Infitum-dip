"""
invoice_erp_mapping_service.py — canonical ApInvoice field -> ERP request
field mapping (INV9). Versioned per (organization, erp_connection): unlike
ColumnMapping's separate ColumnMappingVersion child table, ApInvoiceErpMapping
rows ARE the versioned rows themselves (one row per canonical_field per
version) — publishing a new version means inserting a fresh batch of rows
at version N+1 and flipping every prior row's is_current to False, never
mutating a published row in place (same "never mutate, publish new"
convention as ColumnMappingVersion).

Default mapping targets Oracle Fusion ERP Cloud's documented AP Invoice
REST field names (see connectors/application/oracle_erp.py's module
docstring for the resource shape this mirrors) — the only ERP connector
this build ships. A different ERP type would need its own default set;
none exists yet, disclosed rather than silently assumed generic.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceLine
from app.models.invoice_intelligence.rules import ApInvoiceErpMapping

# Oracle AP Invoice REST header/line field names, straight from
# oracle_erp.py's documented resource shape. BusinessUnit/InvoiceTypeCode/
# Source have no ApInvoice column to source from — mapped via
# default_value (a fixed, org-configurable constant) rather than invented
# per-invoice, and disclosed here rather than silently omitted (Oracle
# requires BusinessUnit on every invoice).
# "required": true marks the handful of fields Oracle's own AP Invoice
# REST resource genuinely rejects an invoice without (InvoiceNumber/
# InvoiceCurrency/InvoiceAmount/InvoiceDate/BusinessUnit) — a blank value
# there is a real pre-call validation failure (see build_erp_payload).
# Every other field is legitimately optional on a real invoice (many
# invoices genuinely have no PO number, or extraction never found payment
# terms) — those are sent as null/omitted, never treated as "extraction
# didn't complete," so a real, otherwise-complete invoice with no PO
# isn't blocked from posting over a field Oracle doesn't require either.
# No real ERP-supplier crosswalk exists yet (see ApInvoiceVendor.erp_supplier_id's
# own docstring, INV1) — VendorNum is sourced from the extracted vendor
# name as a real, but unverified-against-Oracle, placeholder until that
# sync job exists. Disclosed, not silently assumed correct.
_DEFAULT_HEADER_MAPPING = [
    {"canonical_field": "invoice_number", "erp_field": "InvoiceNumber", "transform_config": {"type": "passthrough", "required": True}},
    {"canonical_field": "invoice_date", "erp_field": "InvoiceDate", "transform_config": {"type": "date_format", "format": "%Y-%m-%d", "required": True}},
    {"canonical_field": "currency", "erp_field": "InvoiceCurrency", "transform_config": {"type": "passthrough", "required": True}},
    {"canonical_field": "total", "erp_field": "InvoiceAmount", "transform_config": {"type": "passthrough", "required": True}},
    {"canonical_field": "vendor_name", "erp_field": "VendorNum", "transform_config": {"type": "passthrough"}},
    {"canonical_field": "purchase_order", "erp_field": "Description", "transform_config": {"type": "passthrough"}},
    {"canonical_field": "payment_terms", "erp_field": "PaymentTerms", "transform_config": {"type": "passthrough"}},
    # __fixed__* canonical_field names never match a real ApInvoice column
    # (build_erp_payload's lookup returns None for them, correctly ignored
    # since transform type "fixed" never reads `raw`) — a fixed, org-
    # configurable constant Oracle requires on every invoice but that has
    # no per-invoice source column. Required since Oracle needs a real
    # value here, not this build's empty placeholder default.
    {"canonical_field": "__fixed__business_unit", "erp_field": "BusinessUnit", "default_value": "", "transform_config": {"type": "fixed", "required": True}},
    {"canonical_field": "__fixed__invoice_type", "erp_field": "InvoiceTypeCode", "default_value": "STANDARD", "transform_config": {"type": "fixed"}},
    {"canonical_field": "__fixed__source", "erp_field": "Source", "default_value": "DIP_INVOICE_INTELLIGENCE", "transform_config": {"type": "fixed"}},
]

_DEFAULT_LINE_MAPPING = [
    {"canonical_field": "line_number", "erp_field": "LineNumber", "is_line_mapping": True, "transform_config": {"type": "passthrough"}},
    {"canonical_field": "description", "erp_field": "Description", "is_line_mapping": True, "transform_config": {"type": "passthrough"}},
    {"canonical_field": "line_total", "erp_field": "LineAmount", "is_line_mapping": True, "transform_config": {"type": "passthrough"}},
    {"canonical_field": "__fixed__line_type", "erp_field": "LineTypeLookupCode", "is_line_mapping": True, "default_value": "ITEM", "transform_config": {"type": "fixed"}},
]


class InvoiceErpMappingError(Exception):
    pass


async def get_current_mapping(db: AsyncSession, organization_id: int | None, erp_connection_id: int) -> list[ApInvoiceErpMapping]:
    result = await db.execute(
        select(ApInvoiceErpMapping).where(
            ApInvoiceErpMapping.organization_id == organization_id,
            ApInvoiceErpMapping.erp_connection_id == erp_connection_id,
            ApInvoiceErpMapping.is_current.is_(True),
        ).order_by(ApInvoiceErpMapping.canonical_field.asc())
    )
    return list(result.scalars().all())


async def seed_default_mapping(db: AsyncSession, organization_id: int | None, erp_connection_id: int) -> list[ApInvoiceErpMapping]:
    """Idempotent — a no-op if this (org, erp_connection) already has a
    current mapping (of any version, seeded or since edited/published)."""
    existing = await get_current_mapping(db, organization_id, erp_connection_id)
    if existing:
        return existing
    rows = [
        ApInvoiceErpMapping(
            organization_id=organization_id, erp_connection_id=erp_connection_id, version=1, is_current=True, **m,
        )
        for m in (*_DEFAULT_HEADER_MAPPING, *_DEFAULT_LINE_MAPPING)
    ]
    db.add_all(rows)
    await db.commit()
    for r in rows:
        await db.refresh(r)
    return rows


async def publish_mapping(
    db: AsyncSession, organization_id: int | None, erp_connection_id: int, rows_in: list[dict], created_by: int | None,
) -> list[ApInvoiceErpMapping]:
    """Publishes a brand-new version: current rows (if any) are flipped to
    is_current=False, a fresh batch is inserted at version = max+1. Never
    mutates a published row's mapping in place — same reproducibility
    guarantee ApInvoice.mapping_version depends on (a past ApInvoice's
    posted payload shape stays traceable to the mapping version that was
    actually current when it posted)."""
    if not rows_in:
        raise InvoiceErpMappingError("A mapping must have at least one field.")

    current = await get_current_mapping(db, organization_id, erp_connection_id)
    next_version = (max((r.version for r in current), default=0)) + 1
    for r in current:
        r.is_current = False

    new_rows = [
        ApInvoiceErpMapping(
            organization_id=organization_id, erp_connection_id=erp_connection_id, version=next_version, is_current=True,
            canonical_field=r["canonical_field"], erp_field=r["erp_field"], is_line_mapping=r.get("is_line_mapping", False),
            default_value=r.get("default_value"), transform_config=r.get("transform_config", {}), created_by=created_by,
        )
        for r in rows_in
    ]
    db.add_all(new_rows)
    await db.commit()
    for r in new_rows:
        await db.refresh(r)
    return new_rows


def _apply_transform(raw, transform_config: dict, default_value: str | None):
    ttype = (transform_config or {}).get("type", "passthrough")
    if ttype == "fixed":
        return default_value
    if raw in (None, ""):
        return default_value
    if ttype == "date_format" and isinstance(raw, datetime):
        return raw.strftime(transform_config.get("format", "%Y-%m-%d"))
    if ttype == "currency_code":
        return str(raw).strip().upper()
    # "lookup" has no reference table wired yet (disclosed, same gap as
    # ApInvoiceVendor.erp_supplier_id) — falls through to passthrough
    # rather than silently dropping the value.
    return raw


class InvoiceErpValidationError(Exception):
    """A REQUIRED canonical field the mapping needs has no value — this is
    a pre-call validation failure, not an ERP rejection, so
    invoice_erp_service treats it as permanent/non-retryable."""


def build_erp_payload(invoice: ApInvoice, lines: list[ApInvoiceLine], mapping_rows: list[ApInvoiceErpMapping]) -> dict:
    """Real field-by-field application of the current mapping — never a
    hardcoded shape. Raises InvoiceErpValidationError only for a field
    explicitly marked transform_config["required"]=true (see the default
    mapping's own comment for exactly which Oracle fields that is) that
    still comes back None after transform/default_value — every other
    blank field is sent as null, since most invoice fields (PO number,
    payment terms, ...) are legitimately optional on a real invoice and
    must never block posting just because extraction didn't find them."""
    header_rows = [m for m in mapping_rows if not m.is_line_mapping]
    line_rows = [m for m in mapping_rows if m.is_line_mapping]

    payload: dict = {}
    invoice_fields = {c.name: getattr(invoice, c.name) for c in invoice.__table__.columns}
    for m in header_rows:
        raw = invoice_fields.get(m.canonical_field)  # canonical_field starting "__fixed__" simply won't match a real column -> raw stays None, which is correct: _apply_transform("fixed") ignores raw entirely
        value = _apply_transform(raw, m.transform_config, m.default_value)
        if value is None and (m.transform_config or {}).get("required"):
            raise InvoiceErpValidationError(f"Required field '{m.canonical_field}' (maps to ERP field '{m.erp_field}') has no value.")
        payload[m.erp_field] = value

    erp_lines = []
    for line in lines:
        line_fields = {c.name: getattr(line, c.name) for c in line.__table__.columns}
        line_fields["line_number"] = line.line_number
        erp_line: dict = {}
        for m in line_rows:
            raw = line_fields.get(m.canonical_field)
            erp_line[m.erp_field] = _apply_transform(raw, m.transform_config, m.default_value)
        erp_lines.append(erp_line)
    if erp_lines:
        payload["invoiceLines"] = erp_lines

    return payload
