"""
pipeline_job_executor — real, in-memory, pandas-based DAG executor for a
FULL saved pipeline (Transform Workspace / Pipeline Builder canvas):
source -> transform/enrich/expression -> quality -> target, walking every
node in the saved {nodes, edges} definition in topological order.

This is a SEPARATE, smaller-scope engine from execution/pipeline_executor.py
(which does bounded-memory, checkpointed, resumable CHUNKED extraction of
ONE Source Dataset node to a staging file — built for very large single
tables). This engine instead runs an entire multi-node graph end-to-end
for real, in one in-memory pandas DataFrame per source. For SQL-database
connections (Postgres/MySQL/SQL Server/Oracle/Snowflake/SAP/Vertica/
Fabric/NetSuite SuiteQL) a TABLE/VIEW/QUERY source now reads EXHAUSTIVELY
via the same chunked open_sql_stream()/fetch_chunk() primitive
execution/worker.py's "Run Extraction" engine uses — no silent 50K-row
truncation — up to SOURCE_ROW_HARD_CAP as an in-memory safety ceiling
(this engine still isn't a distributed/spilling engine, so *some* ceiling
has to exist, but it's 40x the old cap). Connectors that don't support
streaming (Salesforce, generic SOAP/REST, files) fall back to the older
single-shot bounded preview, capped at SOURCE_ROW_CAP — every truncation,
at either cap, is disclosed via the source node's log line, never applied
silently.

Node types executed for real:
  source              TABLE/VIEW/QUERY on a saved connection — real
                       exhaustive chunked read for SQL databases (see
                       _stream_all_rows), or the connector's bounded
                       preview_object/preview_sql for connectors that
                       don't support streaming — or an uploaded
                       PipelineSourceFile (CSV/TSV/Excel/JSON read
                       directly with pandas).
  filter, select, rename, cast, sort, deduplicate, calculated,
  conditional, mapping, aggregate                 real pandas transforms.
  join, lookup         real pandas merge — the right-hand dataset is
                       resolved as a table by that name on the pipeline's
                       PRIMARY source connection (the node config has no
                       connection picker of its own; see module note
                       below), read via the same bounded preview path.
  validation_step      config only carries {rulesetName, failAction,
                       passThreshold} — no inline rule definitions and no
                       link to a real saved ValidationRuleSet id, so this
                       does NOT invoke services/validation_service.py
                       (that engine runs rules directly against a live
                       connection+object via SQL, a different data model
                       than this in-memory DataFrame). Runs a real, generic
                       row-completeness check instead (percentage of rows
                       with zero null values) against passThreshold —
                       disclosed as such in the job log, not presented as
                       "rulesetName's actual rules".
  reconciliation_step   real row-count comparison between the current
                       DataFrame and compareWith resolved as a table on
                       the primary source connection, within `tolerance`.
  target                targetType 'dataset' writes via BulkWriter to a
                       DB connection — the node's own connectionId if
                       set, else falling back to the pipeline's primary
                       source connection (kept only for pipelines saved
                       before this node had its own picker); targetType
                       'file' writes under pipeline_outputs/ with pandas;
                       targetType 'sftp' serializes the same way and
                       uploads via the node's own SFTP connection (real
                       paramiko putfo — connectors/sftp/client.py);
                       targetType 'api' POSTs/PUTs/PATCHes the DataFrame
                       as batched JSON to the node's own REST connection
                       (connectors/api/rest.py's send_records() — same
                       auth/retry machinery fetch_api() uses on the
                       source side). dataset/sftp/api targets all have
                       their own connection picker; only 'file' doesn't
                       (it writes straight to local disk, no connection
                       needed).
  notification           only channel == 'webhook' actually sends (a real
                       HTTP POST via httpx); every other channel is
                       logged as not wired to a real transport here.

NOT executed for real — logged as a disclosed skip, node status
'skipped', data passes through unchanged: doc_input, image_scanner,
ocr_engine, ai_extraction, structured_dataset (the canvas' Document
Intelligence node group — that pipeline runs in the separate Document
Intelligence wizard, see pages/document-intelligence/*, which now has its
own real OCR + AI extraction; it isn't invoked from here).

Same in-process/asyncio.create_task, non-distributed limitation as
execution/pipeline_executor.py — an API process restart loses the
in-flight task. Unlike that module, a pipeline job has no per-row
checkpoint to resume from (it's a much shorter, in-memory run), so a job
interrupted this way is left RUNNING and must be re-run from Pipeline
Builder — disclosed, not silently retried or faked as resumed.
"""
from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.connectors.base import ConnectorError, ConnectorUnsupportedError
from app.database.connection import AsyncSessionLocal
from app.loading import scd
from app.loading.writers.base import write_batch_with_fallback
from app.loading.writers.registry import get_bulk_writer
from app.models.connection import Connection
from app.models.pipeline import PipelineVersion
from app.models.pipeline_job import JobStatus, PipelineJob, PipelineJobLog
from app.models.rejected_record import RejectSourceType
from app.services import pipeline_file_service, reject_service
from app.services.connection_service import build_connector, get_connection

SOURCE_ROW_CAP = 50_000  # bounded single-shot preview cap — only still used as a
# fallback for connectors that don't support open_sql_stream (Salesforce, generic
# SOAP/REST, files); see _stream_all_rows below for the real path.
SOURCE_ROW_HARD_CAP = 2_000_000  # in-memory safety ceiling for the streamed path —
# this engine loads a whole source into one pandas DataFrame (it isn't a
# spilling/distributed engine), so some ceiling has to exist, but this one is
# 40x SOURCE_ROW_CAP and disclosed via the same WARN log line if it's ever hit.
SOURCE_STREAM_CHUNK_SIZE = 20_000
OUTPUT_BASE_PATH = "pipeline_outputs"  # relative to the backend process cwd

