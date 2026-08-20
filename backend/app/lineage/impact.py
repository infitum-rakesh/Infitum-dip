"""
Impact analysis + global lineage search — both scan real, live data (every
pipeline's CURRENT version definition + every saved ColumnMapping), never
a cached/duplicated index. Good enough at this codebase's actual scale
(a handful to a few hundred pipelines per org); see module docstring on
graph.py for the "never render/scan the whole graph at once" principle —
this module already bounds itself to one org's rows via the same
organization_id scoping every other repository function uses, and the
per-pipeline scan is O(pipelines), not O(executions), so it stays cheap
even with a long execution history.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.column_mapping import ColumnMapping
from app.models.connection import Connection
from app.models.pipeline import Pipeline, PipelineVersion
from app.monitoring import repository as mon_repo


async def _current_definitions(db: AsyncSession, organization_id: int | None) -> list[tuple[Pipeline, PipelineVersion]]:
    pipelines = await mon_repo.list_pipelines(db, organization_id)
    out = []
    for p in pipelines:
        version = await mon_repo.get_pipeline_version(db, p.id, p.current_version)
        if version is not None:
            out.append((p, version))
    return out


def _node_object_key(node: dict) -> tuple[int | None, str | None] | None:
    cfg = node.get("config") or {}
    conn_id = cfg.get("connectionId")
    obj = cfg.get("object") or cfg.get("datasetName")
    if conn_id is None and not obj:
        return None
    return (conn_id, obj)


async def find_pipelines_using_object(db: AsyncSession, organization_id: int | None, connection_id: int | None, object_name: str | None) -> list[dict]:
    hits = []
    for pipeline, version in await _current_definitions(db, organization_id):
        for node in (version.pipeline_definition or {}).get("nodes") or []:
            if node.get("type") not in ("source", "target"):
                continue
            key = _node_object_key(node)
            if key is None:
                continue
            k_conn, k_obj = key
            if (connection_id is None or k_conn == connection_id) and (object_name is None or k_obj == object_name):
                hits.append({
                    "pipeline_id": pipeline.id, "pipeline_name": pipeline.name, "version_number": version.version_number,
                    "node_id": node["id"], "role": "SOURCE" if node["type"] == "source" else "TARGET",
                })
    return hits


async def find_column_mappings_using_object(db: AsyncSession, organization_id: int | None, connection_id: int | None, object_name: str | None) -> list[dict]:
    stmt = select(ColumnMapping)
    if organization_id is not None:
        stmt = stmt.where(ColumnMapping.organization_id == organization_id)
    rows = list((await db.execute(stmt)).scalars().all())
    hits = []
    for m in rows:
        is_source = (connection_id is None or m.source_connection_id == connection_id) and (object_name is None or m.source_object == object_name)
        is_target = (connection_id is None or m.target_connection_id == connection_id) and (object_name is None or m.target_object == object_name)
        if is_source or (object_name is not None and is_target and not is_source):
            role = "SOURCE" if (connection_id == m.source_connection_id or object_name == m.source_object) else "TARGET"
            hits.append({"mapping_id": m.id, "name": m.name, "role": role})
        elif is_target:
            hits.append({"mapping_id": m.id, "name": m.name, "role": "TARGET"})
    # de-dupe (an object can legitimately match both branches above)
    seen = set()
    deduped = []
    for h in hits:
        k = (h["mapping_id"], h["role"])
        if k not in seen:
            seen.add(k)
            deduped.append(h)
    return deduped


async def impact_analysis(db: AsyncSession, organization_id: int | None, *, connection_id: int | None, object_name: str | None) -> dict:
    conn = await db.get(Connection, connection_id) if connection_id else None
    pipelines_hit = await find_pipelines_using_object(db, organization_id, connection_id, object_name)
    mappings_hit = await find_column_mappings_using_object(db, organization_id, connection_id, object_name)

    downstream_targets = {h["pipeline_name"] for h in pipelines_hit if h["role"] == "TARGET"}
    upstream_sources = {h["pipeline_name"] for h in pipelines_hit if h["role"] == "SOURCE"}
    affected_pipeline_names = {h["pipeline_name"] for h in pipelines_hit}
    affected_mapping_targets = {h["name"] for h in mappings_hit}

    warning = (
        f"{len(affected_pipeline_names)} pipeline(s) affected, "
        f"{len(affected_mapping_targets)} object/column mapping(s) affected, "
        f"{len(downstream_targets)} downstream target(s), {len(upstream_sources)} upstream source use(s)."
    )
    return {
        "asset": {"connection_id": connection_id, "connection_name": conn.name if conn else None, "object_name": object_name},
        "used_by_pipelines": pipelines_hit,
        "used_by_mappings": mappings_hit,
        "downstream_pipeline_count": len(downstream_targets),
        "upstream_pipeline_count": len(upstream_sources),
        "warning_summary": warning,
    }


async def global_search(db: AsyncSession, organization_id: int | None, query: str, limit: int = 50) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return []
    results: list[dict] = []

    conn_stmt = select(Connection)
    if organization_id is not None:
        conn_stmt = conn_stmt.where(Connection.organization_id == organization_id)
    for c in (await db.execute(conn_stmt)).scalars().all():
        if q in c.name.lower():
            pipelines_using = await find_pipelines_using_object(db, organization_id, c.id, None)
            results.append({
                "asset_type": c.connection_type.value if hasattr(c.connection_type, "value") else str(c.connection_type),
                "asset": c.name, "system": c.name, "owner": None,
                "used_by": len(pipelines_using), "upstream_count": 0, "downstream_count": len(pipelines_using),
                "ref": {"kind": "connection", "connection_id": c.id},
            })

    for p in await mon_repo.list_pipelines(db, organization_id):
        if q in p.name.lower():
            results.append({
                "asset_type": "PIPELINE", "asset": p.name, "system": "Pipeline Builder", "owner": None,
                "used_by": None, "upstream_count": None, "downstream_count": None,
                "ref": {"kind": "pipeline", "pipeline_id": p.id},
            })

    map_stmt = select(ColumnMapping)
    if organization_id is not None:
        map_stmt = map_stmt.where(ColumnMapping.organization_id == organization_id)
    for m in (await db.execute(map_stmt)).scalars().all():
        haystack = " ".join(filter(None, [m.name, m.source_object, m.target_object])).lower()
        if q in haystack:
            results.append({
                "asset_type": "DATASET", "asset": m.name, "system": "Object & Column Mapping", "owner": None,
                "used_by": None, "upstream_count": 1, "downstream_count": 1,
                "ref": {"kind": "mapping", "mapping_id": m.id},
            })

    return results[:limit]
