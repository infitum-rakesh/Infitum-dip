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
from app.services import mapping_service
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
    "anomaly_detection_step": "ANOMALY_DETECTION",
    "notification": "TRANSFORMATION",
    # CM8 — the real Column Mapping <-> Pipeline Builder integration node
    # (distinct from the unrelated inline "mapping" node above). Splices
    # real column-to-column edges in build_pipeline_lineage() below, not
    # just a labeled pass-through edge.
    "column_mapping_step": "TRANSFORMATION",
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
    if t == "anomaly_detection_step":
        if cfg.get("jobId"):
            return f"ANOMALY CHECK (job #{cfg['jobId']})"
        methods = cfg.get("methods") or []
        return f"ANOMALY CHECK ({', '.join(methods)})" if methods else None
    if t == "column_mapping_step":
        mapping_id, mapping_version = cfg.get("mappingId"), cfg.get("mappingVersion")
        return f"COLUMN MAPPING (mapping #{mapping_id} v{mapping_version})" if mapping_id and mapping_version else None
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
            if ntype == "column_mapping_step":
                await _splice_column_mapping_lineage(db, node, canvas_id, organization_id, add_node, add_edge)

        add_edge(pipeline_node_id, canvas_id, edge_type="DEFINES")

    for e in canvas_edges:
        src, tgt = e.get("sourceId"), e.get("targetId")
        if src not in canvas_nodes or tgt not in canvas_nodes:
            continue
        src_node = canvas_nodes[src]
        add_edge(f"node:{src}", f"node:{tgt}", label=_transformation_label(src_node), edge_type="TRANSFORMATION")

    return {"pipeline": {"id": pipeline.id, "name": pipeline.name}, "version_number": version.version_number, "nodes": nodes, "edges": edges}


async def _splice_column_mapping_lineage(db: AsyncSession, node: dict, canvas_id: str, organization_id: int | None, add_node, add_edge) -> None:
    """CM8 — the genuinely new piece: turns a column_mapping_step node
    from a single opaque TRANSFORMATION box into real column-to-column
    edges, using the EXACT (mappingId, mappingVersion) this node is
    pinned to — never the mapping's current version, which may have
    moved on since this pipeline version was saved (see
    get_column_mapping_lineage()'s version_number param). Each mapping
    row becomes: source column node(s) -> canvas node (CONSUMES) ->
    target column node (PRODUCES), plus a direct source-column ->
    target-column COLUMN_TRANSFORM edge labeled with the real rule
    (transformation expression / lookup / constant / rename) so "Source
    Column -> Mapping -> Target Column" is an actual traceable path, not
    an inferred one. A constant_value row (no source column at all) still
    gets its target column node, just with no incoming COLUMN_TRANSFORM
    edge — accurately representing that it has no source-side lineage."""
    cfg = node.get("config") or {}
    mapping_id, mapping_version = cfg.get("mappingId"), cfg.get("mappingVersion")
    if not mapping_id or not mapping_version:
        return
    lineage = await get_column_mapping_lineage(db, mapping_id, organization_id, version_number=mapping_version)
    if lineage is None:
        return
    for col in lineage["columns"]:
        target = col.get("target_column")
        if not target:
            continue
        target_col_id = f"{canvas_id}:col:{target}"
        add_node(target_col_id, "COLUMN", target, col["mapping_type"], {"node_type": "column", "parent": canvas_id, "mapping_type": col["mapping_type"]})
        add_edge(canvas_id, target_col_id, edge_type="PRODUCES")
        edge_label = col.get("transformation") or col["mapping_type"]
        for src in col.get("source_columns") or []:
            src_col_id = f"{canvas_id}:col:src:{src}"
            add_node(src_col_id, "COLUMN", src, "SOURCE COLUMN", {"node_type": "column", "parent": canvas_id})
            add_edge(src_col_id, canvas_id, edge_type="CONSUMES")
            add_edge(src_col_id, target_col_id, label=edge_label, edge_type="COLUMN_TRANSFORM")


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


