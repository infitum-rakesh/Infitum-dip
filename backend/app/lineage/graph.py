"""
Lineage graph construction — DERIVED, never stored. Per the explicit user
requirement ("Lineage must be generated from the actual pipeline
definition. Do not manually maintain lineage separately."), this module
builds a typed node/edge graph fresh, every call, from:

  - PipelineVersion.pipeline_definition — the real saved canvas
    ({nodes, edges, settings}), same JSON Transform Workspace/Pipeline
    Builder already reads and writes (models/pipeline.py). Node types
    come from frontend/src/components/pipeline/nodeTypes.js's catalogue —
    mirrored here in CANVAS_TYPE_TO_LINEAGE so the two never drift far
    apart (both are hand-maintained, but this side lists every canvas
    type explicitly so an unmapped type fails loud via .get(..., "DATASET")
    rather than silently misrepresenting it).
  - Connection (models/connection.py) — resolves a source/target node's
    connectionId into a real system type (POSTGRESQL, SFTP, REST, ...)
    for the DATABASE/FILE/SFTP/API lineage node above it.
  - ColumnMapping (models/column_mapping.py) — the real column-to-column
    mapping + transformation-expression store, joined in when a pipeline's
    source/target node's (connection, schema, object) matches a saved
    ColumnMapping row exactly (best-effort — a pipeline's Column Mapping
    canvas node also carries its own inline `mappings`, handled separately
    in column_lineage() below).

Because this always reads PipelineVersion (a specific, immutable version
row — versions are never overwritten, see Pipeline model docstring), a
graph built for version N is byte-for-byte reproducible forever and a
graph built for version N+1 can never retroactively change it — this IS
the "historical lineage never changes" requirement, for free, by
construction.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.column_mapping import ColumnMapping
from app.models.connection import Connection, ConnectionType
from app.monitoring import repository as mon_repo
from sqlalchemy import select

# Canvas node type -> lineage node category. See nodeTypes.js for the
# canonical list this mirrors (frontend/src/components/pipeline/nodeTypes.js).
CANVAS_TYPE_TO_LINEAGE = {
    "source": None,  # resolved dynamically from connectionId/uploadedFileId — see _classify_io_node()
    "target": None,
    "filter": "TRANSFORMATION", "select": "TRANSFORMATION", "rename": "TRANSFORMATION",
    "cast": "TRANSFORMATION", "sort": "TRANSFORMATION", "deduplicate": "TRANSFORMATION",
    "join": "TRANSFORMATION", "lookup": "TRANSFORMATION", "aggregate": "TRANSFORMATION",
    "calculated": "TRANSFORMATION", "conditional": "TRANSFORMATION", "mapping": "TRANSFORMATION",
    "doc_input": "DOCUMENT", "image_scanner": "TRANSFORMATION", "ocr_engine": "OCR",
    "ai_extraction": "AI_MODEL", "structured_dataset": "DATASET",
    "validation_step": "VALIDATION", "reconciliation_step": "RECONCILIATION",
    "notification": "TRANSFORMATION",
}

_DB_TYPES = {
    ConnectionType.SQL_SERVER, ConnectionType.POSTGRESQL, ConnectionType.MYSQL, ConnectionType.ORACLE,
    ConnectionType.SNOWFLAKE, ConnectionType.SAP, ConnectionType.FABRIC, ConnectionType.VERTICA,
    ConnectionType.SALESFORCE, ConnectionType.NETSUITE_SUITEQL, ConnectionType.NETSUITE_SOAP,
}
_FILE_TYPES = {ConnectionType.SFTP, ConnectionType.FTP, ConnectionType.LOCAL_FILE, ConnectionType.NETWORK_FILE}
_API_TYPES = {ConnectionType.REST, ConnectionType.SOAP}
_CLOUD_TYPES = {ConnectionType.AZURE, ConnectionType.AWS}


def _system_lineage_type(conn: Connection | None) -> str:
    if conn is None:
        return "FILE"
    ct = conn.connection_type
    if ct in _DB_TYPES:
        return "DATABASE"
    if ct in _FILE_TYPES:
        return "SFTP" if ct in (ConnectionType.SFTP, ConnectionType.FTP) else "FILE"
    if ct in _API_TYPES:
        return "API"
    if ct in _CLOUD_TYPES:
        return "FILE"
    return "DATABASE"


def _transformation_label(node: dict) -> str | None:
    """Best-effort human-readable transformation expression for an edge
    label, straight from the node's own real config — never fabricated,
    returns None (renders as a plain, unlabeled edge) for node types with
    no single expressive summary."""
    cfg = node.get("config") or {}
    t = node.get("type")
    if t == "filter":
        return cfg.get("condition") or None
    if t == "calculated":
        expr = cfg.get("expression")
        col = cfg.get("columnName")
        return f"{col} = {expr}" if col and expr else expr
    if t == "select":
        cols = cfg.get("columns")
        mode = cfg.get("mode", "include")
        return f"{mode.upper()}: {cols}" if cols else None
    if t == "rename":
        mappings = cfg.get("mappings") or []
        if mappings:
            return "; ".join(f"{m.get('from')}→{m.get('to')}" for m in mappings if m.get("from"))
        return None
    if t == "cast":
        castings = cfg.get("castings") or []
        if castings:
            return "; ".join(f"{c.get('column')}::{c.get('type')}" for c in castings if c.get("column"))
        return None
    if t == "join":
        return f"{cfg.get('joinType', 'inner').upper()} JOIN ON {cfg.get('leftKey')} = {cfg.get('rightKey')}" if cfg.get("leftKey") else None
    if t == "lookup":
        return f"LOOKUP {cfg.get('lookupDataset')} ON {cfg.get('lookupKey')}" if cfg.get("lookupKey") else None
    if t == "aggregate":
        return f"GROUP BY {cfg.get('groupBy')}" if cfg.get("groupBy") else None
    if t == "deduplicate":
        return f"DEDUP ON {cfg.get('keyColumns')}" if cfg.get("keyColumns") else None
    if t == "sort":
        return f"SORT BY {cfg.get('sortBy')} {cfg.get('direction', 'asc').upper()}" if cfg.get("sortBy") else None
    if t == "mapping":
        n = len(cfg.get("mappings") or [])
        return f"{n} column mapping(s)" if n else None
    return None


async def _resolve_connection(db: AsyncSession, connection_id: int | None) -> Connection | None:
    if not connection_id:
        return None
    return await db.get(Connection, connection_id)


async def build_pipeline_lineage(db: AsyncSession, pipeline_id: int, organization_id: int | None, version_number: int | None = None) -> dict | None:
    """Returns {pipeline, version_number, nodes, edges} or None if the
    pipeline/version doesn't exist or isn't in scope for this org."""
    pipeline = await mon_repo.get_pipeline(db, pipeline_id, organization_id)
    if pipeline is None:
        return None
    version = await mon_repo.get_pipeline_version(db, pipeline_id, version_number)
    if version is None:
        return None

    definition = version.pipeline_definition or {}
    canvas_nodes = {n["id"]: n for n in (definition.get("nodes") or [])}
    canvas_edges = definition.get("edges") or []

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(node_id: str, ntype: str, label: str, subtitle: str | None = None, meta: dict | None = None):
        if node_id in seen_ids:
            return
        seen_ids.add(node_id)
        nodes.append({"id": node_id, "type": ntype, "label": label, "subtitle": subtitle, "meta": meta or {}})

    def add_edge(source_id: str, target_id: str, label: str | None = None, edge_type: str = "FLOW"):
        edges.append({"id": f"e_{source_id}__{target_id}", "source": source_id, "target": target_id, "label": label, "edge_type": edge_type})

    pipeline_node_id = f"pipeline:{pipeline.id}"
    add_node(pipeline_node_id, "PIPELINE", pipeline.name, f"v{version.version_number}", {
        "pipeline_id": pipeline.id, "version_number": version.version_number,
    })

    for node in canvas_nodes.values():
        canvas_id = f"node:{node['id']}"
        ntype = node.get("type")
        label = node.get("label") or ntype

        if ntype in ("source", "target"):
            cfg = node.get("config") or {}
            conn = await _resolve_connection(db, cfg.get("connectionId"))
            sys_type = _system_lineage_type(conn)
            if conn is not None:
                sys_id = f"connection:{conn.id}"
                add_node(sys_id, sys_type, conn.name, conn.connection_type.value if hasattr(conn.connection_type, "value") else str(conn.connection_type),
                          {"connection_id": conn.id})
                object_label = cfg.get("object") or cfg.get("schema") or label
                obj_id = f"{canvas_id}:object"
                add_node(obj_id, "TABLE" if sys_type == "DATABASE" else sys_type, object_label,
                          f"{cfg.get('schema')}.{cfg.get('object')}" if cfg.get("schema") else cfg.get("object"),
                          {"connection_id": conn.id, "schema": cfg.get("schema"), "object": cfg.get("object")})
                if ntype == "source":
                    add_edge(sys_id, obj_id, edge_type="CONTAINS")
                    add_edge(obj_id, canvas_id, edge_type="FLOW")
                else:
                    add_edge(canvas_id, obj_id, edge_type="FLOW")
                    add_edge(obj_id, sys_id, edge_type="CONTAINS")
            elif cfg.get("uploadedFileId") or cfg.get("fileName"):
                file_label = cfg.get("fileName") or f"uploaded file #{cfg.get('uploadedFileId')}"
                file_id = f"{canvas_id}:file"
                add_node(file_id, "FILE", file_label, cfg.get("fileType"), {"uploaded_file_id": cfg.get("uploadedFileId")})
                if ntype == "source":
                    add_edge(file_id, canvas_id, edge_type="FLOW")
                else:
                    add_edge(canvas_id, file_id, edge_type="FLOW")
            add_node(canvas_id, "TARGET" if ntype == "target" else "DATASET", label, ntype, {"node_type": ntype, "config_summary": _io_summary(cfg)})
        else:
            lineage_type = CANVAS_TYPE_TO_LINEAGE.get(ntype, "DATASET")
            add_node(canvas_id, lineage_type, label, ntype, {"node_type": ntype})

        add_edge(pipeline_node_id, canvas_id, edge_type="DEFINES")

    for e in canvas_edges:
        src, tgt = e.get("sourceId"), e.get("targetId")
        if src not in canvas_nodes or tgt not in canvas_nodes:
            continue
        src_node = canvas_nodes[src]
        add_edge(f"node:{src}", f"node:{tgt}", label=_transformation_label(src_node), edge_type="TRANSFORMATION")

    return {"pipeline": {"id": pipeline.id, "name": pipeline.name}, "version_number": version.version_number, "nodes": nodes, "edges": edges}


def _io_summary(cfg: dict) -> str | None:
    parts = []
    if cfg.get("objectType"):
        parts.append(cfg["objectType"])
    if cfg.get("targetType"):
        parts.append(cfg["targetType"])
    if cfg.get("mode"):
        parts.append(f"mode={cfg['mode']}")
    return ", ".join(parts) or None


# ── Column-level lineage ──

MAPPING_TYPE_LABELS = {
    "direct": "DIRECT", "constant": "CONSTANT", "lookup": "LOOKUP", "transformed": "TRANSFORMED",
}


def _infer_mapping_type(row: dict) -> str:
    if row.get("constant_value") is not None:
        return "CONSTANT"
    if row.get("lookup"):
        return "LOOKUP"
    if row.get("transformation"):
        return "TRANSFORMED"
    if len(row.get("source_columns") or []) > 1:
        return "DERIVED"
    return "DIRECT"


async def get_column_mapping_lineage(db: AsyncSession, mapping_id: int, organization_id: int | None) -> dict | None:
    mapping = await db.get(ColumnMapping, mapping_id)
    if mapping is None:
        return None
    if organization_id is not None and mapping.organization_id not in (None, organization_id):
        return None
    source_conn = await db.get(Connection, mapping.source_connection_id)
    target_conn = await db.get(Connection, mapping.target_connection_id)
    columns = []
    for row in (mapping.mappings or []):
        columns.append({
            "source_system": source_conn.name if source_conn else None,
            "source_dataset": mapping.source_schema,
            "source_object": mapping.source_object,
            "source_columns": row.get("source_columns") or [],
            "transformation": row.get("transformation") or None,
            "target_system": target_conn.name if target_conn else None,
            "target_dataset": mapping.target_schema,
            "target_object": mapping.target_object,
            "target_column": row.get("target_column"),
            "mapping_type": _infer_mapping_type(row),
            "status": row.get("status"),
        })
    return {
        "mapping_id": mapping.id, "name": mapping.name,
        "source_connection": source_conn.name if source_conn else None,
        "source_object": f"{mapping.source_schema}.{mapping.source_object}" if mapping.source_schema else mapping.source_object,
        "target_connection": target_conn.name if target_conn else None,
        "target_object": f"{mapping.target_schema}.{mapping.target_object}" if mapping.target_schema else mapping.target_object,
        "columns": columns,
        "created_by": mapping.created_by, "updated_by": mapping.updated_by,
        "created_at": mapping.created_at, "updated_at": mapping.updated_at,
    }


async def list_column_mappings_for_search(db: AsyncSession, organization_id: int | None) -> list[ColumnMapping]:
    stmt = select(ColumnMapping)
    if organization_id is not None:
        stmt = stmt.where(ColumnMapping.organization_id == organization_id)
    return list((await db.execute(stmt)).scalars().all())