DI_NODE_TYPES = {"doc_input", "image_scanner", "ocr_engine", "ai_extraction", "structured_dataset"}

_running_tasks: dict[int, asyncio.Task] = {}
_cancelled: set[int] = set()


class NodeExecutionError(Exception):
    """A node failed in a way that should stop the whole job."""


def is_running(job_id: int) -> bool:
    return job_id in _running_tasks


def is_cancelled(job_id: int) -> bool:
    return job_id in _cancelled


def request_cancel(job_id: int) -> bool:
    if job_id not in _running_tasks:
        return False
    _cancelled.add(job_id)
    return True


def start_job(job_id: int) -> None:
    """Fire-and-forget — the API route that calls this does not (and must
    not) await it; the frontend polls GET /pipeline-jobs/{id} and .../logs."""
    task = asyncio.create_task(_execute(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda _t: _running_tasks.pop(job_id, None))


# ── Logging / stats helpers ─────────────────────────────────────────

async def _log(job_id: int, level: str, message: str, node_id: str | None = None) -> None:
    async with AsyncSessionLocal() as db:
        db.add(PipelineJobLog(job_id=job_id, level=level, node_id=node_id, message=str(message)[:2000]))
        await db.commit()


async def _update_node_stats(job_id: int, node_id: str, patch: dict) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(PipelineJob, job_id)
        stats = dict(job.node_stats or {})
        stats[node_id] = {**stats.get(node_id, {}), **patch}
        job.node_stats = stats
        flag_modified(job, "node_stats")
        await db.commit()


async def _finish(job_id: int, status: JobStatus, *, error_message: str | None = None, records_out: int | None = None) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(PipelineJob, job_id)
        job.status = status
        job.completed_at = datetime.now(timezone.utc)
        if error_message is not None:
            job.error_message = error_message
        if records_out is not None:
            job.records_out = records_out
        await db.commit()

    # Monitoring integration seam — SLA evaluation, alert rules, and
    # auto-Incident-on-failure all hang off this one call. Runs in its own
    # session and never raises (see on_job_finished's own try/except), so a
    # monitoring bug can never turn a real pipeline result into a failure.
    from app.monitoring.service import on_job_finished
    await on_job_finished(job_id)


async def _fail(job_id: int, message: str) -> None:
    await _log(job_id, "ERROR", message)
    await _finish(job_id, JobStatus.FAILED, error_message=message[:500])


# ── Topological sort ─────────────────────────────────────────────

def _topo_sort(nodes: dict[str, dict], edges: list[dict]) -> list[str]:
    in_degree = {nid: 0 for nid in nodes}
    adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in edges:
        src, tgt = e.get("sourceId"), e.get("targetId")
        if src not in nodes or tgt not in nodes:
            continue
        adj[src].append(tgt)
        in_degree[tgt] = in_degree.get(tgt, 0) + 1

    queue = [nid for nid, d in in_degree.items() if d == 0]
    order: list[str] = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for nxt in adj.get(nid, []):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(nodes):
        raise ValueError("Pipeline graph has a cycle — cannot determine an execution order.")
    return order


# ── Condition normalizer: the config UI writes SQL-ish conditions
#    ("AND"/"OR"/"!="), pandas.query()/eval() want lowercase and/or/not
#    and "!=" already works — this only rewrites the boolean keywords. ──

def _normalize_expr(expr: str) -> str:
    expr = re.sub(r"\bAND\b", "and", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bOR\b", "or", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bNOT\b", "not", expr, flags=re.IGNORECASE)
    return expr


# ── Connection / connector resolution ───────────────────────────

async def _resolve_connector(connection_id: int, organization_id: int | None, cache: dict[int, tuple]):
    if connection_id in cache:
        return cache[connection_id]
    async with AsyncSessionLocal() as db:
        conn = await get_connection(db, connection_id, organization_id)
        if conn is None:
            raise NodeExecutionError(f"Connection {connection_id} was not found or no longer belongs to this organization.")
        connector = build_connector(conn)
        cache[connection_id] = (conn, connector)
        return conn, connector


# ── Node executors ─────────────────────────────────────────────

async def _stream_all_rows(connector, sql: str, parameters: dict) -> tuple[list[list], list[str], bool]:
    """Real, chunked, exhaustive read of a SELECT via the same
    open_sql_stream()/fetch_chunk() primitive execution/worker.py's "Run
    Extraction" engine already uses for genuinely unbounded reads — this
    replaces the old single-shot preview_object()/preview_sql() call,
    which opened one cursor, called fetchmany() exactly ONCE, and threw
    the rest of the real result set away (the actual cause of the
    "only pulls the first 50,000 rows" bug). Raises ConnectorUnsupportedError
    for connectors that don't implement streaming (Salesforce, generic
    SOAP/REST, files) — callers fall back to the old bounded preview path
    for those. Still enforces SOURCE_ROW_HARD_CAP as a safety ceiling since
    this engine loads the whole source into one in-memory pandas
    DataFrame — but that ceiling is far above SOURCE_ROW_CAP, and hitting
    it is disclosed via `truncated` exactly like the old cap was, never
    applied silently."""
    stream = await asyncio.to_thread(connector.open_sql_stream, sql, parameters or {})
    try:
        columns = list(stream.columns)
        rows: list[list] = []
        truncated = False
        while True:
            chunk = await asyncio.to_thread(stream.fetch_chunk, SOURCE_STREAM_CHUNK_SIZE)
            if not chunk:
                break
            rows.extend([r.get(c) for c in columns] for r in chunk)
            if len(rows) >= SOURCE_ROW_HARD_CAP:
                del rows[SOURCE_ROW_HARD_CAP:]
                # peek one more row to know whether the cap actually cut anything off
                extra = await asyncio.to_thread(stream.fetch_chunk, 1)
                truncated = bool(extra)
                break
            if len(chunk) < SOURCE_STREAM_CHUNK_SIZE:
                break
        return rows, columns, truncated
    finally:
        await asyncio.to_thread(stream.close)


async def _run_source(job_id, node_id, cfg, organization_id, cache):
    uploaded_file_id = cfg.get("uploadedFileId")
    if uploaded_file_id:
        async with AsyncSessionLocal() as db:
            record = await pipeline_file_service.get_by_id(db, uploaded_file_id, organization_id)
        if record is None:
            raise NodeExecutionError(f"Uploaded file {uploaded_file_id} was not found.")
        content = Path(record.storage_path).read_bytes()
        ext = record.extension
        try:
            if ext in ("csv", "tsv", "txt"):
                df = pd.read_csv(io.BytesIO(content), sep=record.delimiter or ",")
            elif ext in ("xlsx", "xls"):
                df = pd.read_excel(io.BytesIO(content), sheet_name=record.selected_sheet or 0)
            elif ext == "json":
                df = pd.read_json(io.BytesIO(content))
            elif ext == "jsonl":
                df = pd.read_json(io.BytesIO(content), lines=True)
            else:
                raise NodeExecutionError(f"Uploaded file type '.{ext}' isn't readable by the pipeline engine.")
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError(f"Could not read uploaded file '{record.filename}': {exc}") from exc
        if len(df) > SOURCE_ROW_CAP:
            await _log(job_id, "WARN", f"Uploaded file has {len(df)} rows — truncated to the first {SOURCE_ROW_CAP} (this engine's per-source cap).", node_id)
            df = df.head(SOURCE_ROW_CAP)
        return df, None

    connection_id = cfg.get("connectionId")
    if not connection_id:
        raise NodeExecutionError("Source Dataset node has no connection and no uploaded file configured.")
    conn, connector = await _resolve_connector(connection_id, organization_id, cache)

    cap_note = SOURCE_ROW_CAP
    try:
        if cfg.get("objectType") == "API":
            endpoint = cfg.get("apiEndpoint")
            if not endpoint:
                raise NodeExecutionError("API source node has no endpoint configured.")
            result = await asyncio.to_thread(
                connector.fetch_api,
                endpoint=endpoint, method=cfg.get("apiMethod") or "GET",
                query_params={p["name"]: p["value"] for p in cfg.get("apiQueryParams", []) if p.get("name")},
                headers={h["name"]: h["value"] for h in cfg.get("apiHeaders", []) if h.get("name")},
                body=cfg.get("apiBody") or None,
                json_path=cfg.get("apiJsonPath") or None,
                pagination=cfg.get("apiPagination") or {"style": "none"},
                limit=SOURCE_ROW_CAP,
                rate_limit_delay=float(cfg.get("apiRateLimitDelay") or 0),
            )
            columns, rows, truncated = result.columns, result.rows, result.truncated
        elif cfg.get("customSql"):
            params = {p["name"]: p["value"] for p in cfg.get("parameters", []) if p.get("name")}
            try:
                rows, columns, truncated = await _stream_all_rows(connector, cfg["customSql"], params)
                cap_note = SOURCE_ROW_HARD_CAP
            except ConnectorUnsupportedError:
                result = await asyncio.to_thread(connector.preview_sql, cfg["customSql"], params, SOURCE_ROW_CAP)
                columns, rows, truncated = result.columns, result.rows, result.truncated
        else:
            object_name = cfg.get("object")
            if not object_name:
                raise NodeExecutionError("Source Dataset node has no object/table selected.")
            schema = cfg.get("schema") or None
            quote_object = getattr(connector, "quote_object", None)
            streamed = False
            if quote_object is not None:
                try:
                    select_sql = f"SELECT * FROM {quote_object(object_name, schema)}"
                    rows, columns, truncated = await _stream_all_rows(connector, select_sql, {})
                    cap_note = SOURCE_ROW_HARD_CAP
                    streamed = True
                except ConnectorUnsupportedError:
                    pass
            if not streamed:
                result = await asyncio.to_thread(connector.preview_object, object_name, schema, SOURCE_ROW_CAP)
                columns, rows, truncated = result.columns, result.rows, result.truncated
    except ConnectorError as exc:
        raise NodeExecutionError(connector._scrub(str(exc))) from exc

    if truncated:
        await _log(job_id, "WARN", f"Source has more than {cap_note} rows — truncated to the first {cap_note} (this engine's per-source cap).", node_id)
    df = pd.DataFrame(rows, columns=columns)
    return df, connection_id


def _run_filter(df, cfg):
    df = df if df is not None else pd.DataFrame()
    condition = (cfg.get("condition") or "").strip()
    if not condition:
        return df
    try:
        return df.query(_normalize_expr(condition), engine="python")
    except Exception as exc:
        raise NodeExecutionError(f"Filter condition failed: {exc}") from exc


def _run_select(df, cfg):
    df = df if df is not None else pd.DataFrame()
    cols = [c.strip() for c in (cfg.get("columns") or "").split(",") if c.strip()]
    if not cols:
        return df
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise NodeExecutionError(f"Column(s) not found: {', '.join(missing)}")
    if cfg.get("mode") == "exclude":
        return df.drop(columns=cols)
    return df[cols]


def _run_rename(df, cfg):
    df = df if df is not None else pd.DataFrame()
    mapping = {m["from"]: m["to"] for m in (cfg.get("mappings") or []) if m.get("from") and m.get("to")}
    return df.rename(columns=mapping)


_CAST_DTYPE = {"string": "string", "number": "float64", "boolean": "boolean"}


def _run_cast(df, cfg):
    df = df if df is not None else pd.DataFrame()
    df = df.copy()
    for c in cfg.get("castings") or []:
        col, typ = c.get("column"), c.get("type")
        if not col or col not in df.columns:
            continue
        try:
            if typ == "date":
                df[col] = pd.to_datetime(df[col], errors="coerce")
            else:
                df[col] = df[col].astype(_CAST_DTYPE.get(typ, "string"))
        except Exception as exc:
            raise NodeExecutionError(f"Could not cast column '{col}' to {typ}: {exc}") from exc
    return df


def _run_sort(df, cfg):
    df = df if df is not None else pd.DataFrame()
    col = cfg.get("sortBy")
    if not col:
        return df
    if col not in df.columns:
        raise NodeExecutionError(f"Sort column '{col}' not found.")
    return df.sort_values(col, ascending=cfg.get("direction", "asc") != "desc")


def _run_dedup(df, cfg):
    df = df if df is not None else pd.DataFrame()
    keys = [c.strip() for c in (cfg.get("keyColumns") or "").split(",") if c.strip()]
    keep = "first" if cfg.get("keepFirst", True) else "last"
    return df.drop_duplicates(subset=keys or None, keep=keep)


def _run_calculated(df, cfg):
    df = df if df is not None else pd.DataFrame()
    col, expr = cfg.get("columnName"), (cfg.get("expression") or "").strip()
    if not col or not expr:
        return df
    df = df.copy()
    try:
        df[col] = df.eval(_normalize_expr(expr), engine="python")
    except Exception as exc:
        raise NodeExecutionError(f"Calculated column '{col}' expression failed: {exc}") from exc
    return df


def _run_conditional(df, cfg):
    df = df if df is not None else pd.DataFrame()
    conditions = cfg.get("conditions") or []
    if not conditions:
        return df
    df = df.copy()
    result = pd.Series([None] * len(df), index=df.index, dtype="object")
    for c in reversed(conditions):  # first rule in the list wins — apply last-to-first so earlier overwrites later
        when, then = c.get("when"), c.get("then")
        if not when:
            continue
        try:
            mask = df.eval(_normalize_expr(when), engine="python")
        except Exception as exc:
            raise NodeExecutionError(f"Conditional rule '{when}' failed: {exc}") from exc
        result = result.mask(mask, then)
    df["conditional_result"] = result
    return df


def _run_mapping(df, cfg):
    df = df if df is not None else pd.DataFrame()
    mapping = {m["source"]: m["target"] for m in (cfg.get("mappings") or []) if m.get("source") and m.get("target")}
    if not mapping:
        return df
    missing = [s for s in mapping if s not in df.columns]
    if missing:
        raise NodeExecutionError(f"Mapping source column(s) not found: {', '.join(missing)}")
    return df[list(mapping.keys())].rename(columns=mapping)


_AGG_FUNC = {"sum": "sum", "avg": "mean", "mean": "mean", "count": "count", "min": "min", "max": "max"}


def _run_aggregate(df, cfg):
    df = df if df is not None else pd.DataFrame()
    group_cols = [c.strip() for c in (cfg.get("groupBy") or "").split(",") if c.strip()]
    aggs = cfg.get("aggregations") or []
    if not aggs:
        return df
    agg_map = {}
    for a in aggs:
        col, func = a.get("column"), _AGG_FUNC.get((a.get("func") or "sum").lower(), "sum")
        if col:
            agg_map[col] = func
    missing = [c for c in [*group_cols, *agg_map] if c not in df.columns]
    if missing:
        raise NodeExecutionError(f"Aggregate column(s) not found: {', '.join(missing)}")
    if group_cols:
        return df.groupby(group_cols, as_index=False).agg(agg_map)
    return pd.DataFrame([df.agg(agg_map)])


async def _run_join(job_id, node_id, df, cfg, organization_id, primary_connection_id, cache):
    df = df if df is not None else pd.DataFrame()
    right_name = cfg.get("rightDataset") or cfg.get("lookupDataset")
    left_key = cfg.get("leftKey") or cfg.get("lookupKey")
    right_key = cfg.get("rightKey") or cfg.get("lookupKey")
    if not right_name or not left_key:
        await _log(job_id, "WARN", "Join/Lookup node is missing a right dataset or key — passed data through unchanged.", node_id)
        return df
    if not primary_connection_id:
        await _log(job_id, "WARN", f"Join/Lookup right dataset '{right_name}' could not be resolved — this pipeline has no upstream Source Dataset connection to read it from. Passed data through unchanged.", node_id)
        return df

    conn, connector = await _resolve_connector(primary_connection_id, organization_id, cache)
    try:
        result = await asyncio.to_thread(connector.preview_object, right_name, None, SOURCE_ROW_CAP)
    except ConnectorError as exc:
        raise NodeExecutionError(f"Could not read right-hand dataset '{right_name}': {connector._scrub(str(exc))}") from exc
    right_df = pd.DataFrame(result.rows, columns=result.columns)

    how = cfg.get("joinType", "inner")
    how = "outer" if how == "full" else how
    if how == "cross":
        return df.merge(right_df, how="cross")
    if right_key and right_key in right_df.columns:
        return df.merge(right_df, how=how, left_on=left_key, right_on=right_key, suffixes=("", "_right"))
    return df.merge(right_df, how=how, left_on=left_key, right_on=left_key if left_key in right_df.columns else right_key, suffixes=("", "_right"))


async def _run_validation(job_id, node_id, df, cfg):
    df = df if df is not None else pd.DataFrame()
    threshold = cfg.get("passThreshold", 95)
    ruleset_label = cfg.get("rulesetName") or "(unnamed)"
    if len(df) == 0:
        await _log(job_id, "WARN", f"Validation '{ruleset_label}' — no rows to check.", node_id)
        return df, "success"

    complete_rows = int(df.notna().all(axis=1).sum())
    pass_rate = round(complete_rows / len(df) * 100, 1)
    await _log(
        job_id, "INFO",
        f"Validation '{ruleset_label}' — generic row-completeness check (this node's config has no inline rules, "
        f"so pass/fail is based on % of rows with no null values): {pass_rate}% vs {threshold}% threshold.",
        node_id,
    )
    if pass_rate < threshold:
        if (cfg.get("failAction") or "warn") == "error":
            raise NodeExecutionError(f"Validation '{ruleset_label}' failed: {pass_rate}% complete rows, below the {threshold}% threshold.")
        await _log(job_id, "WARN", f"Validation '{ruleset_label}' below threshold ({pass_rate}% < {threshold}%) — continuing (fail action = warn).", node_id)
        return df, "warning"
    return df, "success"


async def _run_reconciliation(job_id, node_id, df, cfg, organization_id, primary_connection_id, cache):
    df = df if df is not None else pd.DataFrame()
    compare_with = cfg.get("compareWith")
    if not compare_with or not primary_connection_id:
        await _log(job_id, "WARN", "Reconciliation node has no resolvable comparison target (needs compareWith + an upstream Source Dataset connection) — skipped.", node_id)
        return df, "skipped"

    conn, connector = await _resolve_connector(primary_connection_id, organization_id, cache)
    try:
        target_count = await asyncio.to_thread(connector.row_count, compare_with, None)
    except ConnectorError as exc:
        await _log(job_id, "WARN", f"Reconciliation target '{compare_with}' could not be read: {connector._scrub(str(exc))} — skipped.", node_id)
        return df, "skipped"
    if target_count is None:
        await _log(job_id, "WARN", f"Reconciliation target '{compare_with}' row count unavailable — skipped.", node_id)
        return df, "skipped"

    try:
        tolerance = float(cfg.get("tolerance", 0.01))
    except (TypeError, ValueError):
        tolerance = 0.01
    current = len(df)
    diff = abs(current - target_count)
    allowed = max(1, round(target_count * tolerance)) if tolerance < 1 else tolerance
    status = "success" if diff <= allowed else "warning"
    await _log(job_id, "INFO", f"Reconciliation vs '{compare_with}': pipeline has {current} rows, target has {target_count} rows (diff {diff}, tolerance {allowed}).", node_id)
    return df, status


def _serialize_dataframe(df: pd.DataFrame, file_type: str, delimiter: str = ",") -> bytes:
    """Shared by the 'file' (local disk) and 'sftp' (remote upload) target
    branches below — same real pandas serialization either way, only the
    destination differs. Whole-file-in-memory, same disclosed bound as
    every other in-memory target in this engine (SOURCE_ROW_CAP-sized
    DataFrames, not a streaming writer)."""
    buf = io.BytesIO()
    if file_type == "excel":
        df.to_excel(buf, index=False)
    elif file_type == "json":
        buf.write(df.to_json(orient="records", indent=2).encode())
    else:
        buf.write(df.to_csv(index=False, sep=delimiter).encode())
    return buf.getvalue()


def _file_ext(file_type: str) -> str:
    return {"csv": "csv", "excel": "xlsx", "json": "json"}.get(file_type, "csv")


async def _run_target(job_id, node_id, df, cfg, organization_id, primary_connection_id, cache, pipeline_id, job_id_for_path):
    """Returns (df, output_file, rejected) — output_file is the path of
    the file just written, RELATIVE to OUTPUT_BASE_PATH (e.g.
    '3/node_xyz/out.csv'), for file targets; None for dataset/sftp/api
    targets (sftp writes to a remote host, api writes over HTTP — neither
    has a local download). Recorded onto the job's node_stats by the
    caller so the Pipeline Builder job log can offer a real download of
    the exact file this run produced (api/pipeline_jobs.py). rejected is
    [{"row": dict, "error": str}, ...] — only ever non-empty for the
    dataset full-load path below (KK3); every other target type returns
    an empty list (a bad file write/SFTP upload/API POST fails the whole
    node, same as before — there's no per-row bulk-then-fallback concept
    for those transports)."""
    df = df if df is not None else pd.DataFrame()
    target_type = cfg.get("targetType", "dataset")

    if target_type == "file":
        file_name = cfg.get("fileName") or "output"
        file_type = cfg.get("fileType", "csv")
        mode = cfg.get("mode", "replace")
        out_dir = Path(OUTPUT_BASE_PATH) / str(pipeline_id) / node_id
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = "" if mode == "replace" else f"_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        out_path = out_dir / f"{file_name}{suffix}.{_file_ext(file_type)}"
        try:
            data = _serialize_dataframe(df, file_type, cfg.get("fileDelimiter", ","))
            out_path.write_bytes(data)
        except Exception as exc:
            raise NodeExecutionError(f"Could not write output file: {exc}") from exc
        await _log(job_id, "INFO", f"Wrote {len(df)} row(s) to {out_path}", node_id)
        return df, f"{pipeline_id}/{node_id}/{out_path.name}", []

    if target_type == "sftp":
        connection_id = cfg.get("connectionId")
        if not connection_id:
            raise NodeExecutionError("SFTP target has no connection selected.")
        file_name = cfg.get("fileName") or "output"
        file_type = cfg.get("fileType", "csv")
        mode = cfg.get("mode", "replace")
        conn, connector = await _resolve_connector(connection_id, organization_id, cache)
        remote_dir = (cfg.get("remoteDir") or connector.config.get("base_path") or "/").rstrip("/") or "/"
        suffix = "" if mode == "replace" else f"_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        remote_path = f"{remote_dir}/{file_name}{suffix}.{_file_ext(file_type)}"
        try:
            data = _serialize_dataframe(df, file_type, cfg.get("fileDelimiter", ","))
            written = await asyncio.to_thread(connector.write_file, remote_path, data)
        except ConnectorError as exc:
            raise NodeExecutionError(connector._scrub(str(exc))) from exc
        await _log(job_id, "INFO", f"Uploaded {len(df)} row(s) ({written} bytes) to {conn.name}:{remote_path}", node_id)
        return df, None, []

    if target_type == "api":
        connection_id = cfg.get("connectionId")
        if not connection_id:
            raise NodeExecutionError("API target has no connection selected.")
        endpoint = cfg.get("apiEndpoint")
        if not endpoint:
            raise NodeExecutionError("API target node has no endpoint configured.")
        conn, connector = await _resolve_connector(connection_id, organization_id, cache)
        records = df.to_dict(orient="records")
        try:
            stats = await asyncio.to_thread(
                connector.send_records,
                endpoint=endpoint, method=cfg.get("apiMethod") or "POST",
                query_params={p["name"]: p["value"] for p in cfg.get("apiQueryParams", []) if p.get("name")},
                headers={h["name"]: h["value"] for h in cfg.get("apiHeaders", []) if h.get("name")},
                records=records, records_key=cfg.get("apiRecordsKey") or None,
                batch_size=int(cfg.get("apiBatchSize") or 500),
            )
        except ConnectorError as exc:
            raise NodeExecutionError(connector._scrub(str(exc))) from exc
        await _log(
            job_id, "INFO",
            f"POSTed {stats['records_sent']} record(s) to {conn.name}:{endpoint} across {stats['batches_sent']} batch(es) (HTTP {stats['last_status']}).",
            node_id,
        )
        return df, None, []

    # dataset target — the node's OWN connection picker (cfg.connectionId)
    # takes priority when set; falling back to the pipeline's PRIMARY
    # source connection is kept only for pipelines saved before this node
    # had a picker of its own (never silently reinterpreted — the two
    # produce identical behavior when the source connection IS the
    # intended target, and the fallback is what let a file-sourced
    # pipeline — e.g. Document Intelligence's "Continue to Transformation"
    # output — end up with literally no way to choose a database).
    dataset_name = cfg.get("datasetName")
    if not dataset_name:
        raise NodeExecutionError("Dataset target has no dataset/table name configured.")
    target_connection_id = cfg.get("connectionId") or primary_connection_id
    if not target_connection_id:
        raise NodeExecutionError("Dataset target has no connection selected — pick one, or add an upstream Source Dataset node with a database connection.")

    conn, connector = await _resolve_connector(target_connection_id, organization_id, cache)
    try:
        writer = get_bulk_writer(conn.connection_type.value, connector)
    except ConnectorError as exc:
        raise NodeExecutionError(str(exc)) from exc

    load_type = cfg.get("loadType", "full")

    if load_type == "delta":
        # Real SCD Type 1 / Type 2 — see loading/scd.py. This is the one
        # branch of _run_target that does NOT go through the generic
        # ensure_table/truncate/write_batch flow below: SCD2 in particular
        # needs its own transaction-scoped SELECT+UPDATE+INSERT sequence
        # that a single write_batch() call can't express.
        delta_strategy = cfg.get("deltaStrategy", "scd1")
        business_keys = [k for k in (cfg.get("businessKeys") or []) if k]
        if not business_keys:
            raise NodeExecutionError(
                f"{'SCD Type 2' if delta_strategy == 'scd2' else 'SCD Type 1'} requires at least one business key "
                f"column — none configured on this target node."
            )
        change_detection = cfg.get("changeDetection", "auto")
        changed_columns = [c for c in (cfg.get("changedColumns") or []) if c] if change_detection == "manual" else None
        try:
            if delta_strategy == "scd2":
                stats = await asyncio.to_thread(
                    scd.scd2_merge, connector, writer, None, dataset_name, business_keys, df,
                    change_columns=changed_columns,
                    start_col=cfg.get("scd2StartColumn") or "effective_start_date",
                    end_col=cfg.get("scd2EndColumn") or "effective_end_date",
                    current_col=cfg.get("scd2CurrentColumn") or "is_current",
                )
                await _log(
                    job_id, "INFO",
                    f"SCD Type 2 merge on '{dataset_name}': {stats['new']} new key(s), {stats['changed']} changed "
                    f"(prior version closed, new version inserted), {stats['unchanged']} unchanged.",
                    node_id,
                )
            else:
                stats = await asyncio.to_thread(
                    scd.scd1_merge, connector, writer, None, dataset_name, business_keys, df, change_columns=changed_columns,
                )
                await _log(
                    job_id, "INFO",
                    f"SCD Type 1 merge on '{dataset_name}': {stats['inserted']} inserted, {stats['updated']} updated, "
                    f"{stats['unchanged']} unchanged (skipped — no change detected).",
                    node_id,
                )
        except scd.ScdError as exc:
            raise NodeExecutionError(str(exc)) from exc
        except ConnectorError as exc:
            raise NodeExecutionError(connector._scrub(str(exc))) from exc
        except Exception as exc:
            raise NodeExecutionError(f"SCD merge failed for target table '{dataset_name}': {connector._scrub(str(exc))}") from exc
        return df, None, []

    # ── Full load ──────────────────────────────────────────────────
    # fullLoadStrategy (new Load Strategy panel) maps onto the same two
    # internal levers (create_if_missing/drop_recreate + append-vs-
    # truncate mode) the engine already had — see §5 of the load-strategy
    # spec. Pipelines saved before that panel existed have no
    # fullLoadStrategy field at all and keep running exactly as before,
    # straight off their raw `mode`/`createIfNotExists`/`dropAndRecreate`
    # config (never silently reinterpreted).
    full_strategy = cfg.get("fullLoadStrategy")
    if full_strategy:
        create_if_missing = full_strategy in ("create_if_not_exists", "truncate_load", "append")
        drop_recreate = full_strategy == "drop_recreate"
        mode = "replace" if full_strategy == "truncate_load" else "append"
    else:
        mode = cfg.get("mode", "replace")
        if mode in ("upsert", "merge"):
            await _log(job_id, "WARN", "Dataset target has no key-column configuration on this node type — treating upsert/merge as append.", node_id)
            mode = "append"
        create_if_missing = cfg.get("createIfNotExists", True)
        drop_recreate = bool(cfg.get("dropAndRecreate", False))

    # Create/recreate the target table from the real DataFrame's dtypes
    # (loading/writers/base.py's ensure_table — generic across every
    # SQLAlchemy-backed writer via reflection/DDL, not fabricated per
    # vendor). create_if_missing/drop_recreate were derived above, either
    # from the new Load Strategy panel's fullLoadStrategy or (for
    # pipelines saved before it existed) the legacy createIfNotExists/
    # dropAndRecreate fields directly.
    table_status = "exists"
    try:
        if create_if_missing or drop_recreate:
            table_status = await asyncio.to_thread(writer.ensure_table, None, dataset_name, df, recreate=drop_recreate)
            if table_status != "exists":
                await _log(job_id, "INFO", f"Target table '{dataset_name}' {table_status} from the pipeline's output columns.", node_id)
        elif not await asyncio.to_thread(writer.table_exists, None, dataset_name):
            raise NodeExecutionError(
                f"Target table '{dataset_name}' does not exist and \"Create table if missing\" is turned off for this node."
            )
    except NodeExecutionError:
        raise
    except Exception as exc:
        raise NodeExecutionError(f"Could not prepare target table '{dataset_name}': {connector._scrub(str(exc))}") from exc

    # A table that was just created/recreated is already empty — only
    # truncate one that already existed coming into this run.
    if mode == "replace" and table_status == "exists":
        try:
            await asyncio.to_thread(writer.truncate, None, dataset_name)
        except ConnectorError as exc:
            raise NodeExecutionError(f"Could not truncate target table: {connector._scrub(str(exc))}") from exc

    columns = list(df.columns)
    rows = df.to_dict(orient="records")
    written = 0
    rejected: list[dict] = []
    try:
        for i in range(0, len(rows), 5000):
            batch = rows[i:i + 5000]
            # write_batch_with_fallback (KK2/KK3): tries the writer's real
            # bulk insert for the whole batch first, exactly as before;
            # only on failure does it retry row-by-row, so one bad row in
            # a 5,000-row batch no longer fails the other 4,999 — each row
            # that still fails on its own comes back with its own real
            # error instead of the batch-level one.
            batch_written, batch_rejects = await asyncio.to_thread(
                write_batch_with_fallback, writer, None, dataset_name, columns, batch
            )
            written += batch_written
            rejected.extend(batch_rejects)
    except ConnectorError as exc:
        raise NodeExecutionError(f"Could not write to target table '{dataset_name}': {connector._scrub(str(exc))}") from exc
    if rejected:
        await reject_service.record_rejects(
            organization_id=organization_id, source_type=RejectSourceType.PIPELINE_JOB, source_id=job_id,
            node_id=node_id, target_object=dataset_name, rejects=rejected,
        )
        await _log(
            job_id, "WARN",
            f"{len(rejected)} row(s) rejected writing to '{dataset_name}' — see Rejected Records for each row and its real error.",
            node_id,
        )
    await _log(job_id, "INFO", f"Wrote {written} row(s) to '{dataset_name}' ({mode}).", node_id)
    return df, None, rejected


async def _run_notification(job_id, node_id, cfg):
    channel = cfg.get("channel", "email")
    if channel != "webhook":
        await _log(job_id, "WARN", f"Notification channel '{channel}' isn't wired to a real transport in this build — not sent.", node_id)
        return
    url = cfg.get("recipients")
    if not url:
        await _log(job_id, "WARN", "Webhook notification has no URL configured — not sent.", node_id)
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"event": "pipeline_job_node", "node_id": node_id})
        await _log(job_id, "INFO", f"Webhook POSTed to {url} — HTTP {resp.status_code}.", node_id)
    except Exception as exc:
        await _log(job_id, "WARN", f"Webhook POST to {url} failed: {exc}", node_id)


# ── Main driver ─────────────────────────────────────────────────

async def _execute(job_id: int) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(PipelineJob, job_id)
        organization_id = job.organization_id
        version_result = await db.execute(
            select(PipelineVersion).where(
                PipelineVersion.pipeline_id == job.pipeline_id,
                PipelineVersion.version_number == job.pipeline_version,
            )
        )
        version = version_result.scalar_one_or_none()
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)
        await db.commit()

    if version is None:
        await _fail(job_id, "Pipeline version no longer exists (version retention keeps only the last 5).")
        return

    defn = version.pipeline_definition or {}
    nodes = {n["id"]: n for n in defn.get("nodes", [])}
    edges = defn.get("edges", [])

    await _log(job_id, "INFO", f"Job started — pipeline v{job.pipeline_version}, {len(nodes)} node(s).")

    try:
        order = _topo_sort(nodes, edges)

        # Deliberately strict (bracket access, not .get()) — every real
        # edge this app itself ever saves always has both keys, so a
        # missing one means the definition is genuinely corrupt and
        # should fail loudly here, not be silently reinterpreted as "no
        # edge" (which would run src1/tgt1 as disconnected — a target
        # node quietly getting an empty DataFrame instead of its real
        # upstream input is a far worse outcome than a clear failure).
        incoming: dict[str, list[str]] = {}
        for e in edges:
            incoming.setdefault(e["targetId"], []).append(e["sourceId"])
    except ValueError as exc:
        await _fail(job_id, str(exc))
        return
    except Exception as exc:
        # Defense in depth: this setup step runs BEFORE the node loop's
        # own try/except below, and start_job() fires _execute() via
        # asyncio.create_task() without ever awaiting it or checking its
        # result — an exception raised here previously had nowhere to go
        # but a silently-dropped "Task exception was never retrieved"
        # warning, leaving the job stuck RUNNING forever with no error a
        # user could ever see. Never again: any failure here is now a
        # real, visible FAILED status with a real message.
        await _fail(job_id, f"Could not prepare this job's node graph: {exc}"[:300])
        return

    dataframes: dict[str, pd.DataFrame] = {}
    connector_cache: dict[int, tuple] = {}
    primary_connection_id: int | None = None
    total_in: int | None = None
    total_out: int | None = None

    try:
        for node_id in order:
            if is_cancelled(job_id):
                await _log(job_id, "WARN", "Cancellation requested — stopping before the next node.", node_id)
                await _finish(job_id, JobStatus.CANCELLED)
                return

            node = nodes[node_id]
            ntype = node.get("type")
            label = node.get("label", ntype)
            cfg = node.get("config", {}) or {}
            src_ids = incoming.get(node_id, [])
            input_df = dataframes.get(src_ids[0]) if src_ids else None

            t0 = datetime.now(timezone.utc)
            await _log(job_id, "INFO", f"Running '{label}' ({ntype})…", node_id)
            status = "success"
            output_file = None
            rejected = []

            try:
                if ntype in DI_NODE_TYPES:
                    df = input_df if input_df is not None else pd.DataFrame()
                    await _log(job_id, "WARN", f"'{label}' is a Document Intelligence node — not executed by the general pipeline engine (that workflow runs in the Document Intelligence wizard). Passed data through unchanged.", node_id)
                    status = "skipped"
                elif ntype == "source":
                    df, resolved_conn_id = await _run_source(job_id, node_id, cfg, organization_id, connector_cache)
                    if primary_connection_id is None and resolved_conn_id:
                        primary_connection_id = resolved_conn_id
                elif ntype == "filter":
                    df = _run_filter(input_df, cfg)
                elif ntype == "select":
                    df = _run_select(input_df, cfg)
                elif ntype == "rename":
                    df = _run_rename(input_df, cfg)
                elif ntype == "cast":
                    df = _run_cast(input_df, cfg)
                elif ntype == "sort":
                    df = _run_sort(input_df, cfg)
                elif ntype == "deduplicate":
                    df = _run_dedup(input_df, cfg)
                elif ntype == "calculated":
                    df = _run_calculated(input_df, cfg)
                elif ntype == "conditional":
                    df = _run_conditional(input_df, cfg)
                elif ntype == "mapping":
                    df = _run_mapping(input_df, cfg)
                elif ntype == "aggregate":
                    df = _run_aggregate(input_df, cfg)
                elif ntype in ("join", "lookup"):
                    df = await _run_join(job_id, node_id, input_df, cfg, organization_id, primary_connection_id, connector_cache)
                elif ntype == "validation_step":
                    df, status = await _run_validation(job_id, node_id, input_df, cfg)
                elif ntype == "reconciliation_step":
                    df, status = await _run_reconciliation(job_id, node_id, input_df, cfg, organization_id, primary_connection_id, connector_cache)
                elif ntype == "target":
                    df, output_file, rejected = await _run_target(job_id, node_id, input_df, cfg, organization_id, primary_connection_id, connector_cache, job.pipeline_id, job_id)
                elif ntype == "notification":
                    df = input_df
                    await _run_notification(job_id, node_id, cfg)
                else:
                    df = input_df if input_df is not None else pd.DataFrame()
                    await _log(job_id, "WARN", f"Unknown node type '{ntype}' — skipped.", node_id)
                    status = "skipped"
            except NodeExecutionError as exc:
                await _update_node_stats(job_id, node_id, {"label": label, "type": ntype, "status": "error", "message": str(exc)})
                await _fail(job_id, f"Node '{label}': {exc}")
                return

            dataframes[node_id] = df
            rows_out = 0 if df is None else len(df)
            rows_in = 0 if input_df is None else len(input_df)
            duration_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
            stats_patch = {
                "label": label, "type": ntype, "status": status,
                "rows_in": rows_in, "rows_out": rows_out, "duration_ms": duration_ms,
            }
            if output_file:
                stats_patch["output_file"] = output_file
            if rejected:
                stats_patch["rows_rejected"] = len(rejected)
            await _update_node_stats(job_id, node_id, stats_patch)
            await _log(job_id, "INFO", f"'{label}' finished — {rows_in} in / {rows_out} out ({duration_ms} ms).", node_id)
            if total_in is None:
                total_in = rows_in
            total_out = rows_out

        async with AsyncSessionLocal() as db:
            job = await db.get(PipelineJob, job_id)
            job.records_in = total_in
            await db.commit()
        await _log(job_id, "INFO", "Job finished successfully.")
        await _finish(job_id, JobStatus.SUCCEEDED, records_out=total_out)
    except Exception as exc:  # last-resort safety net — never leave a job stuck RUNNING
        await _fail(job_id, f"Unexpected error: {exc}"[:300])
    finally:
        _cancelled.discard(job_id)