async def get_column_mapping_lineage(
    db: AsyncSession, mapping_id: int, organization_id: int | None, version_number: int | None = None,
) -> dict | None:
    """version_number — CM8: reads the EXACT immutable ColumnMappingVersion
    row (mappings/source_*/target_* all snapshotted on that row, see
    models/column_mapping_version.py), not the retired
    ColumnMapping.mappings header column, which CM2's create_new_version()
    no longer updates past v1. Omitting version_number resolves the same
    "effective" version the standalone module's own UI shows (current
    published, else latest) — used by Lineage's standalone Column Mapping
    lookup; a pipeline's column_mapping_step node always passes its own
    pinned version explicitly (see build_pipeline_lineage() below), since
    that's the version that ACTUALLY ran, not whatever the mapping's
    current version happens to be today."""
    mapping = await db.get(ColumnMapping, mapping_id)
    if mapping is None:
        return None
    if organization_id is not None and mapping.organization_id not in (None, organization_id):
        return None
    version = (
        await mapping_service.get_version(db, mapping_id, version_number) if version_number is not None
        else await mapping_service.get_effective_mappings(db, mapping)
    )
    if version is None:
        return None
    source_conn = await db.get(Connection, version.source_connection_id)
    target_conn = await db.get(Connection, version.target_connection_id)
    columns = []
    for row in (version.mappings or []):
        columns.append({
            "source_system": source_conn.name if source_conn else None,
            "source_dataset": version.source_schema,
            "source_object": version.source_object,
            "source_columns": row.get("source_columns") or [],
            "transformation": row.get("transformation") or None,
            "target_system": target_conn.name if target_conn else None,
            "target_dataset": version.target_schema,
            "target_object": version.target_object,
            "target_column": row.get("target_column"),
            "mapping_type": _infer_mapping_type(row),
            "status": row.get("status"),
        })
    return {
        "mapping_id": mapping.id, "name": mapping.name, "version_number": version.version_number, "version_status": version.status,
        "source_connection": source_conn.name if source_conn else None,
        "source_object": f"{version.source_schema}.{version.source_object}" if version.source_schema else version.source_object,
        "target_connection": target_conn.name if target_conn else None,
        "target_object": f"{version.target_schema}.{version.target_object}" if version.target_schema else version.target_object,
        "columns": columns,
        "created_by": mapping.created_by, "updated_by": mapping.updated_by,
        "created_at": mapping.created_at, "updated_at": mapping.updated_at,
    }


async def list_column_mappings_for_search(db: AsyncSession, organization_id: int | None) -> list[ColumnMapping]:
    stmt = select(ColumnMapping)
    if organization_id is not None:
        stmt = stmt.where(ColumnMapping.organization_id == organization_id)
    return list((await db.execute(stmt)).scalars().all())


# ── Invoice Intelligence lineage (INV22) ──

# The exact extraction-field-name -> ApInvoice-column-name lookup
# services/invoice/invoice_processing_service.py's materialize_invoice()
# already uses (fmap.get("po_number") -> invoice.purchase_order, etc.) —
# reused verbatim here rather than re-declared, so this graph can never
# drift from what actually populated the row it's describing.
INVOICE_HEADER_FIELD_MAP = {
    "invoice_number": "invoice_number", "invoice_date": "invoice_date", "due_date": "due_date",
    "vendor_name": "vendor_name", "po_number": "purchase_order", "currency": "currency",
    "payment_terms": "payment_terms", "subtotal": "subtotal", "tax_amount": "tax",
    "discount_amount": "discount", "shipping_amount": "freight", "total_amount": "total",
}


