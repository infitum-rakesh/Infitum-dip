"""
Data Catalog service — real asset discovery over live Connections
(structured) and uploaded Documents (unstructured).

No separate catalog registry/table exists — an asset's identity is
derived live from its real source every call: a structured asset is
`(connection_id, schema, object)` resolved via the SAME connector
introspection already used by the Source Dataset node (list_schemas/
list_objects/get_columns/row_count — see connectors/database/base.py),
and an unstructured asset is a row in the `documents` table. There is
nothing to "sync" because there is no separate copy of the data to go
stale — every list/profile call reflects what the connection/document
actually looks like right now.

Bounded, disclosed scope (same philosophy as the rest of this codebase's
real-but-bounded engines):
  - Structured discovery caps schemas/objects per connection
    (MAX_SCHEMAS_PER_CONNECTION / MAX_OBJECTS_PER_CONNECTION) — a full
    catalog crawl of a huge warehouse isn't attempted; disclosed via
    `truncated` on the stats payload rather than silently dropped.
  - Profiling (get_profile) runs real aggregate SQL (COUNT/NULL-count/
    DISTINCT-count/MIN/MAX/AVG) computed in ONE round trip per object.
    Duplicate-row detection (a full GROUP BY over every column) is
    skipped above DUPLICATE_SCAN_ROW_CAP rows — disclosed via
    `duplicate_distribution.skipped_reason`, not returned as 0.
  - Lineage is derived by scanning every pipeline's CURRENT saved
    definition for Source Dataset nodes pointing at this connection+
    object, and Output Dataset nodes whose dataset name matches (target
    nodes have no connection picker of their own yet in the canvas — see
    TransformationDesigner.jsx — so a target match is name-only, a
    disclosed heuristic, not a certainty).
  - Usage: real consuming-pipeline count (same pipeline scan as
    lineage). Query-level usage (who queried this how often) has no
    tracking infrastructure anywhere in this codebase — those fields are
    returned as None with `tracked: false` rather than fabricated.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal

from app.connectors.base import ConnectorError
from app.models.connection import Connection, ConnectionCategory, ConnectionStatus
from app.models.document import Document, DocumentStatus
from app.models.incident import Incident
from app.models.pipeline import Pipeline, PipelineVersion
from app.models.user import User
from app.models.validation import ValidationRuleResult, ValidationRun, ValidationRuleSet
from app.services.connection_service import build_connector

MAX_SCHEMAS_PER_CONNECTION = 10
MAX_OBJECTS_PER_CONNECTION = 300
DUPLICATE_SCAN_ROW_CAP = 200_000
DUPLICATE_SCAN_MAX_COLUMNS = 20


# ── Asset id encoding — no registry, so the id must carry everything
#    needed to re-resolve the real source. ────────────────────────────

def _encode_id(*parts: Any) -> str:
    raw = json.dumps(list(parts), separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_id(asset_id: str) -> list[Any] | None:
    try:
        padded = asset_id + "=" * (-len(asset_id) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception:
        return None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── Structured discovery (live connector introspection) ────────────

async def _active_db_connections(db: AsyncSession, organization_id: int | None) -> list[Connection]:
    stmt = select(Connection).where(
        Connection.is_active.is_(True),
        Connection.category == ConnectionCategory.DATABASE,
        Connection.status == ConnectionStatus.ACTIVE,
    )
    if organization_id is not None:
        stmt = stmt.where(Connection.organization_id == organization_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _list_structured_assets(db: AsyncSession, organization_id: int | None) -> tuple[list[dict], bool]:
    connections = await _active_db_connections(db, organization_id)
    owner_ids = {c.owner_id for c in connections if c.owner_id}
    name_map = await _user_name_map(db, owner_ids)

    assets: list[dict] = []
    truncated = False
    for conn in connections:
        try:
            connector = build_connector(conn)
            schemas = await asyncio.to_thread(connector.list_schemas)
        except ConnectorError:
            continue
        schemas = schemas or [None]
        if len(schemas) > MAX_SCHEMAS_PER_CONNECTION:
            schemas = schemas[:MAX_SCHEMAS_PER_CONNECTION]
            truncated = True

        object_count = 0
        for schema_name in schemas:
            if object_count >= MAX_OBJECTS_PER_CONNECTION:
                truncated = True
                break
            try:
                objects = await asyncio.to_thread(connector.list_objects, schema_name, None)
            except ConnectorError:
                continue
            for obj in objects:
                if object_count >= MAX_OBJECTS_PER_CONNECTION:
                    truncated = True
                    break
                object_count += 1
                assets.append({
                    "id": _encode_id("db", conn.id, obj.schema, obj.name),
                    "name": obj.name,
                    "asset_type": "View" if getattr(obj, "object_type", "TABLE") == "VIEW" else "Table",
                    "source_type": "Database",
                    "source": conn.connection_type.value.replace("_", " ").title(),
                    "environment": conn.environment.value,
                    "database": conn.config.get("database") or conn.config.get("account") or conn.name,
                    "schema": obj.schema or "public",
                    "object": obj.name,
                    "domain": None,
                    "layer": None,
                    "rows": None,       # not profiled on list — see get_profile()
                    "columns": None,
                    "quality_score": None,
                    "null_percent": None,
                    "duplicate_percent": None,
                    "owner": name_map.get(conn.owner_id, "—"),
                    "status": "Active",
                    "tags": [],
                    "description": f"{obj.schema + '.' if obj.schema else ''}{obj.name} on connection \"{conn.name}\" ({conn.connection_type.value}).",
                    "created_at": _iso(conn.created_at),
                    "last_updated": _iso(conn.updated_at),
                    "last_profiled": None,
                    "is_structured": True,
                    "connection_id": conn.id,
                })
    return assets, truncated


async def _list_unstructured_assets(db: AsyncSession, organization_id: int | None) -> list[dict]:
    stmt = select(Document)
    if organization_id is not None:
        stmt = stmt.where(Document.organization_id == organization_id)
    result = await db.execute(stmt)
    docs = list(result.scalars().all())
    owner_ids = {d.uploaded_by_id for d in docs if d.uploaded_by_id}
    name_map = await _user_name_map(db, owner_ids)

    assets = []
    for d in docs:
        quality = round(d.confidence, 1) if d.confidence is not None else (round(d.ocr_percent, 1) if d.ocr_percent is not None else None)
        assets.append({
            "id": _encode_id("doc", d.id),
            "name": d.filename,
            "asset_type": "File",
            "source_type": "Document",
            "source": d.doc_type.value,
            "environment": "PRODUCTION",
            "database": "Document Store",
            "schema": "unstructured",
            "object": d.filename,
            "domain": None,
            "layer": None,
            "rows": None,
            "columns": None,
            "quality_score": quality,
            "null_percent": None,
            "duplicate_percent": None,
            "owner": name_map.get(d.uploaded_by_id, "—"),
            "status": "Active" if d.status != DocumentStatus.FAILED else "Draft",
            "tags": [d.status.value.lower().replace("_", "-")],
            "description": f"{d.doc_type.value} document, {d.page_count} page(s), uploaded to {d.folder}.",
            "created_at": _iso(d.uploaded_at),
            "last_updated": _iso(d.updated_at),
            "last_profiled": _iso(d.processing_completed_at),
            "is_structured": False,
            "document_id": d.id,
        })
    return assets


async def _user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row.id: row.full_name for row in result.all()}


async def _all_assets(db: AsyncSession, organization_id: int | None) -> tuple[list[dict], bool]:
    structured, truncated = await _list_structured_assets(db, organization_id)
    unstructured = await _list_unstructured_assets(db, organization_id)
    return structured + unstructured, truncated


# ── Query helpers ────────────────────────────────────────────────────

def _matches_search(asset: dict, q: str) -> bool:
    q = q.lower().strip()
    if not q:
        return True
    haystack = " ".join(str(v) for v in [
        asset["name"], asset["schema"], asset["database"], asset["owner"],
        asset["source"], asset["object"], " ".join(asset["tags"]),
    ]).lower()
    return q in haystack


async def list_assets(
    db: AsyncSession, organization_id: int | None, *,
    search: str = "", source_type: str | None = None, environment: str | None = None,
    database: str | None = None, schema: str | None = None, asset_type: str | None = None,
    owner: str | None = None, status: str | None = None, quality: str | None = None,
    tag: str | None = None, source: str | None = None,
    page: int = 1, page_size: int = 20, sort_by: str = "last_updated", sort_dir: str = "desc",
) -> dict[str, Any]:
    all_assets, _truncated = await _all_assets(db, organization_id)
    results = [a for a in all_assets if _matches_search(a, search)]

    def _f(val, field):
        nonlocal results
        if val and val != "All":
            results = [a for a in results if str(a.get(field)) == val]

    _f(source_type, "source_type")
    _f(environment, "environment")
    _f(database, "database")
    _f(schema, "schema")
    _f(asset_type, "asset_type")
    _f(owner, "owner")
    _f(status, "status")
    _f(source, "source")
    if tag and tag != "All":
        results = [a for a in results if tag in a["tags"]]
    if quality and quality != "All":
        band = {"high": lambda s: s is not None and s >= 90, "medium": lambda s: s is not None and 70 <= s < 90, "low": lambda s: s is not None and s < 70}.get(quality)
        if band:
            results = [a for a in results if band(a["quality_score"])]

    reverse = sort_dir == "desc"
    results = sorted(results, key=lambda a: (a.get(sort_by) is None, a.get(sort_by) or ""), reverse=reverse)

    total = len(results)
    start = max(0, (page - 1) * page_size)
    page_items = results[start:start + page_size]
    return {"items": page_items, "total": total, "page": page, "page_size": page_size, "total_pages": max(1, (total + page_size - 1) // page_size)}


async def get_facets(db: AsyncSession, organization_id: int | None) -> dict[str, Any]:
    all_assets, _truncated = await _all_assets(db, organization_id)

    def _sorted_set(field):
        return sorted({a[field] for a in all_assets if a.get(field)})

    return {
        "source_type": ["Database", "File", "API", "Document"],
        "environment": _sorted_set("environment"),
        "database": _sorted_set("database"),
        "schema": _sorted_set("schema"),
        "asset_type": _sorted_set("asset_type"),
        "owner": _sorted_set("owner"),
        "status": _sorted_set("status"),
        "tags": sorted({t for a in all_assets for t in a["tags"]}),
        "source": _sorted_set("source"),
        "quality": ["high", "medium", "low"],
    }


async def get_stats(db: AsyncSession, organization_id: int | None) -> dict[str, Any]:
    all_assets, truncated = await _all_assets(db, organization_id)
    total = len(all_assets)
    datasets = len([a for a in all_assets if a["asset_type"] in ("Table", "View")])
    tables = len([a for a in all_assets if a["asset_type"] == "Table"])
    files = len([a for a in all_assets if a["asset_type"] == "File"])
    scored = [a["quality_score"] for a in all_assets if a["quality_score"] is not None]
    avg_quality = round(sum(scored) / len(scored), 1) if scored else None
    recent = sorted((a for a in all_assets if a["last_updated"]), key=lambda a: a["last_updated"], reverse=True)[:8]
    return {
        "total_assets": total, "datasets": datasets, "tables": tables, "files": files,
        "data_quality_score": avg_quality, "recent_assets": recent, "truncated": truncated,
    }


# ── Single-asset resolution ─────────────────────────────────────────

async def _resolve(db: AsyncSession, asset_id: str, organization_id: int | None):
    """Returns ('db', connection, connector, schema, object, asset_dict) or ('doc', document, None, ...) or None."""
    parts = _decode_id(asset_id)
    if not parts:
        return None
    kind = parts[0]
    if kind == "db":
        _, connection_id, schema_name, object_name = parts
        from app.services.connection_service import get_connection
        conn = await get_connection(db, connection_id, organization_id)
        if conn is None:
            return None
        try:
            connector = build_connector(conn)
        except ConnectorError:
            return None
        structured, _ = await _list_structured_assets(db, organization_id)
        asset = next((a for a in structured if a["id"] == asset_id), None)
        if asset is None:
            # Connection is real but this exact object no longer appears
            # (dropped, or beyond MAX_OBJECTS_PER_CONNECTION) — build a
            # minimal real asset dict from the id itself rather than 404
            # on something that may still genuinely exist.
            asset = {
                "id": asset_id, "name": object_name, "asset_type": "Table", "source_type": "Database",
                "source": conn.connection_type.value.replace("_", " ").title(), "environment": conn.environment.value,
                "database": conn.config.get("database") or conn.name, "schema": schema_name or "public",
                "object": object_name, "domain": None, "layer": None, "rows": None, "columns": None,
                "quality_score": None, "null_percent": None, "duplicate_percent": None, "owner": "—",
                "status": "Active", "tags": [], "description": "", "created_at": _iso(conn.created_at),
                "last_updated": _iso(conn.updated_at), "last_profiled": None, "is_structured": True, "connection_id": conn.id,
            }
        return ("db", conn, connector, schema_name, object_name, asset)
    if kind == "doc":
        _, document_id = parts
        doc = await db.get(Document, document_id)
        if doc is None or (organization_id is not None and doc.organization_id != organization_id):
            return None
        unstructured = await _list_unstructured_assets(db, organization_id)
        asset = next((a for a in unstructured if a["id"] == asset_id), None)
        return ("doc", doc, None, None, None, asset)
    return None


async def get_asset(db: AsyncSession, asset_id: str, organization_id: int | None) -> dict | None:
    resolved = await _resolve(db, asset_id, organization_id)
    return resolved[5] if resolved else None


async def get_columns(db: AsyncSession, asset_id: str, organization_id: int | None) -> list[dict] | None:
    resolved = await _resolve(db, asset_id, organization_id)
    if resolved is None:
        return None
    kind, _obj, connector, schema_name, object_name, _asset = resolved
    if kind != "db":
        return []  # documents have no structured columns
    try:
        cols = await asyncio.to_thread(connector.get_columns, object_name, schema_name)
    except ConnectorError:
        return []
    return [
        {"name": c.name, "data_type": c.data_type, "nullable": c.nullable, "primary_key": c.primary_key,
         "null_percent": None, "distinct_percent": None, "min": None, "max": None, "quality_score": None}
        for c in cols
    ]


# ── Real profiling — one round trip of aggregate SQL per object ───

def _profile_sync(connector, object_name: str, schema_name: str | None, columns: list) -> dict:
    engine = connector.engine(pool_size=1)
    quoted_table = connector._quote_object(object_name, schema_name, engine)
    prep = engine.dialect.identifier_preparer

    numeric_types = ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL", "SERIAL", "MONEY")
    select_parts = ["COUNT(*) AS __total__"]
    numeric_cols = []
    for c in columns:
        q = prep.quote(c.name)
        select_parts.append(f"SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS {prep.quote('null_' + c.name)}")
        select_parts.append(f"COUNT(DISTINCT {q}) AS {prep.quote('distinct_' + c.name)}")
        if any(t in c.data_type.upper() for t in numeric_types):
            numeric_cols.append(c.name)
            select_parts.append(f"MIN({q}) AS {prep.quote('min_' + c.name)}")
            select_parts.append(f"MAX({q}) AS {prep.quote('max_' + c.name)}")
            select_parts.append(f"AVG({q}) AS {prep.quote('avg_' + c.name)}")

    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT {', '.join(select_parts)} FROM {quoted_table}")).mappings().first()
        total = row["__total__"] or 0

        by_column, unique_values, numeric_stats = [], {}, {}
        for c in columns:
            null_n = row.get(f"null_{c.name}") or 0
            distinct_n = row.get(f"distinct_{c.name}") or 0
            by_column.append({"column": c.name, "null_percent": round(null_n / total * 100, 1) if total else 0.0})
            unique_values[c.name] = round(distinct_n / total * 100, 1) if total else 0.0
            if c.name in numeric_cols:
                mn, mx, avg = row.get(f"min_{c.name}"), row.get(f"max_{c.name}"), row.get(f"avg_{c.name}")
                numeric_stats[c.name] = {"min": mn, "max": mx, "average": round(float(avg), 2) if avg is not None else None}

        duplicate_rows = None
        skipped_reason = None
        if total and total <= DUPLICATE_SCAN_ROW_CAP and columns:
            dup_cols = [prep.quote(c.name) for c in columns[:DUPLICATE_SCAN_MAX_COLUMNS]]
            dup_sql = f"SELECT COUNT(*) FROM (SELECT {', '.join(dup_cols)} FROM {quoted_table} GROUP BY {', '.join(dup_cols)} HAVING COUNT(*) > 1) dup_t"
            try:
                duplicate_rows = conn.execute(text(dup_sql)).scalar() or 0
            except Exception:
                skipped_reason = "Duplicate scan failed for this object's column types."
        elif total:
            skipped_reason = f"Skipped — {total:,} rows exceeds the {DUPLICATE_SCAN_ROW_CAP:,}-row duplicate-scan cap."

        top_values = []
        if columns:
            first_col = prep.quote(columns[0].name)
            try:
                top_rows = conn.execute(text(
                    f"SELECT {first_col} AS v, COUNT(*) AS c FROM {quoted_table} "
                    f"WHERE {first_col} IS NOT NULL GROUP BY {first_col} ORDER BY c DESC LIMIT 5"
                )).all()
                top_values = [{"value": str(r[0]), "count": r[1], "percent": round(r[1] / total * 100, 1) if total else 0.0} for r in top_rows]
            except Exception:
                top_values = []

    return {
        "row_count": total,
        "null_distribution": {
            "overall_null_percent": round(sum(b["null_percent"] for b in by_column) / len(by_column), 1) if by_column else 0.0,
            "by_column": by_column,
        },
        "duplicate_distribution": {
            "overall_duplicate_percent": round(duplicate_rows / total * 100, 1) if (duplicate_rows is not None and total) else None,
            "duplicate_rows": duplicate_rows,
            "skipped_reason": skipped_reason,
        },
        "unique_values": unique_values,
        "value_distribution": top_values,
        "top_values": top_values,
        "data_type_distribution": _dtype_counts(columns),
        "numeric_stats": _combine_numeric(numeric_stats),
    }


def _dtype_counts(columns: list) -> list[dict]:
    counts: dict[str, int] = {}
    for c in columns:
        base = c.data_type.split("(")[0].upper()
        counts[base] = counts.get(base, 0) + 1
    return [{"type": k, "count": v} for k, v in counts.items()]


def _combine_numeric(numeric_stats: dict) -> dict | None:
    if not numeric_stats:
        return None
    mins = [v["min"] for v in numeric_stats.values() if v["min"] is not None]
    maxs = [v["max"] for v in numeric_stats.values() if v["max"] is not None]
    avgs = [v["average"] for v in numeric_stats.values() if v["average"] is not None]
    return {
        "min": min(mins) if mins else None, "max": max(maxs) if maxs else None,
        "average": round(sum(avgs) / len(avgs), 2) if avgs else None, "std_dev": None,
        "by_column": numeric_stats,
    }


async def get_profile(db: AsyncSession, asset_id: str, organization_id: int | None) -> dict | None:
    resolved = await _resolve(db, asset_id, organization_id)
    if resolved is None:
        return None
    kind, obj, connector, schema_name, object_name, asset = resolved

    if kind == "doc":
        doc: Document = obj
        return {
            "asset_id": asset_id, "row_count": doc.page_count,
            "null_distribution": {"overall_null_percent": None, "by_column": []},
            "duplicate_distribution": {"overall_duplicate_percent": None, "duplicate_rows": None, "skipped_reason": "Not applicable — unstructured document."},
            "unique_values": {}, "value_distribution": [], "top_values": [], "data_type_distribution": [],
            "numeric_stats": None,
            "document_status": doc.status.value, "ocr_confidence": doc.ocr_percent, "extraction_confidence": doc.extraction_percent,
            "profiled_at": _iso(doc.processing_completed_at),
        }

    try:
        columns = await asyncio.to_thread(connector.get_columns, object_name, schema_name)
        profile = await asyncio.to_thread(_profile_sync, connector, object_name, schema_name, columns)
    except ConnectorError as exc:
        return {"asset_id": asset_id, "error": connector._scrub(str(exc)), "row_count": None, "profiled_at": _iso(datetime.now(timezone.utc))}

    profile["asset_id"] = asset_id
    profile["profiled_at"] = _iso(datetime.now(timezone.utc))
    return profile


# ── Lineage — derived from real saved pipeline definitions ─────────

async def _current_pipeline_definitions(db: AsyncSession, organization_id: int | None) -> list[tuple[str, dict]]:
    stmt = select(Pipeline)
    if organization_id is not None:
        stmt = stmt.where(Pipeline.organization_id == organization_id)
    result = await db.execute(stmt)
    pipelines = list(result.scalars().all())
    out = []
    for p in pipelines:
        v = await db.execute(select(PipelineVersion).where(PipelineVersion.pipeline_id == p.id, PipelineVersion.version_number == p.current_version))
        version = v.scalar_one_or_none()
        if version:
            out.append((p.name, version.pipeline_definition or {}))
    return out


async def get_lineage(db: AsyncSession, asset_id: str, organization_id: int | None) -> dict | None:
    resolved = await _resolve(db, asset_id, organization_id)
    if resolved is None:
        return None
    kind, obj, _connector, _schema, _object, asset = resolved
    if asset is None:
        return None

    pipelines = await _current_pipeline_definitions(db, organization_id)
    feeds_from, feeds_into = [], []
    for pipeline_name, defn in pipelines:
        for node in defn.get("nodes", []):
            cfg = node.get("config", {}) or {}
            if node.get("type") == "source" and kind == "db" and cfg.get("connectionId") == asset.get("connection_id") and cfg.get("object") == asset.get("object"):
                feeds_into.append(pipeline_name)
            if node.get("type") == "target" and cfg.get("datasetName") == asset.get("name"):
                feeds_from.append(pipeline_name)

    nodes = [{"id": f"src_{i}", "label": p, "type": "source", "is_current": False} for i, p in enumerate(dict.fromkeys(feeds_from))]
    nodes.append({"id": "current", "label": asset["name"], "type": "dataset", "is_current": True})
    nodes += [{"id": f"tgt_{i}", "label": p, "type": "target", "is_current": False} for i, p in enumerate(dict.fromkeys(feeds_into))]
    current_idx = next(i for i, n in enumerate(nodes) if n["is_current"])
    edges = [{"from": nodes[i]["id"], "to": nodes[i + 1]["id"]} for i in range(len(nodes) - 1)]
    return {"asset_id": asset_id, "nodes": nodes, "edges": edges, "note": "Derived from pipelines currently referencing this asset by name — not a full column-level lineage graph." if (feeds_from or feeds_into) else "No saved pipeline currently references this asset."}


# ── Quality — derived from the real profile (+ real ValidationRuleSet results, if one targets this exact object) ──

async def get_quality(db: AsyncSession, asset_id: str, organization_id: int | None) -> dict | None:
    resolved = await _resolve(db, asset_id, organization_id)
    if resolved is None:
        return None
    kind, obj, _connector, schema_name, object_name, asset = resolved

    rules = []
    if kind == "db":
        conn_id = asset.get("connection_id")
        rs_stmt = select(ValidationRuleSet).where(ValidationRuleSet.connection_id == conn_id, ValidationRuleSet.object_name == object_name)
        if organization_id is not None:
            rs_stmt = rs_stmt.where(ValidationRuleSet.organization_id == organization_id)
        rs_result = await db.execute(rs_stmt)
        rule_sets = list(rs_result.scalars().all())
        for rs in rule_sets:
            run_result = await db.execute(select(ValidationRun).where(ValidationRun.rule_set_id == rs.id).order_by(ValidationRun.created_at.desc()).limit(1))
            latest_run = run_result.scalar_one_or_none()
            if latest_run:
                res_result = await db.execute(select(ValidationRuleResult).where(ValidationRuleResult.run_id == latest_run.id))
                for r in res_result.scalars().all():
                    rules.append({"rule": r.rule_name, "column": ", ".join(r.columns) if r.columns else "—", "status": r.status.value.lower(), "failed_records": r.fail_count})

    profile = await get_profile(db, asset_id, organization_id)
    if profile is None or profile.get("row_count") is None:
        overall_null = None
        dup_pct = None
    else:
        overall_null = profile["null_distribution"]["overall_null_percent"]
        dup_pct = profile["duplicate_distribution"]["overall_duplicate_percent"]

    completeness = round(100 - overall_null, 1) if overall_null is not None else None
    uniqueness = round(100 - dup_pct, 1) if dup_pct is not None else None
    rule_pass_rates = [r["status"] == "passed" for r in rules]
    validity = round(sum(rule_pass_rates) / len(rule_pass_rates) * 100, 1) if rule_pass_rates else None

    dims = {k: v for k, v in {"completeness": completeness, "uniqueness": uniqueness, "validity": validity}.items() if v is not None}
    overall = round(sum(dims.values()) / len(dims), 1) if dims else (asset.get("quality_score") if asset else None)

    return {
        "asset_id": asset_id, "overall_score": overall,
        "last_evaluated": profile.get("profiled_at") if profile else None,
        "failed_records_total": sum(r["failed_records"] for r in rules) if rules else 0,
        "dimensions": dims, "rules": rules,
        "note": None if rules else "No saved Validation rule set targets this exact object yet — dimensions are computed directly from live profiling, not rule evaluation.",
    }


# ── Incidents — real, matched by dataset/connection name on the Incident table ──

async def get_incidents(db: AsyncSession, asset_id: str, organization_id: int | None) -> list[dict] | None:
    resolved = await _resolve(db, asset_id, organization_id)
    if resolved is None:
        return None
    _kind, _obj, _connector, _schema, _object, asset = resolved
    if asset is None:
        return None

    stmt = select(Incident).where(Incident.dataset_name == asset["name"])
    if organization_id is not None:
        stmt = stmt.where(Incident.organization_id == organization_id)
    result = await db.execute(stmt)
    incidents = list(result.scalars().all())
    return [
        {"title": i.title, "severity": i.severity.value, "status": i.status.value, "opened_at": _iso(i.detected_at), "resolved_at": _iso(i.resolved_at)}
        for i in incidents
    ]


# ── Usage — real consuming-pipeline count; no query-log tracking exists ──

async def get_usage(db: AsyncSession, asset_id: str, organization_id: int | None) -> dict | None:
    resolved = await _resolve(db, asset_id, organization_id)
    if resolved is None:
        return None
    _kind, _obj, _connector, _schema, _object, asset = resolved
    if asset is None:
        return None

    pipelines = await _current_pipeline_definitions(db, organization_id)
    consuming = set()
    for pipeline_name, defn in pipelines:
        for node in defn.get("nodes", []):
            cfg = node.get("config", {}) or {}
            if node.get("type") == "source" and cfg.get("connectionId") == asset.get("connection_id") and cfg.get("object") == asset.get("object"):
                consuming.add(pipeline_name)
            if node.get("type") == "target" and cfg.get("datasetName") == asset.get("name"):
                consuming.add(pipeline_name)

    return {
        "asset_id": asset_id, "tracked": False,
        "queries_last_30d": None, "distinct_users_last_30d": None, "consuming_dashboards": None,
        "daily_query_counts": [],
        "consuming_pipelines": len(consuming),
        "consuming_pipeline_names": sorted(consuming),
        "note": "Query-level usage isn't tracked anywhere in this build — only real pipeline references are counted.",
    }
