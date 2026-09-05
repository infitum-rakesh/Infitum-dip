"""
Lineage service — the API-facing layer over graph.py/impact.py. Adds
execution lineage (a real PipelineJob's node_stats overlaid onto its
version's static graph — record counts per node, not fabricated) and the
version-list helper Pipeline Lineage's version picker needs.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.lineage import graph as graph_mod
from app.lineage import impact as impact_mod
from app.models.pipeline import Pipeline, PipelineVersion
from app.models.pipeline_job import PipelineJob
from app.monitoring import repository as mon_repo


async def get_pipeline_lineage(db: AsyncSession, pipeline_id: int, organization_id: int | None, version_number: int | None = None) -> dict | None:
    return await graph_mod.build_pipeline_lineage(db, pipeline_id, organization_id, version_number)


async def list_pipeline_versions(db: AsyncSession, pipeline_id: int, organization_id: int | None) -> list[dict]:
    pipeline = await mon_repo.get_pipeline(db, pipeline_id, organization_id)
    if pipeline is None:
        return []
    from sqlalchemy import select
    stmt = select(PipelineVersion).where(PipelineVersion.pipeline_id == pipeline_id).order_by(PipelineVersion.version_number.desc())
    rows = list((await db.execute(stmt)).scalars().all())
    return [{"version_number": v.version_number, "is_current": v.version_number == pipeline.current_version, "created_at": v.created_at} for v in rows]


async def get_execution_lineage(db: AsyncSession, job_id: int, organization_id: int | None) -> dict | None:
    job = await mon_repo.get_pipeline_job(db, job_id, organization_id)
    if job is None:
        return None
    static_graph = await graph_mod.build_pipeline_lineage(db, job.pipeline_id, organization_id, job.pipeline_version)
    if static_graph is None:
        return None
    stats = job.node_stats or {}
    for node in static_graph["nodes"]:
        # node["id"] is "node:<canvas_id>" for canvas-derived nodes (as opposed
        # to synthetic "node:<canvas_id>:object"/":file" sub-nodes) — pull the
        # raw canvas id back out so it can be matched against job.node_stats.
        if node["id"].startswith("node:") and ":" not in node["id"][5:]:
            canvas_id = node["id"][5:]
            s = stats.get(canvas_id)
            if s:
                node["execution"] = {
                    "status": s.get("status"), "rows_in": s.get("rows_in"), "rows_out": s.get("rows_out"),
                    "duration_ms": s.get("duration_ms"), "message": s.get("message"),
                }
    return {
        "job_id": job.id, "pipeline_id": job.pipeline_id, "pipeline_name": job.pipeline_name,
        "pipeline_version": job.pipeline_version, "status": job.status.value,
        "started_at": job.started_at, "completed_at": job.completed_at,
        "records_in": job.records_in, "records_out": job.records_out,
        "graph": static_graph,
    }


async def get_impact(db: AsyncSession, organization_id: int | None, connection_id: int | None, object_name: str | None) -> dict:
    return await impact_mod.impact_analysis(db, organization_id, connection_id=connection_id, object_name=object_name)


async def search(db: AsyncSession, organization_id: int | None, query: str) -> list[dict]:
    return await impact_mod.global_search(db, organization_id, query)


async def get_column_mapping_lineage(db: AsyncSession, mapping_id: int, organization_id: int | None) -> dict | None:
    return await graph_mod.get_column_mapping_lineage(db, mapping_id, organization_id)


async def get_invoice_lineage(db: AsyncSession, invoice_id: int, organization_id: int | None) -> dict | None:
    """INV22 — see graph_mod.build_invoice_lineage()'s own docstring."""
    return await graph_mod.build_invoice_lineage(db, invoice_id, organization_id)


async def list_column_mappings(db: AsyncSession, organization_id: int | None) -> list[dict]:
    rows = await graph_mod.list_column_mappings_for_search(db, organization_id)
    return [{"id": m.id, "name": m.name, "source_object": m.source_object, "target_object": m.target_object, "status": m.status} for m in rows]


async def get_overview(db: AsyncSession, organization_id: int | None) -> dict:
    """Lineage Overview tab — org-scoped counts, not a full graph render
    (per the "never render the entire graph at once" requirement)."""
    pipelines = await mon_repo.list_pipelines(db, organization_id)
    mappings = await graph_mod.list_column_mappings_for_search(db, organization_id)
    from sqlalchemy import select, func
    from app.models.connection import Connection
    conn_stmt = select(func.count()).select_from(Connection)
    if organization_id is not None:
        conn_stmt = conn_stmt.where(Connection.organization_id == organization_id)
    connection_count = (await db.execute(conn_stmt)).scalar_one()
    return {
        "pipeline_count": len(pipelines), "column_mapping_count": len(mappings), "connection_count": connection_count,
        "pipelines": [{"id": p.id, "name": p.name, "current_version": p.current_version} for p in pipelines[:50]],
    }