async def build_invoice_lineage(db: AsyncSession, invoice_id: int, organization_id: int | None) -> dict | None:
    """Returns {invoice, nodes, edges} or None if the invoice doesn't exist
    or isn't in scope for this org. Traces the REAL chain this specific
    ApInvoice actually went through: Document -> OCR -> AI/Custom-Engine
    extraction -> per-field normalization -> ApInvoice header columns ->
    line items -> validation rule outcomes -> ERP field mapping -> ERP
    posting attempt(s) -> reconciliation — each edge backed by an actual
    row (ApInvoiceValidationResult, ApInvoiceErpMapping,
    ApInvoiceErpTransaction, ApInvoiceReconciliation), never inferred.
    Stages that genuinely didn't happen yet (not posted, no reconciliation
    run) are simply absent from the graph rather than faked."""
    from app.models.document import Document
    from app.models.invoice_intelligence.core import ApInvoice, ApInvoiceLine
    from app.models.invoice_intelligence.erp import ApInvoiceErpTransaction, ApInvoiceReconciliation
    from app.models.invoice_intelligence.rules import ApInvoiceValidationResult
    from app.services.invoice import invoice_erp_mapping_service
    from app.services.invoice.invoice_inbox_service import get_invoice

    invoice = await get_invoice(db, invoice_id, organization_id)
    if invoice is None:
        return None
    doc = await db.get(Document, invoice.document_id)

    nodes: list[dict] = []
    edges: list[dict] = []
    seen_ids: set[str] = set()

    def add_node(node_id: str, ntype: str, label: str, subtitle: str | None = None, meta: dict | None = None):
        if node_id in seen_ids:
            return
        seen_ids.add(node_id)
        nodes.append({"id": node_id, "type": ntype, "label": label, "subtitle": subtitle, "meta": meta or {}})

    def add_edge(source_id: str, target_id: str, label: str | None = None, edge_type: str = "FLOW"):
        edges.append({"id": f"e_{source_id}__{target_id}__{len(edges)}", "source": source_id, "target": target_id, "label": label, "edge_type": edge_type})

    invoice_node_id = f"invoice:{invoice.id}"
    add_node(invoice_node_id, "INVOICE", invoice.invoice_number or f"Invoice #{invoice.id}", invoice.vendor_name, {
        "invoice_id": invoice.id, "status": invoice.status.value, "final_decision": invoice.final_decision.value if invoice.final_decision else None,
        "total": invoice.total, "currency": invoice.currency,
    })

    ai_model_id = None
    if doc is not None:
        doc_id = f"document:{doc.id}"
        add_node(doc_id, "DOCUMENT", doc.filename, doc.doc_type.value if hasattr(doc.doc_type, "value") else str(doc.doc_type), {"document_id": doc.id})

        ocr_id = f"{doc_id}:ocr"
        if doc.ocr_result:
            regions = doc.ocr_result.get("regions") or []
            add_node(ocr_id, "OCR", "OCR", f"{len(regions)} region(s), {doc.ocr_result.get('confidence')}% confidence", {"document_id": doc.id})
        else:
            add_node(ocr_id, "OCR", "OCR", "Skipped — no OCR result on this document", {"document_id": doc.id, "skipped": True})
        add_edge(doc_id, ocr_id, edge_type="FLOW")

        ai_model_id = f"{doc_id}:ai_model"
        if doc.extraction_model == "CUSTOM_ENGINE":
            add_node(ai_model_id, "AI_MODEL", "Custom Extraction Engine (no AI)", "Deterministic rule-based fallback", {"document_id": doc.id})
        elif doc.extraction_model:
            add_node(ai_model_id, "AI_MODEL", doc.extraction_model, "AI Provider connection", {"document_id": doc.id})
        else:
            add_node(ai_model_id, "AI_MODEL", "Unknown", "extraction_model not recorded for this document (processed before this tracking existed, or extraction failed)", {"document_id": doc.id, "unknown": True})
        add_edge(ocr_id, ai_model_id, edge_type="FLOW")

        extracted_fields = {f.get("name"): f for f in ((doc.extraction_result or {}).get("fields") or []) if f.get("type") != "table"}
        for extraction_name, invoice_col in INVOICE_HEADER_FIELD_MAP.items():
            col_value = getattr(invoice, invoice_col, None)
            if col_value is None:
                continue
            invoice_col_id = f"{invoice_node_id}:col:{invoice_col}"
            add_node(invoice_col_id, "COLUMN", invoice_col, str(col_value)[:80], {"node_type": "column", "parent": invoice_node_id})
            src = extracted_fields.get(extraction_name)
            if src is not None:
                src_field_id = f"{doc_id}:field:{extraction_name}"
                add_node(src_field_id, "COLUMN", extraction_name, str(src.get("value"))[:80] if src.get("value") is not None else None,
                          {"node_type": "column", "parent": ai_model_id, "confidence": src.get("confidence")})
                add_edge(ai_model_id, src_field_id, edge_type="PRODUCES")
                add_edge(src_field_id, invoice_col_id, label="normalize", edge_type="COLUMN_TRANSFORM")
            add_edge(invoice_col_id, invoice_node_id, edge_type="PRODUCES")

    # Line items — real ApInvoiceLine rows, kept at row granularity (not
    # further split field-by-field — the line_items table extraction
    # already collapses into these rows, see invoice_processing_service).
    lines_stmt = select(ApInvoiceLine).where(ApInvoiceLine.invoice_id == invoice.id).order_by(ApInvoiceLine.line_number)
    lines = list((await db.execute(lines_stmt)).scalars().all())
    for line in lines:
        line_id = f"{invoice_node_id}:line:{line.id}"
        add_node(line_id, "DATASET", f"Line {line.line_number}", line.description, {
            "line_id": line.id, "quantity": line.quantity, "unit_price": line.unit_price, "line_total": line.line_total,
        })
        add_edge(invoice_node_id, line_id, edge_type="CONTAINS")
        if ai_model_id:
            add_edge(ai_model_id, line_id, edge_type="PRODUCES")

    # Validation — real ApInvoiceValidationResult rows for this invoice's
    # actual processing run (rule_version pinned, see ApInvoice.rule_version).
    val_stmt = select(ApInvoiceValidationResult).where(ApInvoiceValidationResult.invoice_id == invoice.id).order_by(ApInvoiceValidationResult.id)
    val_results = list((await db.execute(val_stmt)).scalars().all())
    for v in val_results:
        v_id = f"{invoice_node_id}:validation:{v.id}"
        add_node(v_id, "VALIDATION", v.category.value, v.message, {"result": v.result.value, "rule_id": v.rule_id, "rule_version": v.rule_version})
        add_edge(invoice_node_id, v_id, label=v.result.value, edge_type="VALIDATED_BY")

    # ERP mapping + posting + reconciliation — only present once an ERP
    # connection is actually attached to this invoice (erp_connection_id
    # is set the moment invoice_erp_service makes its first real attempt).
    erp_field_ids: list[str] = []
    if invoice.erp_connection_id:
        conn = await db.get(Connection, invoice.erp_connection_id)
        erp_system_id = f"erp:{invoice.erp_connection_id}"
        add_node(erp_system_id, "API", conn.name if conn else f"ERP Connection #{invoice.erp_connection_id}",
                  conn.connection_type.value if conn and hasattr(conn.connection_type, "value") else None, {"connection_id": invoice.erp_connection_id})

        mappings = await invoice_erp_mapping_service.get_current_mapping(db, organization_id, invoice.erp_connection_id)
        for m in mappings:
            if m.is_line_mapping:
                continue  # line-level ERP mapping omitted here — header mapping already demonstrates the real splice; avoids N x line-count node blowup
            erp_field_id = f"{erp_system_id}:field:{m.erp_field}:v{m.version}"
            add_node(erp_field_id, "COLUMN", m.erp_field, m.transform_config.get("type") if m.transform_config else None, {"node_type": "column", "parent": erp_system_id})
            add_edge(erp_field_id, erp_system_id, edge_type="CONTAINS")
            erp_field_ids.append(erp_field_id)
            if m.canonical_field.startswith("__fixed__"):
                continue  # constant value — genuinely no source-side lineage, matches _splice_column_mapping_lineage's own constant handling
            invoice_col_id = f"{invoice_node_id}:col:{m.canonical_field}"
            if invoice_col_id in seen_ids:
                add_edge(invoice_col_id, erp_field_id, label=m.transform_config.get("type") if m.transform_config else "mapped", edge_type="COLUMN_TRANSFORM")

        txn_stmt = select(ApInvoiceErpTransaction).where(ApInvoiceErpTransaction.invoice_id == invoice.id).order_by(ApInvoiceErpTransaction.attempt_number)
        txns = list((await db.execute(txn_stmt)).scalars().all())
        last_txn_id = None
        for t in txns:
            t_id = f"invoice:{invoice.id}:erp_txn:{t.id}"
            add_node(t_id, "ERP_TRANSACTION", f"Attempt #{t.attempt_number}", t.status.value, {
                "erp_transaction_id": t.erp_transaction_id, "status": t.status.value, "error_code": t.error_code, "is_retryable": t.is_retryable,
            })
            for erp_field_id in erp_field_ids:
                add_edge(erp_field_id, t_id, edge_type="CONSUMES")
            add_edge(invoice_node_id, t_id, edge_type="POSTS_TO")
            last_txn_id = t_id

        if last_txn_id:
            recon_stmt = select(ApInvoiceReconciliation).where(ApInvoiceReconciliation.invoice_id == invoice.id).order_by(ApInvoiceReconciliation.id.desc())
            recon = (await db.execute(recon_stmt)).scalars().first()
            if recon is not None:
                r_id = f"invoice:{invoice.id}:reconciliation:{recon.id}"
                mismatches = [c for c in (recon.field_comparisons or []) if c.get("result") == "MISMATCH"]
                add_node(r_id, "RECONCILIATION", "Reconciliation", recon.status.value, {
                    "status": recon.status.value, "field_count": len(recon.field_comparisons or []), "mismatch_count": len(mismatches),
                })
                add_edge(last_txn_id, r_id, label=recon.status.value, edge_type="RECONCILED_BY")

    return {"invoice": {"id": invoice.id, "invoice_number": invoice.invoice_number, "vendor_name": invoice.vendor_name}, "nodes": nodes, "edges": edges}
