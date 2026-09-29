"""
Pipeline service — backs the Transform Workspace canvas.

Versioning rules (see models/pipeline.py + the Transform Workspace spec):
  - Save NEVER overwrites a pipeline_definition in place. Every save inserts
    a new PipelineVersion row and bumps Pipeline.current_version.
  - Only the most recent 5 versions are retained; anything older is pruned
    right after a successful save.
  - Restoring an old version does not rewind history — it creates a brand
    new version copying that old definition forward (e.g. restoring v7 on
    top of v8 produces v9), so history is never destructively rewritten.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import AsyncSessionLocal

from app.models.pipeline import Pipeline, PipelineVersion
from app.models.pipeline_job import PipelineJob, PipelineJobLog
from app.models.user import User

MAX_VERSIONS = 5


class PipelineNotFoundError(Exception):
    pass


class PipelineVersionNotFoundError(Exception):
    pass


# ── Lookups ──────────────────────────────────────────────────────

async def get_pipeline(db: AsyncSession, pipeline_id: int, organization_id: int | None) -> Pipeline | None:
    stmt = select(Pipeline).where(Pipeline.id == pipeline_id)
    if organization_id is not None:
        stmt = stmt.where(Pipeline.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_pipeline_for_document(db: AsyncSession, document_id: int, organization_id: int | None) -> Pipeline | None:
    """Open Pipeline bug fix — lets the caller check for an existing
    pipeline before building a new one, so re-clicking "Open Pipeline"
    for the same document reopens it instead of creating a duplicate."""
    stmt = select(Pipeline).where(Pipeline.document_id == document_id)
    if organization_id is not None:
        stmt = stmt.where(Pipeline.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_pipelines(db: AsyncSession, organization_id: int | None) -> tuple[list[Pipeline], int]:
    stmt = select(Pipeline).order_by(Pipeline.updated_at.desc())
    if organization_id is not None:
        stmt = stmt.where(Pipeline.organization_id == organization_id)
    result = await db.execute(stmt)
    items = list(result.scalars().all())
    return items, len(items)


async def get_version(db: AsyncSession, pipeline_id: int, version_number: int) -> PipelineVersion | None:
    result = await db.execute(
        select(PipelineVersion).where(
            PipelineVersion.pipeline_id == pipeline_id,
            PipelineVersion.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()


async def get_current_version(db: AsyncSession, pipeline: Pipeline) -> PipelineVersion | None:
    return await get_version(db, pipeline.id, pipeline.current_version)


async def list_versions(db: AsyncSession, pipeline_id: int) -> list[PipelineVersion]:
    result = await db.execute(
        select(PipelineVersion)
        .where(PipelineVersion.pipeline_id == pipeline_id)
        .order_by(PipelineVersion.version_number.desc())
    )
    return list(result.scalars().all())


# ── Create / save (always a new version) ───────────────────────────

async def create_pipeline(
    db: AsyncSession, *, organization_id: int | None, user_id: int | None,
    name: str, nodes: list[dict], edges: list[dict], settings: dict,
    document_id: int | None = None,
) -> Pipeline:
    pipeline = Pipeline(
        organization_id=organization_id, name=name, current_version=1,
        created_by=user_id, updated_by=user_id, document_id=document_id,
    )
    db.add(pipeline)
    await db.flush()  # assign pipeline.id

    version = PipelineVersion(
        pipeline_id=pipeline.id, version_number=1,
        pipeline_definition={"nodes": nodes, "edges": edges, "settings": settings},
        created_by=user_id, change_summary="Initial version",
    )
    db.add(version)
    await db.commit()
    await db.refresh(pipeline)
    return pipeline


async def save_version(
    db: AsyncSession, pipeline: Pipeline, *, user_id: int | None,
    name: str | None, nodes: list[dict], edges: list[dict], settings: dict,
    change_summary: str | None = None,
) -> tuple[Pipeline, PipelineVersion]:
    new_version_number = pipeline.current_version + 1

    version = PipelineVersion(
        pipeline_id=pipeline.id, version_number=new_version_number,
        pipeline_definition={"nodes": nodes, "edges": edges, "settings": settings},
        created_by=user_id, change_summary=change_summary,
    )
    db.add(version)

    pipeline.current_version = new_version_number
    if name:
        pipeline.name = name
    pipeline.updated_by = user_id
    db.add(pipeline)
    await db.flush()

    # Retention: keep only the most recent MAX_VERSIONS version rows.
    cutoff = new_version_number - MAX_VERSIONS
    if cutoff >= 1:
        old = await db.execute(
            select(PipelineVersion).where(
                PipelineVersion.pipeline_id == pipeline.id,
                PipelineVersion.version_number <= cutoff,
            )
        )
        for row in old.scalars().all():
            await db.delete(row)

    await db.commit()
    await db.refresh(pipeline)
    await db.refresh(version)
    return pipeline, version


async def restore_version(
    db: AsyncSession, pipeline: Pipeline, version_number: int, *, user_id: int | None,
) -> tuple[Pipeline, PipelineVersion]:
    old = await get_version(db, pipeline.id, version_number)
    if old is None:
        raise PipelineVersionNotFoundError(f"Version {version_number} not found")
    defn = old.pipeline_definition or {}
    return await save_version(
        db, pipeline, user_id=user_id, name=None,
        nodes=defn.get("nodes", []), edges=defn.get("edges", []), settings=defn.get("settings", {}),
        change_summary=f"Restored from v{version_number}",
    )


async def delete_pipeline(db: AsyncSession, pipeline: Pipeline) -> None:
    """Deletes the pipeline and everything that references it — including
    its PipelineJob run history and each job's PipelineJobLog lines.
    Missing this used to leave orphaned job/log rows behind (neither
    FK is ON DELETE CASCADE, and SQLite doesn't enforce FKs by default),
    which was a real, observed bug: SQLite reuses a deleted row's
    integer id for the next insert (no true AUTOINCREMENT unless the
    table opts in — see models/pipeline_job.py), so a brand-new job for
    a totally different, later pipeline could be assigned the exact
    same id as one of these orphans and inherit its stale log lines —
    e.g. an old SFTP failure from a deleted pipeline bleeding into an
    unrelated CSV job's log view. Deleting logs -> jobs -> versions ->
    pipeline, in that order, keeps every step consistent even without
    DB-level cascade support."""
    job_ids_subq = select(PipelineJob.id).where(PipelineJob.pipeline_id == pipeline.id)
    await db.execute(PipelineJobLog.__table__.delete().where(PipelineJobLog.job_id.in_(job_ids_subq)))
    await db.execute(PipelineJob.__table__.delete().where(PipelineJob.pipeline_id == pipeline.id))
    await db.execute(
        PipelineVersion.__table__.delete().where(PipelineVersion.pipeline_id == pipeline.id)
    )
    await db.delete(pipeline)
    await db.commit()


# ── Compare ──────────────────────────────────────────────────────

_TRACKED_CONFIG_KEYS_LABEL = "config"


def _node_label(n: dict) -> str:
    return n.get("label") or n.get("type") or n.get("id", "?")


def _diff_node(a: dict, b: dict) -> list[str]:
    changes = []
    if a.get("label") != b.get("label"):
        changes.append(f"label: {a.get('label')!r} → {b.get('label')!r}")
    if a.get("type") != b.get("type"):
        changes.append(f"type: {a.get('type')!r} → {b.get('type')!r}")
    cfg_a, cfg_b = a.get("config") or {}, b.get("config") or {}
    keys = set(cfg_a) | set(cfg_b)
    for k in sorted(keys):
        if cfg_a.get(k) != cfg_b.get(k):
            changes.append(f"config.{k}: {cfg_a.get(k)!r} → {cfg_b.get(k)!r}")
    return changes


def diff_definitions(def_a: dict, def_b: dict) -> list[dict]:
    """Node/edge level diff — added / removed / modified, by id."""
    nodes_a = {n["id"]: n for n in (def_a.get("nodes") or [])}
    nodes_b = {n["id"]: n for n in (def_b.get("nodes") or [])}
    edges_a = {e["id"]: e for e in (def_a.get("edges") or [])}
    edges_b = {e["id"]: e for e in (def_b.get("edges") or [])}

    entries: list[dict] = []

    for nid in nodes_b.keys() - nodes_a.keys():
        n = nodes_b[nid]
        entries.append({"kind": "node", "change": "added", "id": nid, "label": _node_label(n), "before": None, "after": n, "field_changes": []})
    for nid in nodes_a.keys() - nodes_b.keys():
        n = nodes_a[nid]
        entries.append({"kind": "node", "change": "removed", "id": nid, "label": _node_label(n), "before": n, "after": None, "field_changes": []})
    for nid in nodes_a.keys() & nodes_b.keys():
        a, b = nodes_a[nid], nodes_b[nid]
        changes = _diff_node(a, b)
        if changes:
            entries.append({"kind": "node", "change": "modified", "id": nid, "label": _node_label(b), "before": a, "after": b, "field_changes": changes})

    for eid in edges_b.keys() - edges_a.keys():
        e = edges_b[eid]
        entries.append({"kind": "edge", "change": "added", "id": eid, "label": f"{e.get('sourceId')} → {e.get('targetId')}", "before": None, "after": e, "field_changes": []})
    for eid in edges_a.keys() - edges_b.keys():
        e = edges_a[eid]
        entries.append({"kind": "edge", "change": "removed", "id": eid, "label": f"{e.get('sourceId')} → {e.get('targetId')}", "before": e, "after": None, "field_changes": []})

    return entries


# ── Output shaping ───────────────────────────────────────────────

async def user_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    async with AsyncSessionLocal() as platform_db:  # User is a PlatformBase model — never the tenant-routed `db`
        result = await platform_db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
        return {row[0]: row[1] for row in result.all()}


def version_to_summary(v: PipelineVersion, is_current: bool, name_map: dict[int, str]) -> dict:
    return {
        "version_number": v.version_number,
        "is_current": is_current,
        "created_by": v.created_by,
        "created_by_name": name_map.get(v.created_by),
        "created_at": v.created_at,
        "change_summary": v.change_summary,
    }


def version_to_detail(v: PipelineVersion, is_current: bool, name_map: dict[int, str]) -> dict:
    defn = v.pipeline_definition or {}
    return {
        **version_to_summary(v, is_current, name_map),
        "nodes": defn.get("nodes", []),
        "edges": defn.get("edges", []),
        "settings": defn.get("settings", {}),
    }


def pipeline_to_out(p: Pipeline, name_map: dict[int, str]) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "current_version": p.current_version,
        "created_by": p.created_by,
        "created_by_name": name_map.get(p.created_by),
        "updated_by": p.updated_by,
        "updated_by_name": name_map.get(p.updated_by),
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }
