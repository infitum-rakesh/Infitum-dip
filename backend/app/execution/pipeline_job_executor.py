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
Fabric/NetSuite SuiteQL) AND for Salesforce, a TABLE/VIEW/QUERY source now
reads EXHAUSTIVELY via the same chunked open_sql_stream()/fetch_chunk()
primitive execution/worker.py's "Run Extraction" engine uses — no silent
row truncation. A plain [source, *row-wise transforms, target] shape runs
through the real interleaved streaming fast path below (_execute_streaming_
chain) instead, which is genuinely O(chunk size) memory — extract and load
happen chunk-by-chunk, nothing ever accumulates — so it has NO row cap at
all; SOURCE_ROW_HARD_CAP only still applies to the older, full-buffering
per-node engine (used when the pipeline needs a whole-dataset operation —
sort/aggregate/dedupe/join — or a non-streaming target), where it remains
a real in-memory safety ceiling since that engine still isn't a
distributed/spilling engine (real bug found+fixed live 2026-09-16: this
cap used to also apply inside the streaming fast path itself, silently
truncating a genuinely unbounded SFDC -> Oracle extraction at exactly
2,000,000 rows even though nothing was actually being buffered — removed
there entirely). Streaming is a generic capability
check (connector.build_select_all_sql() — see connectors/base.py), NOT
gated on the SQL-only quote_object()/quote_identifier() concept the way an
earlier version of this engine mistakenly was — that earlier gate silently
routed every Salesforce object through the bounded preview_object() path
below instead, which Salesforce itself hard-caps near 2,000 rows
regardless of the object's real size or the Source Dataset node's Load
Type (BULK/AUTO/ROW_BY_ROW) setting; both are now real, honored, and
disclosed via the source node's log line if the row cap is ever hit.
Connectors that genuinely don't implement build_select_all_sql() (generic
SOAP/REST, files) still fall back to the older single-shot bounded
preview, capped at SOURCE_ROW_CAP.

Node types executed for real:
  source              TABLE/VIEW/QUERY on a saved connection — real
                       exhaustive chunked read for every connector that
                       implements build_select_all_sql() + open_sql_stream()
                       (see _stream_all_rows), or the connector's bounded
                       preview_object/preview_sql for connectors that
                       don't support streaming — or an uploaded
                       PipelineSourceFile (CSV/TSV/Excel/JSON/XML/Parquet
                       read directly with pandas, or via
                       connectors/files/xml_format.py for XML).
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

  invoice_intake, invoice_processing, invoice_validation,
  invoice_review_gate, invoice_erp_posting, invoice_reconciliation
                       INV24 — Invoice Intelligence node group. Each is a
                       thin wrapper around a real, already-shipped
                       Invoice Intelligence service call — one row per
                       document/invoice flows through the DataFrame,
                       exactly like every other node here, but the real
                       work (file discovery, OCR/AI extraction, canonical
                       materialization, the confidence-model decision
                       matrix, ERP posting, reconciliation) happens in
                       services/document_service.py and services/
                       invoice/*, never reimplemented here. Deliberately
                       6 nodes, not 7: there is no separate "Anomaly
                       Check" node — invoice_decision_service.decide()
                       (called by invoice_validation below) already runs
                       the real INV23 anomaly check as part of its own
                       decision matrix, so a standalone node would either
                       duplicate that check or silently do nothing new.
                         invoice_intake        real file discovery (reuses
                       document_schedule_service's own discovery +
                       DocumentSourceRegistry idempotency ledger — the
                       exact machinery Document Source schedules use,
                       with schedule_id=None marking "discovered by a
                       Pipeline Builder run") + document_service.
                       save_from_source(). Outputs one row per Document.
                         invoice_processing     document_service.
                       process_document(document_type="INVOICE",
                       skip_invoice_pipeline=True) — real OCR + AI
                       extraction only (the automatic materialize/
                       validate/decide chain that call would otherwise
                       trigger is skipped here so invoice_validation
                       below is the one place it actually runs — see
                       skip_invoice_pipeline's own docstring for why this
                       split needed a real, minimal, additive parameter
                       rather than two competing code paths).
                         invoice_validation      real materialize_invoice()
                       + evaluate_invoice() + decide() — the actual
                       confidence model, decision matrix, real Review
                       Queue item creation on REVIEW_REQUIRED, and the
                       real INV23 anomaly check, all exactly once.
                         invoice_review_gate     a real filter (sync, no
                       DB access) — only rows whose final_decision is in
                       cfg["passOn"] (default: STRAIGHT_THROUGH) continue
                       downstream; everything else was already routed to
                       a real Review Queue item above and is genuinely
                       held back, not silently forced through.
                         invoice_erp_posting     invoice_erp_service.
                       post_invoice() (real idempotent posting, retry,
                       circuit breaker — INV9). Posting itself is fire-
                       and-forget (a real ERP round trip can run well
                       past one request), so this node polls each
                       invoice's own erp_status for a bounded window and
                       reports whatever real status it actually settles
                       at — never fabricates SUCCEEDED early.
                         invoice_reconciliation  invoice_reconciliation_
                       service.reconcile_invoice() (real GET-and-compare
                       — INV10). ERP Posting's own success path already
                       auto-triggers this once per invoice, so this node
                       is often a genuine RE-verification rather than a
                       first-time check — still a real network round
                       trip and a real result, never a cached replay.

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
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

import app.models  # ensure all ORM mappers are initialized
from app.connectors.base import ColumnInfo, ConnectorError, ConnectorUnsupportedError
from app.database import tenant_engine_registry
from app.execution import mapping_execution
from app.loading import scd
from app.loading.writers.base import write_batch_with_fallback
from app.loading.writers.registry import get_bulk_writer
from app.models.connection import Connection
from app.models.pipeline import PipelineVersion
from app.models.pipeline_job import JobStatus, PipelineJob, PipelineJobLog
from app.models.rejected_record import RejectSourceType
from app.services import pipeline_file_service, pipeline_job_service, reject_service
from app.services.connection_service import build_connector, get_connection

SOURCE_ROW_CAP = 50_000  # bounded single-shot preview cap — only still used as a
# fallback for connectors that don't support open_sql_stream (Salesforce, generic
# SOAP/REST, files); see _stream_all_rows below for the real path.
SOURCE_ROW_HARD_CAP = 25_000_000  # in-memory safety ceiling for the OLDER,
# full-buffering per-node engine only (_run_source/_stream_all_rows below) —
# used when a pipeline needs a whole-dataset node (sort/aggregate/dedupe/join)
# or a non-DB target, so it genuinely still loads one source into one pandas
# DataFrame and needs *some* ceiling. The real streaming fast path
# (_execute_streaming_chain — a plain [source, *row-wise transforms, target]
# pipeline, e.g. a straightforward SFDC -> Oracle extraction) is O(chunk
# size) memory and has NO cap at all as of 2026-09-16 — see that function's
# own comment for the real production bug this fixed (a genuinely unbounded
# extraction was silently truncated at exactly 2,000,000 rows even though
# nothing was ever actually buffered). Raised from 2,000,000 to 25,000,000
# here too, for the pipelines that still do need real buffering, disclosed
# via the same WARN log line if it's ever hit.
SOURCE_STREAM_CHUNK_SIZE = 20_000
OUTPUT_BASE_PATH = "pipeline_outputs"  # relative to the backend process cwd

DI_NODE_TYPES = {"doc_input", "image_scanner", "ocr_engine", "ai_extraction", "structured_dataset"}

_running_tasks: dict[int, asyncio.Task] = {}
_cancelled: set[int] = set()
_cancel_events: dict[int, threading.Event] = {}
# job_id -> organization_id, set once in start_job() (before the task even
# starts) and read by every _log/_update_node_stats/_finish call below —
# avoids threading organization_id through all ~47 of those call sites
# individually; same "small module-level dict keyed by job_id" shape
# _running_tasks/_cancelled already use for their own cross-cutting concerns.
_job_org_cache: dict[int, int | None] = {}


class NodeExecutionError(Exception):
    """A node failed in a way that should stop the whole job."""


@dataclass(frozen=True)
class TargetLoadConfig:
    load_mode: str
    load_strategy: str
    create_table: bool
    drop_existing: bool
    truncate_first: bool
    write_mode: str

    def as_log_dict(self) -> dict:
        return {
            "loadMode": self.load_mode,
            "loadStrategy": self.load_strategy,
            "createTable": self.create_table,
            "dropExisting": self.drop_existing,
            "truncateFirst": self.truncate_first,
            "writeMode": self.write_mode,
        }


def normalize_target_load_config(cfg: dict | None) -> TargetLoadConfig:
    cfg = cfg or {}
    canonical_mode = str(cfg.get("loadMode") or "").upper()
    canonical_strategy = str(cfg.get("loadStrategy") or "").upper()

    if canonical_mode:
        if canonical_mode == "APPEND":
            return TargetLoadConfig("APPEND", "APPEND", False, False, False, "insert")
        if canonical_mode == "FULL":
            if canonical_strategy in ("DROP_AND_CREATE", "DROP_RECREATE"):
                return TargetLoadConfig("FULL", "DROP_AND_CREATE", True, True, False, "insert")
            if canonical_strategy in ("TRUNCATE_AND_LOAD", "TRUNCATE_LOAD"):
                return TargetLoadConfig("FULL", "TRUNCATE_AND_LOAD", True, False, True, "insert")
            if canonical_strategy in ("CREATE_IF_NOT_EXISTS", "CREATE_IF_MISSING"):
                return TargetLoadConfig("FULL", "CREATE_IF_NOT_EXISTS", True, False, False, "insert")
            raise NodeExecutionError("Invalid target load configuration: FULL load requires a valid loadStrategy.")
        if canonical_mode == "DELTA":
            return TargetLoadConfig("DELTA", canonical_strategy or str(cfg.get("deltaStrategy") or "scd1").upper(), False, False, False, "merge")
        raise NodeExecutionError(f"Invalid target load configuration: unknown loadMode '{canonical_mode}'.")

    load_type = str(cfg.get("loadType") or "full").lower()
    full_strategy = str(cfg.get("fullLoadStrategy") or "").lower()

    if load_type == "delta":
        return TargetLoadConfig(
            load_mode="DELTA",
            load_strategy=str(cfg.get("deltaStrategy") or "scd1").upper(),
            create_table=False,
            drop_existing=False,
            truncate_first=False,
            write_mode="merge",
        )

    if full_strategy:
        if full_strategy == "drop_recreate":
            return TargetLoadConfig("FULL", "DROP_AND_CREATE", True, True, False, "insert")
        if full_strategy == "truncate_load":
            return TargetLoadConfig("FULL", "TRUNCATE_AND_LOAD", True, False, True, "insert")
        if full_strategy == "append":
            return TargetLoadConfig("APPEND", "APPEND", False, False, False, "insert")
        if full_strategy == "create_if_not_exists":
            return TargetLoadConfig("FULL", "CREATE_IF_NOT_EXISTS", True, False, False, "insert")
        raise NodeExecutionError(f"Invalid target load configuration: unknown full-load strategy '{full_strategy}'.")

    mode = str(cfg.get("mode") or "replace").lower()
    if bool(cfg.get("dropAndRecreate")):
        return TargetLoadConfig("FULL", "DROP_AND_CREATE", True, True, False, "insert")
    if mode in ("replace", "truncate", "truncate_load"):
        return TargetLoadConfig(
            "FULL", "TRUNCATE_AND_LOAD",
            bool(cfg.get("createIfNotExists", True)), False, True, "insert",
        )
    if mode in ("append", "insert"):
        return TargetLoadConfig(
            "APPEND", "APPEND",
            bool(cfg.get("createIfNotExists", False)), False, False, "insert",
        )
    if mode in ("upsert", "merge"):
        return TargetLoadConfig("DELTA", "SCD1", False, False, False, "merge")
    raise NodeExecutionError(f"Invalid target load configuration: unknown mode '{mode}'.")


def is_running(job_id: int) -> bool:
    return job_id in _running_tasks


def is_cancelled(job_id: int) -> bool:
    if job_id in _cancelled:
        return True
    evt = _cancel_events.get(job_id)
    return evt.is_set() if evt else False


def request_cancel(job_id: int) -> bool:
    _cancelled.add(job_id)
    evt = _cancel_events.get(job_id)
    if evt:
        evt.set()
    task = _running_tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    return True


def start_job(job_id: int, organization_id: int | None) -> None:
    """Fire-and-forget — the API route that calls this does not (and must
    not) await it; the frontend polls GET /pipeline-jobs/{id} and .../logs.
    organization_id comes from the job row the caller already has in hand —
    recorded here (see _job_org_cache above) before the task even starts."""
    _job_org_cache[job_id] = organization_id
    _cancel_events[job_id] = threading.Event()
    task = asyncio.create_task(_execute(job_id, organization_id))
    _running_tasks[job_id] = task
    def _cleanup(_t):
        _running_tasks.pop(job_id, None)
        _cancel_events.pop(job_id, None)
    task.add_done_callback(_cleanup)


# ── Logging / stats helpers ─────────────────────────────────────────

async def _log(job_id: int, level: str, message: str, node_id: str | None = None) -> None:
    try:
        sessionmaker = await tenant_engine_registry.get_background_sessionmaker(_job_org_cache.get(job_id))
        async with sessionmaker() as db:
            db.add(PipelineJobLog(job_id=job_id, level=level, node_id=node_id, message=str(message)[:2000]))
            await db.commit()
    except Exception as exc:
        print(f"[pipeline_job_executor] Log error for job {job_id}: {exc}", flush=True)


async def _update_node_stats(job_id: int, node_id: str, patch: dict) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(_job_org_cache.get(job_id))
    async with sessionmaker() as db:
        job = await db.get(PipelineJob, job_id)
        if job is None:
            return
        stats = dict(job.node_stats or {})
        stats[node_id] = {**stats.get(node_id, {}), **patch}
        job.node_stats = stats
        flag_modified(job, "node_stats")
        await db.commit()


async def _update_job_metrics(job_id: int, patch: dict) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(_job_org_cache.get(job_id))
    async with sessionmaker() as db:
        job = await db.get(PipelineJob, job_id)
        if job is None:
            return
        stats = dict(job.node_stats or {})
        metrics = dict(stats.get("__metrics__") or {})
        stats["__metrics__"] = {**metrics, **patch}
        job.node_stats = stats
        flag_modified(job, "node_stats")
        await db.commit()


async def _finish(job_id: int, status: JobStatus, *, error_message: str | None = None, records_out: int | None = None) -> None:
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(_job_org_cache.get(job_id))
    async with sessionmaker() as db:
        job = await db.get(PipelineJob, job_id)
        if job is not None:
            job.status = status
            job.completed_at = datetime.now(timezone.utc)
            if error_message is not None:
                job.error_message = error_message
            if records_out is not None:
                job.records_out = records_out

            stats = dict(job.node_stats or {})
            metrics = dict(stats.get("__metrics__") or {})
            if status == JobStatus.SUCCEEDED:
                metrics["stage"] = "COMPLETED"
            elif status == JobStatus.FAILED:
                metrics["stage"] = "FAILED"
            elif status == JobStatus.CANCELLED:
                metrics["stage"] = "ABORTED"
            stats["__metrics__"] = metrics
            job.node_stats = stats
            flag_modified(job, "node_stats")
            await db.commit()

    # Monitoring integration seam — SLA evaluation, alert rules, and
    # auto-Incident-on-failure all hang off this one call. Runs in its own
    # session and never raises (see on_job_finished's own try/except), so a
    # monitoring bug can never turn a real pipeline result into a failure.
    from app.monitoring.service import on_job_finished
    await on_job_finished(job_id, _job_org_cache.get(job_id))


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
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        conn = await get_connection(db, connection_id, organization_id)
        if conn is None:
            raise NodeExecutionError(f"Connection {connection_id} was not found or no longer belongs to this organization.")
        connector = build_connector(conn)
        cache[connection_id] = (conn, connector)
        return conn, connector


# ── Node executors ─────────────────────────────────────────────

async def _stream_all_rows(
    connector, sql: str, parameters: dict, *, job_id: int | None = None, node_id: str | None = None, extraction_mode: str | None = None,
) -> tuple[list[list], list[str], bool]:
    """Real, chunked, exhaustive read of a SELECT via the same
    open_sql_stream()/fetch_chunk() primitive execution/worker.py's "Run
    Extraction" engine already uses for genuinely unbounded reads — this
    replaces the old single-shot preview_object()/preview_sql() call,
    which opened one cursor, called fetchmany() exactly ONCE, and threw
    the rest of the real result set away (the actual cause of the
    "only pulls the first 50,000 rows" bug). Raises ConnectorUnsupportedError
    for connectors that don't implement streaming (generic SOAP/REST,
    files) — callers fall back to the old bounded preview path for those.
    Still enforces SOURCE_ROW_HARD_CAP as a safety ceiling since this
    engine loads the whole source into one in-memory pandas DataFrame —
    but that ceiling is far above SOURCE_ROW_CAP, and hitting it is
    disclosed via `truncated` exactly like the old cap was, never applied
    silently.

    extraction_mode threads a connector-specific extraction hint (today:
    Salesforce's BULK/AUTO/ROW_BY_ROW Load Type — see
    SalesforceConnector.open_sql_stream) straight to open_sql_stream();
    every other connector's open_sql_stream() accepts and ignores it.
    job_id/node_id (when given) mirror real per-chunk progress into this
    job's own visible log (PipelineJobLog), not just the server console —
    the job log viewer is what a user actually watches during a run."""
    stream = await asyncio.to_thread(connector.open_sql_stream, sql, parameters or {}, extraction_mode=extraction_mode)
    try:
        columns = list(stream.columns)
        rows: list[list] = []
        truncated = False
        chunk_num = 0
        while True:
            chunk = await asyncio.to_thread(stream.fetch_chunk, SOURCE_STREAM_CHUNK_SIZE)
            if not chunk:
                break
            chunk_num += 1
            rows.extend([r.get(c) for c in columns] for r in chunk)
            if job_id is not None:
                await _log(job_id, "INFO", f"Fetched chunk {chunk_num} — {len(chunk):,} row(s) (running total: {len(rows):,}).", node_id)
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
        sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
        async with sessionmaker() as db:
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
            elif ext == "xml":
                # Reuses connectors/files/xml_format.py's iter_rows — same
                # record-boundary convention (root's direct children are
                # records, attributes become "@attr" columns) an XML file
                # already gets when read via a connection instead of an
                # upload, so both paths produce identically-shaped rows.
                from app.connectors.files.xml_format import iter_rows as _xml_iter_rows
                df = pd.DataFrame(list(_xml_iter_rows(io.BytesIO(content))))
            elif ext == "parquet":
                df = pd.read_parquet(io.BytesIO(content))
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
    source_columns = None
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
            extraction_mode = cfg.get("loadType") if conn.connection_type.value == "SALESFORCE" else None
            try:
                await _log(job_id, "INFO", f"Streaming custom SOQL/SQL from {conn.name} (exhaustive read, no row cap besides the {SOURCE_ROW_HARD_CAP:,}-row in-memory safety ceiling)...", node_id)
                rows, columns, truncated = await _stream_all_rows(
                    connector, cfg["customSql"], params, job_id=job_id, node_id=node_id, extraction_mode=extraction_mode,
                )
                cap_note = SOURCE_ROW_HARD_CAP
                await _log(job_id, "INFO", f"Streamed {len(rows):,} row(s) from {conn.name}.", node_id)
            except ConnectorUnsupportedError:
                result = await asyncio.to_thread(connector.preview_sql, cfg["customSql"], params, SOURCE_ROW_CAP)
                columns, rows, truncated = result.columns, result.rows, result.truncated
        else:
            object_name = cfg.get("object")
            if not object_name:
                raise NodeExecutionError("Source Dataset node has no object/table selected.")
            schema = cfg.get("schema") or None
            if hasattr(connector, "get_columns"):
                try:
                    source_columns = await asyncio.to_thread(connector.get_columns, object_name, schema)
                except Exception:
                    source_columns = None
            # build_select_all_sql() (connectors/base.py) is the generic
            # capability check for "can this connector genuinely stream
            # this object exhaustively" — SQL-dialect connectors quote the
            # object name via quote_object() (see connectors/database/
            # base.py's override); Salesforce builds a real, unbounded
            # (no LIMIT) SOQL SELECT off its own describe() field list
            # (see SalesforceConnector.build_select_all_sql). Previously
            # this branch was gated on `getattr(connector, "quote_object")`
            # directly, a SQL-only concept Salesforce has no reason to
            # implement — so a Salesforce (or any other build_select_all_sql-
            # capable, non-SQL) source always fell through to
            # preview_object() below, which is a genuinely bounded preview
            # call (Salesforce's own hard-caps it near 2,000 rows) — the
            # actual cause of pipelines truncating a Salesforce object at
            # ~2,000 records regardless of its real size or the Load Type
            # (BULK/AUTO/ROW_BY_ROW) selected on the node.
            extraction_mode = cfg.get("loadType") if conn.connection_type.value == "SALESFORCE" else None
            streamed = False
            try:
                select_sql = connector.build_select_all_sql(object_name, schema)
                await _log(
                    job_id, "INFO",
                    f"Streaming '{object_name}' from {conn.name} exhaustively"
                    + (f" (Load Type: {extraction_mode})" if extraction_mode else "")
                    + f" — no row cap besides the {SOURCE_ROW_HARD_CAP:,}-row in-memory safety ceiling...",
                    node_id,
                )
                rows, columns, truncated = await _stream_all_rows(
                    connector, select_sql, {}, job_id=job_id, node_id=node_id, extraction_mode=extraction_mode,
                )
                cap_note = SOURCE_ROW_HARD_CAP
                streamed = True
                await _log(job_id, "INFO", f"Streamed {len(rows):,} row(s) from '{object_name}'.", node_id)
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
    if source_columns:
        df.attrs["source_columns"] = source_columns
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


async def _run_column_mapping(job_id, node_id, df, cfg, organization_id, connector_cache):
    """CM6 — the real ETL execution wiring: resolves the EXACT pinned
    (mappingId, mappingVersion) this node references (server-side
    PUBLISHED re-check, never trusting the config panel's own filtering)
    and applies its real rename/constant/default/transformation/lookup
    rules to this node's actual in-flight DataFrame — not a cosmetic
    end-of-pipeline rename. All the real logic lives in
    execution/mapping_execution.py (kept out of this already-1300-line
    module, per this build's "do not put everything in one file"
    instruction); this wrapper is just the session/logging plumbing every
    other DB-touching node handler in this file already has (see
    _run_reconciliation/_run_target for the same shape)."""
    df = df if df is not None else pd.DataFrame()
    mapping_id = cfg.get("mappingId")
    mapping_version = cfg.get("mappingVersion")
    if not mapping_id or not mapping_version:
        await _log(job_id, "WARN", "Column Mapping node has no mapping attached — passed data through unchanged.", node_id)
        return df, "skipped", []

    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        try:
            version = await mapping_execution.resolve_mapping_version(db, mapping_id, mapping_version, organization_id)
        except mapping_execution.MappingExecutionError as exc:
            raise NodeExecutionError(str(exc)) from exc

        await _log(job_id, "INFO", f"Applying Column Mapping {mapping_id} v{mapping_version} ({len(version.mappings or [])} mapped column(s)) to {len(df)} row(s)…", node_id)

        async def _log_fn(level: str, message: str) -> None:
            await _log(job_id, level, message, node_id)

        try:
            mapped_df, rejects = await mapping_execution.apply_mapping_to_dataframe(
                db, df, version, organization_id=organization_id, connector_cache=connector_cache, log=_log_fn,
            )
        except mapping_execution.MappingExecutionError as exc:
            raise NodeExecutionError(str(exc)) from exc

        if rejects:
            await reject_service.record_rejects(
                organization_id=organization_id, source_type=RejectSourceType.PIPELINE_JOB, source_id=job_id,
                node_id=node_id, target_object=version.target_object, rejects=rejects,
            )
            await _log(job_id, "WARN", f"{len(rejects)} row(s) rejected by Column Mapping (on_conversion_failure=REJECT_RECORD) — see Rejected Records.", node_id)

    status = "warning" if rejects else "success"
    await _log(job_id, "INFO", f"Column Mapping applied — {len(mapped_df)} row(s) out, {len(rejects)} rejected.", node_id)
    return mapped_df, status, rejects


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


def _infer_series_data_type(series) -> str:
    """Infers a connector-style type-hint string (NUMERIC/BOOLEAN/
    TIMESTAMP/VARCHAR — what anomaly/profiler.py's classify_type()
    expects) from a pandas Series' actual values, for the inline
    (no-jobId) Anomaly Detection node config path below.

    Trusting str(series.dtype) alone is wrong here: a Postgres NUMERIC/
    DECIMAL column comes back as Python Decimal objects, which pandas
    stores under dtype "object" (not "float64") — classify_type("OBJECT")
    falls through to "string", silently disabling Z_SCORE/MODIFIED_Z_SCORE/
    IQR/PERCENTILE for the most common real-world numeric column type.
    Sample real values instead of trusting the dtype label whenever the
    dtype itself is the uninformative "object" bucket.
    """
    dtype_str = str(series.dtype)
    if dtype_str != "object":
        return dtype_str  # numpy-native dtype (int64/float64/bool/datetime64[ns]) — classify_type() already maps these correctly
    sample = series.dropna()
    if sample.empty:
        return dtype_str
    v = sample.iloc[0]
    if isinstance(v, bool):
        return "BOOLEAN"
    if isinstance(v, (int, float, Decimal)):
        return "NUMERIC"
    if isinstance(v, (datetime, date)):
        return "TIMESTAMP"
    return "VARCHAR"


async def _run_anomaly_detection(job_id, node_id, df, cfg, organization_id, primary_connection_id, cache):
    """AD10 — real anomaly detection against the pipeline's actual
    in-flight DataFrame at this node, via DataFrameRowSource (see
    anomaly/dataframe_source.py's module docstring for why this is
    genuinely real rather than a passthrough — same detectors/profiler
    every standalone anomaly run uses, tier-2 streaming path since there's
    no live connector). Node placement in the graph (before vs after the
    Target node) is what makes this PRE_LOAD vs POST_LOAD in practice —
    there's no separate "mode" branch to write, the node just sees
    whatever DataFrame is flowing through it at that point.

    If cfg["jobId"] references a real AnomalyJob (§44 — "prefer
    referencing", never duplicating config), this run genuinely persists
    as another real AnomalyExecution against that job (trigger="Pipeline"),
    reading/recording real baseline history exactly like a manual "Run
    Now" — visible in Anomaly Results/Execution Detail like any other
    run. The job's own columns/detection_config/baseline_config/
    threshold_config are used (not this node's own fields, which are then
    only the inline-mode fallback below). Without a jobId, detection runs
    from this node's own inline config with no baseline history (cold
    start — every run compares against itself) and nothing persisted to
    the anomaly module (there's no AnomalyJob to attach rows to) — only
    logged into this pipeline job's own log, same "stored + displayed vs
    genuinely wired" honesty split used elsewhere in this file.

    On-critical actions always come from THIS NODE's own cfg["onCritical"]
    (not the referenced job's on_critical, which isn't wired to anything
    yet — see AD11) — {create_incident, stop_pipeline}. "Mark pipeline
    failed" and "stop pipeline" are the same real mechanism here (raising
    NodeExecutionError both stops the run and marks the PipelineJob
    FAILED) — there's no third, softer "continue but mark failed" state
    this executor supports, so the config UI doesn't offer one.
    """
    df = df if df is not None else pd.DataFrame()
    if cfg.get("enabled") is False:
        await _log(job_id, "INFO", "Anomaly Detection node is disabled — passed data through unchanged.", node_id)
        return df, "skipped"
    if len(df) == 0:
        await _log(job_id, "WARN", "Anomaly Detection node — no rows to check.", node_id)
        return df, "skipped"

    from app.anomaly import baseline as anomaly_baseline
    from app.anomaly import engine as anomaly_engine
    from app.anomaly import scoring as anomaly_scoring
    from app.anomaly.dataframe_source import DataFrameRowSource
    from app.anomaly.profiler import classify_type, profile_columns
    from app.models.anomaly import AnomalyExecution, AnomalyExecutionStatus, AnomalyJob, ColumnResultStatus

    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)

    job_ref_id = cfg.get("jobId")
    anomaly_job = None
    if job_ref_id:
        async with sessionmaker() as db:
            candidate = await db.get(AnomalyJob, int(job_ref_id))
            if candidate and (organization_id is None or candidate.organization_id == organization_id):
                anomaly_job = candidate
            elif candidate:
                await _log(job_id, "WARN", f"Anomaly Job #{job_ref_id} does not belong to this organization — running inline config instead.", node_id)

    if anomaly_job:
        columns = anomaly_job.columns or []
        detection_config = anomaly_job.detection_config or {}
        baseline_config = anomaly_job.baseline_config or {}
        bands = anomaly_scoring.bands_from_config(anomaly_job.threshold_config or {})
        label = anomaly_job.name
    else:
        wanted = [c for c in (cfg.get("columnNames") or []) if c in df.columns]
        missing = [c for c in (cfg.get("columnNames") or []) if c not in df.columns]
        if missing:
            await _log(job_id, "WARN", f"Anomaly Detection — column(s) not present in this node's data, skipped: {', '.join(missing)}.", node_id)
        columns = cfg.get("columns") or [{"name": c, "data_type": _infer_series_data_type(df[c])} for c in wanted]
        detection_config = {"methods": cfg.get("methods") or [], "id_column": cfg.get("idColumn") or None}
        baseline_config = {}
        bands = anomaly_scoring.DEFAULT_BANDS
        label = cfg.get("label") or "Pipeline Anomaly Check"

    if not columns:
        await _log(job_id, "WARN", "Anomaly Detection node has no columns configured — skipped.", node_id)
        return df, "skipped"

    row_source = DataFrameRowSource(df, label=label)
    stats = profile_columns(row_source, None, node_id, columns)
    total_records = stats.get("__total__") or len(df)

    job_wide_methods = detection_config.get("methods") or []
    job_wide_params = detection_config.get("params") or {}
    id_column = detection_config.get("id_column")
    per_column_config = detection_config.get("columns") or {}

    async with sessionmaker() as db:
        execution = None
        if anomaly_job:
            execution = AnomalyExecution(
                job_id=anomaly_job.id, organization_id=organization_id, status=AnomalyExecutionStatus.RUNNING,
                trigger="Pipeline",
            )
            db.add(execution)
            await db.flush()

        column_results = []  # real AnomalyColumnResult rows (job path) or lightweight stand-ins (inline path)
        for col in columns:
            name = col["name"]
            kind = classify_type(col.get("data_type", ""))
            current_stats = dict(stats.get(name) or {})
            current_stats.setdefault("data_type", kind)

            baseline_stats = None
            if anomaly_job:
                baseline_stats = await anomaly_baseline.resolve_baseline(
                    db, job_id=anomaly_job.id, column_name=name, baseline_config=baseline_config,
                )

            col_config = per_column_config.get(name) or {}
            methods = col_config.get("methods") or job_wide_methods
            records_count = current_stats.get("count") or total_records or 0

            hits = []
            for method in methods:
                params = anomaly_engine._resolve_params(method, job_wide_params, col_config.get("params") or {})
                hits.extend(anomaly_engine._run_detector(
                    method, row_source=row_source, column=name, kind=kind,
                    current_stats=current_stats, baseline_stats=baseline_stats,
                    id_column=id_column, params=params, job_config=detection_config,
                ))

            if anomaly_job:
                col_result = await anomaly_engine._persist_column_result(
                    db, execution_id=execution.id, column_name=name, data_type=kind, records=records_count,
                    hits=hits, baseline_stats=baseline_stats or {}, current_stats=current_stats, bands=bands,
                )
                column_results.append(col_result)
            else:
                severities = [anomaly_scoring.severity_for_score(anomaly_scoring.normalize_score(h.raw_score, bands), bands) for h in hits]
                severity = anomaly_engine._severity_rollup(severities)
                stand_in = type("InlineColumnResult", (), {})()
                stand_in.status, stand_in.anomalies, stand_in.column_name = anomaly_engine._column_status(severity), len(hits), name
                column_results.append(stand_in)

        overall_status = anomaly_engine._overall_status([cr.status for cr in column_results])
        anomalies_found = sum(cr.anomalies for cr in column_results)

        if anomaly_job and execution:
            execution.records_scanned = total_records
            execution.columns_scanned = len(columns)
            execution.anomalies_found = anomalies_found
            execution.critical_count = sum(1 for cr in column_results if cr.status == ColumnResultStatus.CRITICAL)
            execution.warning_count = sum(1 for cr in column_results if cr.status == ColumnResultStatus.WARNING)
            execution.overall_status = overall_status
            execution.status = AnomalyExecutionStatus.SUCCESS
            execution.completed_at = datetime.now(timezone.utc)

        await db.commit()
        execution_id = execution.id if execution else None

    await _log(
        job_id, "INFO",
        f"Anomaly Detection '{label}' — {anomalies_found} anomalies across {len(columns)} column(s), overall {overall_status.value}."
        + (f" Recorded as real execution #{execution_id}." if execution_id else " Inline check — not linked to a saved Anomaly Job, nothing persisted to the anomaly module."),
        node_id,
    )

    is_critical = overall_status.value == "CRITICAL"
    is_warning = overall_status.value == "WARNING"
    on_critical = cfg.get("onCritical") or {}
    node_status = "error" if (is_critical and on_critical.get("stop_pipeline")) else ("warning" if (is_critical or is_warning) else "success")

    if is_critical and on_critical.get("create_incident"):
        try:
            from app.services import incident_service
            async with sessionmaker() as db2:
                pj = await db2.get(PipelineJob, job_id)
                await incident_service.report_incident(
                    db2, organization_id=organization_id, source="ANOMALY_DETECTION",
                    title=f"Critical anomalies — {label}",
                    description=f"{anomalies_found} anomalies found across {len(columns)} column(s) during pipeline execution.",
                    severity="CRITICAL", pipeline_id=pj.pipeline_id if pj else None,
                )
                await _log(job_id, "INFO", "Anomaly Detection: incident created for CRITICAL result.", node_id)
        except Exception as exc:  # never let incident filing sink an otherwise-real detection result
            await _log(job_id, "WARN", f"Anomaly Detection: could not create incident: {exc}", node_id)

    if is_critical and on_critical.get("stop_pipeline"):
        raise NodeExecutionError(f"Anomaly Detection '{label}' found CRITICAL anomalies ({anomalies_found} total) — pipeline stopped per on-critical config.")

    return df, node_status


# ── Invoice Intelligence node group (INV24) — see module docstring above ──

async def _resolve_connection_row(connection_id: int, organization_id: int | None) -> Connection:
    """document_schedule_service's discovery functions want the real
    Connection row itself (they build their own connector internally),
    not the (conn, connector) tuple _resolve_connector() caches for the
    generic transform nodes above."""
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    async with sessionmaker() as db:
        conn = await get_connection(db, connection_id, organization_id)
        if conn is None:
            raise NodeExecutionError(f"Connection {connection_id} was not found or no longer belongs to this organization.")
        return conn


async def _run_invoice_intake(job_id, node_id, cfg, organization_id) -> pd.DataFrame:
    import types

    from app.connectors.base import ConnectorError as _ConnectorError
    from app.models.document_schedule import DocumentSourceStatus
    from app.services import document_schedule_service as doc_sched_svc
    from app.services import document_service

    connection_id = cfg.get("connectionId")
    if not connection_id:
        raise NodeExecutionError("Invoice Intake node has no connection configured.")
    source_path = cfg.get("path") or "/"
    batch_size = int(cfg.get("batchSize") or 50)
    # A plain, never-persisted stand-in for the two fields discover_files()
    # reads off a real DocumentScheduleConfig row — that model's schedule_id
    # is a real, NOT NULL FK to schedules.id, so a Pipeline Builder-driven
    # intake (no Schedule involved at all) genuinely can't create one; this
    # avoids fabricating a fake Schedule just to satisfy an unrelated FK.
    fake_config = types.SimpleNamespace(file_pattern=cfg.get("filePattern") or None, recursive_scan=bool(cfg.get("recursive", False)))

    connection = await _resolve_connection_row(int(connection_id), organization_id)
    try:
        files = await doc_sched_svc.discover_files(connection, fake_config)
    except _ConnectorError as exc:
        raise NodeExecutionError(f"Could not list files at '{source_path}': {connection._scrub(str(exc)) if hasattr(connection, '_scrub') else exc}") from exc
    files = files[: max(1, batch_size)]
    await _log(job_id, "INFO", f"Invoice Intake — discovered {len(files)} file(s) at '{source_path}'.", node_id)

    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    rows: list[dict] = []
    async with sessionmaker() as db:
        for file_info in files:
            try:
                content_hash = await doc_sched_svc.compute_file_hash(connection, file_info.path)
            except _ConnectorError as exc:
                await _log(job_id, "WARN", f"Could not hash '{file_info.name}': {exc} — skipped.", node_id)
                continue

            # schedule_id=None/run_id=None — this row is real (same
            # DocumentSourceRegistry idempotency ledger a Schedule uses),
            # just attributed to a Pipeline Builder run instead of a
            # Schedule; needs_processing() below works identically either way.
            registry = await doc_sched_svc.upsert_registry_entry(
                db, organization_id=organization_id, schedule_id=None, connection=connection,
                source_path=source_path, file_info=file_info, content_hash=content_hash, run_id=None,
            )
            if not doc_sched_svc.needs_processing(registry):
                rows.append({"document_id": registry.document_id, "filename": file_info.name, "intake_status": "ALREADY_PROCESSED"})
                continue
            if not doc_sched_svc.is_supported_file(file_info.name):
                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.UNSUPPORTED)
                await _log(job_id, "WARN", f"'{file_info.name}' is not a supported file type — skipped.", node_id)
                continue

            try:
                stream = await asyncio.to_thread(build_connector(connection).open_read_stream, file_info.path)
                try:
                    content = await asyncio.to_thread(stream.read)
                finally:
                    await asyncio.to_thread(stream.close)
            except _ConnectorError as exc:
                await doc_sched_svc.transition(db, registry, DocumentSourceStatus.FAILED, error=str(exc))
                await _log(job_id, "WARN", f"Could not download '{file_info.name}': {exc}", node_id)
                continue

            doc = await document_service.save_from_source(
                db, content=content, filename=file_info.name, organization_id=organization_id,
                source_connection_id=connection.id, source_path=source_path, source_schedule_id=None,
            )
            registry.document_id = doc.id
            await doc_sched_svc.transition(db, registry, DocumentSourceStatus.QUEUED)
            rows.append({"document_id": doc.id, "filename": doc.filename, "intake_status": "QUEUED"})

    df = pd.DataFrame(rows, columns=["document_id", "filename", "intake_status"])
    queued = int((df["intake_status"] == "QUEUED").sum()) if len(df) else 0
    await _log(job_id, "INFO", f"Invoice Intake finished — {len(df)} document row(s), {queued} newly queued.", node_id)
    return df


async def _run_invoice_processing(job_id, node_id, df, organization_id) -> tuple[pd.DataFrame, str]:
    from app.models.document import Document
    from app.services import document_service

    df = df if df is not None else pd.DataFrame()
    if "document_id" not in df.columns or len(df) == 0:
        await _log(job_id, "WARN", "Invoice Processing — no document(s) to process (upstream node produced none).", node_id)
        return pd.DataFrame(columns=["document_id", "extraction_status"]), "success"

    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    rows: list[dict] = []
    async with sessionmaker() as db:
        for raw_id in df["document_id"].dropna().tolist():
            doc_id = int(raw_id)
            doc = await db.get(Document, doc_id)
            if doc is None:
                rows.append({"document_id": doc_id, "extraction_status": "NOT_FOUND"})
                continue
            doc = await document_service.process_document(db, doc, document_type="INVOICE", skip_invoice_pipeline=True)
            rows.append({"document_id": doc.id, "extraction_status": doc.status.value})
            await _log(job_id, "INFO", f"Invoice Processing — '{doc.filename}': {doc.status.value} ({doc.extraction_percent}% extraction confidence).", node_id)

    out = pd.DataFrame(rows, columns=["document_id", "extraction_status"])
    failed = int((out["extraction_status"] == "FAILED").sum()) if len(out) else 0
    return out, ("warning" if failed else "success")


async def _run_invoice_validation(job_id, node_id, df, organization_id) -> tuple[pd.DataFrame, str]:
    from app.models.document import Document
    from app.models.invoice_intelligence.core import ApInvoiceSource
    from app.services.invoice import invoice_decision_service, invoice_processing_service, invoice_validation_service

    columns = ["document_id", "invoice_id", "invoice_number", "vendor_name", "total", "final_decision"]
    df = df if df is not None else pd.DataFrame()
    if "document_id" not in df.columns or len(df) == 0:
        await _log(job_id, "WARN", "Invoice Validation — no document(s) to validate.", node_id)
        return pd.DataFrame(columns=columns), "success"

    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    rows: list[dict] = []
    async with sessionmaker() as db:
        await invoice_validation_service.seed_default_rules(db, organization_id)
        for raw_id in df["document_id"].dropna().tolist():
            doc_id = int(raw_id)
            doc = await db.get(Document, doc_id)
            if doc is None or not doc.extraction_result or not doc.extraction_result.get("fields"):
                rows.append({"document_id": doc_id, "invoice_id": None, "invoice_number": None, "vendor_name": None, "total": None, "final_decision": "NO_EXTRACTION"})
                continue
            try:
                # ApInvoiceSource.PIPELINE — defined since INV1, never
                # actually set anywhere until this node: a real, disclosed
                # gap this phase closes rather than defaulting to
                # MANUAL_UPLOAD for a document this pipeline itself intook.
                invoice = await invoice_processing_service.materialize_invoice(db, doc, source=ApInvoiceSource.PIPELINE)
            except invoice_processing_service.InvoiceProcessingError as exc:
                await _log(job_id, "WARN", f"Could not materialize invoice for document #{doc_id}: {exc}", node_id)
                rows.append({"document_id": doc_id, "invoice_id": None, "invoice_number": None, "vendor_name": None, "total": None, "final_decision": "MATERIALIZE_FAILED"})
                continue
            results = await invoice_validation_service.evaluate_invoice(db, invoice)
            decision = await invoice_decision_service.decide(db, invoice, results)
            await _log(job_id, "INFO", f"Invoice Validation — '{invoice.invoice_number or invoice.id}': {decision.value}.", node_id)
            rows.append({
                "document_id": doc_id, "invoice_id": invoice.id, "invoice_number": invoice.invoice_number,
                "vendor_name": invoice.vendor_name, "total": invoice.total, "final_decision": decision.value,
            })

    out = pd.DataFrame(rows, columns=columns)
    review_count = int((out["final_decision"] == "REVIEW_REQUIRED").sum()) if len(out) else 0
    return out, ("warning" if review_count else "success")


def _run_invoice_review_gate(df: pd.DataFrame | None, cfg: dict) -> pd.DataFrame:
    df = df if df is not None else pd.DataFrame()
    if "final_decision" not in df.columns or len(df) == 0:
        return df
    pass_on = cfg.get("passOn") or ["STRAIGHT_THROUGH"]
    return df[df["final_decision"].isin(pass_on)].reset_index(drop=True)


async def _run_invoice_erp_posting(job_id, node_id, df, cfg, organization_id) -> tuple[pd.DataFrame, str]:
    from app.models.invoice_intelligence.core import ApInvoice
    from app.models.invoice_intelligence.erp import ApInvoiceErpStatus
    from app.services.invoice import invoice_erp_service

    df = df if df is not None else pd.DataFrame()
    connection_id = cfg.get("erpConnectionId")
    if not connection_id:
        await _log(job_id, "WARN", "ERP Posting node has no ERP connection configured — skipped.", node_id)
        return df, "skipped"
    if "invoice_id" not in df.columns or len(df) == 0:
        await _log(job_id, "WARN", "ERP Posting — no invoice(s) reached this node.", node_id)
        return df, "success"

    settle_timeout = float(cfg.get("settleTimeoutSeconds") or 60)
    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    outcomes: list[str] = []
    for raw_id in df["invoice_id"].dropna().tolist():
        invoice_id = int(raw_id)
        async with sessionmaker() as db:
            invoice = await db.get(ApInvoice, invoice_id)
            if invoice is None:
                outcomes.append("NOT_FOUND")
                continue
            if invoice.erp_status == ApInvoiceErpStatus.SUCCEEDED:
                await _log(job_id, "INFO", f"Invoice #{invoice_id} already posted — skipped.", node_id)
                outcomes.append(invoice.erp_status.value)
                continue
            try:
                await invoice_erp_service.post_invoice(db, invoice, erp_connection_id=int(connection_id))
            except invoice_erp_service.InvoiceErpError as exc:
                await _log(job_id, "WARN", f"Invoice #{invoice_id} could not be posted: {exc}", node_id)
                outcomes.append("POST_REJECTED")
                continue

        # post_invoice() is fire-and-forget (asyncio.create_task, its own
        # session) — poll this invoice's own erp_status for a bounded
        # window rather than reporting a fabricated SUCCEEDED before the
        # real background attempt (with real retry/backoff) finishes.
        # Safe to await here: this whole job already runs as its own
        # background task, not inside an HTTP request.
        elapsed = 0.0
        interval = 0.5
        final_status = None
        while elapsed < settle_timeout:
            await asyncio.sleep(interval)
            elapsed += interval
            async with sessionmaker() as check_db:
                fresh = await check_db.get(ApInvoice, invoice_id)
                if fresh is not None and fresh.erp_status not in (ApInvoiceErpStatus.PENDING, ApInvoiceErpStatus.RETRYING):
                    final_status = fresh.erp_status.value
                    break
        if final_status is None:
            await _log(job_id, "WARN", f"Invoice #{invoice_id} — ERP post still in progress after {settle_timeout:.0f}s; check its status later.", node_id)
            final_status = "PENDING"
        else:
            await _log(job_id, "INFO", f"Invoice #{invoice_id} — ERP post settled: {final_status}.", node_id)
        outcomes.append(final_status)

    out = df.copy()
    out["erp_status"] = outcomes
    failed = sum(1 for o in outcomes if o in ("FAILED", "DEAD_LETTER", "POST_REJECTED"))
    return out, ("warning" if failed else "success")


async def _run_invoice_reconciliation(job_id, node_id, df, organization_id) -> tuple[pd.DataFrame, str]:
    from app.models.invoice_intelligence.core import ApInvoice
    from app.services.invoice import invoice_reconciliation_service

    df = df if df is not None else pd.DataFrame()
    if "invoice_id" not in df.columns or len(df) == 0:
        await _log(job_id, "WARN", "Reconciliation — no invoice(s) reached this node.", node_id)
        return df, "success"

    sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
    statuses: list[str] = []
    async with sessionmaker() as db:
        for raw_id in df["invoice_id"].dropna().tolist():
            invoice_id = int(raw_id)
            invoice = await db.get(ApInvoice, invoice_id)
            if invoice is None:
                statuses.append("NOT_FOUND")
                continue
            if not invoice.erp_transaction_id:
                statuses.append("NOT_POSTED")
                continue
            try:
                recon = await invoice_reconciliation_service.reconcile_invoice(db, invoice)
                await _log(job_id, "INFO", f"Invoice #{invoice_id} reconciliation: {recon.status.value}.", node_id)
                statuses.append(recon.status.value)
            except invoice_reconciliation_service.InvoiceReconciliationError as exc:
                await _log(job_id, "WARN", f"Invoice #{invoice_id} could not be reconciled: {exc}", node_id)
                statuses.append("ERROR")

    out = df.copy()
    out["reconciliation_status"] = statuses
    mismatches = sum(1 for s in statuses if s in ("MISMATCH", "FAILED", "ERROR"))
    return out, ("warning" if mismatches else "success")


def _dataframe_to_xml_bytes(df: pd.DataFrame) -> bytes:
    """Hand-rolled via stdlib xml.etree.ElementTree, not pandas.to_xml() —
    to_xml()'s default parser is lxml, which isn't a project dependency
    (unlike pyarrow for parquet below); ElementTree needs nothing extra
    and matches services/sql_export_writers.py's XmlChunkWriter root/row
    element convention (root="rows", row="row") used by the standalone
    Format Conversion page, for a consistent look across both features."""
    import xml.etree.ElementTree as ET

    root = ET.Element("rows")
    for _, row in df.iterrows():
        row_el = ET.SubElement(root, "row")
        for col, val in row.items():
            child = ET.SubElement(row_el, str(col))
            if val is not None and not (isinstance(val, float) and pd.isna(val)):
                child.text = val.isoformat() if isinstance(val, (date, datetime)) else str(val)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _serialize_dataframe(df: pd.DataFrame, file_type: str, delimiter: str = ",") -> bytes:
    """Shared by the 'file' (local disk) and 'sftp' (remote upload) target
    branches below — same real pandas serialization either way, only the
    destination differs. Whole-file-in-memory, same disclosed bound as
    every other in-memory target in this engine (SOURCE_ROW_CAP-sized
    DataFrames, not a streaming writer).

    Real gap found live: this only ever emitted CSV/Excel/JSON — a user
    building a pipeline in Transform Workspace had no way to write XML or
    Parquet output, even though the standalone Format Conversion page
    (services/sql_export_writers.py) already writes both. XML reuses that
    page's root/row element convention (not its ChunkWriter class, since
    this function serializes an already-materialized DataFrame rather than
    a RowSource's chunk stream); Parquet reuses the same pyarrow dependency
    via pandas.DataFrame.to_parquet() — same Decimal/datetime->float/string
    lossiness disclosed in ParquetChunkWriter's own docstring applies here
    too, since pandas' pyarrow engine has the same underlying limitation."""
    buf = io.BytesIO()
    if file_type == "excel":
        df.to_excel(buf, index=False)
    elif file_type == "json":
        buf.write(df.to_json(orient="records", indent=2).encode())
    elif file_type == "xml":
        buf.write(_dataframe_to_xml_bytes(df))
    elif file_type == "parquet":
        df.to_parquet(buf, index=False)
    else:
        buf.write(df.to_csv(index=False, sep=delimiter).encode())
    return buf.getvalue()


def _file_ext(file_type: str) -> str:
    return {"csv": "csv", "excel": "xlsx", "json": "json", "xml": "xml", "parquet": "parquet"}.get(file_type, "csv")


def _metadata_columns_for_dataframe(source_columns: list[ColumnInfo] | None, df: pd.DataFrame) -> list[ColumnInfo] | None:
    if not source_columns or df is None or len(df.columns) == 0:
        return None
    by_name = {str(c.name).lower(): c for c in source_columns}
    resolved: list[ColumnInfo] = []
    for name in df.columns:
        col = by_name.get(str(name).lower())
        if col is None:
            return None
        resolved.append(col)
    return resolved


async def _log_effective_target_config(
    job_id: int,
    node_id: str,
    target_conn: Connection,
    schema: str | None,
    table: str,
    load_config: TargetLoadConfig,
    *,
    batch_size: int,
    insert_method: str,
) -> None:
    env = target_conn.environment.value if hasattr(target_conn.environment, "value") else str(target_conn.environment or "Default")
    target_name = f"{schema}.{table}" if schema else table
    await _log(job_id, "INFO", "=" * 60, node_id)
    await _log(job_id, "INFO", "EFFECTIVE TARGET CONFIGURATION", node_id)
    await _log(job_id, "INFO", "=" * 60, node_id)
    await _log(job_id, "INFO", f"Connection: {target_conn.name}", node_id)
    await _log(job_id, "INFO", f"Database Type: {target_conn.connection_type.value}", node_id)
    await _log(job_id, "INFO", f"Environment: {env}", node_id)
    await _log(job_id, "INFO", f"Target: {target_name}", node_id)
    await _log(job_id, "INFO", f"Load Mode: {load_config.load_mode}", node_id)
    await _log(job_id, "INFO", f"Load Strategy: {load_config.load_strategy}", node_id)
    await _log(job_id, "INFO", f"Drop Existing: {'YES' if load_config.drop_existing else 'NO'}", node_id)
    await _log(job_id, "INFO", f"Create Table: {'YES' if load_config.create_table else 'NO'}", node_id)
    await _log(job_id, "INFO", f"Truncate First: {'YES' if load_config.truncate_first else 'NO'}", node_id)
    await _log(job_id, "INFO", f"Batch Size: {batch_size:,}", node_id)
    await _log(job_id, "INFO", f"Insert Method: {insert_method.upper()}", node_id)
    await _log(job_id, "INFO", "=" * 60, node_id)


async def _run_target(job_id, node_id, df, cfg, organization_id, primary_connection_id, cache, pipeline_id, job_id_for_path, source_columns=None):
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
    dataset_name = (cfg.get("datasetName") or "").strip().strip('"\'')
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

    load_config = normalize_target_load_config(cfg)
    await _log_effective_target_config(
        job_id, node_id, conn, None, dataset_name, load_config,
        batch_size=5000, insert_method="bulk",
    )

    # Create/recreate the target table from the real DataFrame's dtypes
    # (loading/writers/base.py's ensure_table — generic across every
    # SQLAlchemy-backed writer via reflection/DDL, not fabricated per
    # vendor). create_if_missing/drop_recreate were derived above, either
    # from the new Load Strategy panel's fullLoadStrategy or (for
    # pipelines saved before it existed) the legacy createIfNotExists/
    # dropAndRecreate fields directly.
    table_status = "exists"
    try:
        if load_config.create_table or load_config.drop_existing:
            await _update_job_metrics(job_id, {
                "stage": "PREPARING_TARGET",
                "processed": len(df),
                "loaded": 0,
            })
            if load_config.drop_existing:
                await _log(job_id, "WARN", f"Drop + Recreate selected for target table '{dataset_name}' — existing table will be dropped before load if it exists.", node_id)
            source_cols = _metadata_columns_for_dataframe(source_columns, df)

            if source_cols:
                await _log(job_id, "INFO", f"Preparing target table '{dataset_name}' from source metadata ({len(source_cols)} column definitions).", node_id)
                table_status = await asyncio.to_thread(
                    writer.ensure_table_from_columns,
                    None,
                    dataset_name,
                    source_cols,
                    recreate=load_config.drop_existing,
                    recreate_incompatible=load_config.truncate_first,
                )
            else:
                await _log(job_id, "WARN", f"Source metadata was not available for all output columns; preparing target table '{dataset_name}' from DataFrame sample types.", node_id)
                table_status = await asyncio.to_thread(writer.ensure_table, None, dataset_name, df, recreate=load_config.drop_existing)
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
    if load_config.truncate_first and table_status == "exists":
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
            batch_id = (i // 5000) + 1
            await _log(job_id, "INFO", f"[TARGET] Insert started: batch={batch_id}, rows={len(batch):,}, table={dataset_name}", node_id)
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
            await _log(
                job_id,
                "INFO",
                f"[TARGET] Commit completed: batch={batch_id}, successful={batch_written:,}, rejected={len(batch_rejects):,}, loaded_total={written:,}",
                node_id,
            )
    except ConnectorError as exc:
        raise NodeExecutionError(f"Could not write to target table '{dataset_name}': {connector._scrub(str(exc))}") from exc
    except Exception as exc:
        await _log(job_id, "ERROR", f"[TARGET] Insert failed for '{dataset_name}': {connector._scrub(str(exc))}", node_id)
        raise NodeExecutionError(f"Could not write to target table '{dataset_name}': {connector._scrub(str(exc))}") from exc
    if rejected:
        await reject_service.record_rejects(
            organization_id=organization_id, source_type=RejectSourceType.PIPELINE_JOB, source_id=job_id,
            node_id=node_id, target_object=dataset_name, rejects=rejected,
        )
        await _log(
            job_id, "ERROR",
            f"{len(rejected)} row(s) rejected writing to '{dataset_name}' — see Rejected Records for each row and its real error.",
            node_id,
        )
        raise NodeExecutionError(f"Target load failed: {len(rejected)} row(s) were rejected writing to '{dataset_name}'.")
    await _log(job_id, "INFO", f"Wrote {written} row(s) to '{dataset_name}' ({load_config.load_mode} / {load_config.load_strategy}).", node_id)
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

# ── Streaming fast path — real concurrent extract+load ─────────────
# Bug report, verbatim: "when extracting data from source to target using
# pipeline then its trying to fetch full data from source then starting
# load into target. it should not like this. data fetch and load should
# work simuntenously for better performance and time efficient." — real,
# observed live: a NetSuite -> Database pipeline's job log showed
# sequential "Fetched chunk N" lines with zero interleaved load activity,
# because _run_source (above) fully drains _stream_all_rows into one
# DataFrame before _run_target ever runs.
#
# Both halves were ALREADY chunked internally (_stream_all_rows's own
# fetch_chunk loop; _run_target's own 5,000-row write_batch_with_fallback
# loop) — they just ran one fully after the other, node by node, the way
# this whole engine's node-by-node/full-DataFrame-per-node model requires.
# Rewriting that model for every node type (sort/dedup/aggregate/join/
# validation/reconciliation/anomaly-detection all genuinely need to see
# the full dataset — this is a real constraint, not laziness) is a much
# larger undertaking than this fix; instead this adds a real, narrowly-
# scoped fast path for the single most common pipeline shape — a linear
# chain of exactly one Source Dataset node, zero or more purely row-wise/
# stateless transforms (filter/select/rename/cast/calculated/conditional/
# mapping — verified stateless per-chunk: none of them reference any row
# other than the one they're on), and one plain full-load Database target
# — where each fetched chunk is transformed and written to the target
# immediately, before the next chunk is even requested. Any other shape
# (a blocking transform, a join, multiple targets, a notification node,
# an SCD/file/SFTP/API target, ...) safely falls through to the existing,
# unchanged, full-materialize engine below — this never partially applies.
_STREAMABLE_TRANSFORM_FNS = {
    "filter": _run_filter,
    "select": _run_select,
    "rename": _run_rename,
    "cast": _run_cast,
    "calculated": _run_calculated,
    "conditional": _run_conditional,
    "mapping": _run_mapping,
}


def _find_streaming_chain(nodes: dict[str, dict], edges: list[dict], order: list[str]) -> list[str] | None:
    """Returns the ordered [source, *transforms, target] node-id chain
    when the WHOLE pipeline is exactly that shape (no branching, no extra
    nodes anywhere), or None when it isn't — see module note above for
    what disqualifies a pipeline. Pure inspection of the saved node/edge
    config; makes no connector calls and has no side effects."""
    if len(order) < 2:
        return None
    out_edges: dict[str, list[str]] = {}
    in_count: dict[str, int] = {n: 0 for n in nodes}
    for e in edges:
        out_edges.setdefault(e["sourceId"], []).append(e["targetId"])
        in_count[e["targetId"]] = in_count.get(e["targetId"], 0) + 1

    node_id = order[0]
    node = nodes[node_id]
    if node.get("type") != "source" or in_count.get(node_id, 0) != 0:
        return None
    scfg = node.get("config", {}) or {}
    if scfg.get("uploadedFileId") or scfg.get("objectType") == "API" or not scfg.get("connectionId"):
        return None  # none of these go through the chunked open_sql_stream() primitive

    chain = [node_id]
    while True:
        outs = out_edges.get(node_id, [])
        if len(outs) != 1:
            return None
        node_id = outs[0]
        if in_count.get(node_id, 0) != 1:
            return None
        node = nodes.get(node_id)
        if node is None:
            return None
        ntype = node.get("type")
        if ntype == "target":
            tcfg = node.get("config", {}) or {}
            if tcfg.get("targetType", "dataset") != "dataset":
                return None  # file/sftp/api targets stay on the existing path
            if tcfg.get("loadType") == "delta":
                return None  # SCD needs the whole result set for its merge, not a per-chunk one
            chain.append(node_id)
            break
        if ntype not in _STREAMABLE_TRANSFORM_FNS:
            return None
        chain.append(node_id)
        if len(chain) > len(nodes):
            return None  # defensive only — _topo_sort already rejects real cycles
    if out_edges.get(node_id):
        return None  # the target must be a true sink

    return chain if set(chain) == set(order) else None


async def _execute_streaming_chain(
    job_id: int, chain: list[str], nodes: dict[str, dict], organization_id: int | None,
    connector_cache: dict[int, tuple], pipeline_id: int,
) -> tuple[int, int, bool] | None:
    """Runs the chain _find_streaming_chain() identified as one real
    interleaved extract-transform-load loop. Returns (total_in, total_out,
    cancelled), or None when the source connector turns out not to
    support streaming after all (build_select_all_sql/open_sql_stream
    raising ConnectorUnsupportedError — discovered only by calling them,
    unlike everything _find_streaming_chain checks) — always discovered
    before any target table has been touched, so the caller can fall back
    to the normal engine with zero side effects having happened yet."""
    source_id, target_id = chain[0], chain[-1]
    transform_ids = chain[1:-1]
    source_node, target_node = nodes[source_id], nodes[target_id]
    scfg = source_node.get("config", {}) or {}
    tcfg = target_node.get("config", {}) or {}

    conn, connector = await _resolve_connector(scfg["connectionId"], organization_id, connector_cache)
    object_name = scfg.get("object")
    schema = scfg.get("schema") or None
    custom_sql = scfg.get("customSql")
    params = {p["name"]: p["value"] for p in scfg.get("parameters", []) if p.get("name")} if custom_sql else {}
    extraction_mode = scfg.get("loadType") if conn.connection_type.value == "SALESFORCE" else None

    dataset_name = (tcfg.get("datasetName") or "").strip().strip('"\'')
    if not dataset_name:
        raise NodeExecutionError("Dataset target has no dataset/table name configured.")
    target_connection_id = tcfg.get("connectionId") or scfg["connectionId"]
    target_conn, target_connector = await _resolve_connector(target_connection_id, organization_id, connector_cache)
    try:
        writer = get_bulk_writer(target_conn.connection_type.value, target_connector)
    except ConnectorError as exc:
        raise NodeExecutionError(str(exc)) from exc

    load_config = normalize_target_load_config(tcfg)

    src_info = {
        'type': 'database',
        'dbType': conn.connection_type.value,
        'env': conn.environment.value if hasattr(conn.environment, 'value') else str(conn.environment or 'Default'),
        'table': object_name or 'CustomQuery',
        'query': (custom_sql.replace('\n', ' ')[:80] + '...') if custom_sql and len(custom_sql) > 80 else (custom_sql or None),
        'useBulkApi': extraction_mode or 'auto'
    }
    tgt_info = {
        'type': 'database',
        'dbType': target_conn.connection_type.value,
        'env': target_conn.environment.value if hasattr(target_conn.environment, 'value') else str(target_conn.environment or 'Default'),
        'table': dataset_name,
        **load_config.as_log_dict(),
        'batchSize': SOURCE_STREAM_CHUNK_SIZE,
        'insertMethod': 'bulk'
    }
    target_label = target_conn.connection_type.value.replace('_', ' ').title()

    await _log(job_id, "INFO", f"Source Config: {src_info}", source_id)
    await _log(job_id, "INFO", f"Target Config: {tgt_info}", target_id)
    await _log_effective_target_config(
        job_id, target_id, target_conn, None, dataset_name, load_config,
        batch_size=SOURCE_STREAM_CHUNK_SIZE, insert_method="bulk",
    )
    target_prepared = False
    if object_name and not transform_ids:
        await _update_job_metrics(job_id, {
            "stage": "PREPARING_TARGET",
            "extracted": 0,
            "processed": 0,
            "loaded": 0,
            "rejected": 0,
        })
        await _log(job_id, "INFO", "Target preparation started before source extraction.", target_id)
        table_status = "exists"
        try:
            if load_config.create_table or load_config.drop_existing:
                if load_config.drop_existing:
                    await _log(job_id, "WARN", f"Drop-and-create strategy selected. Dropping target '{dataset_name}' before load if it exists.", target_id)
                source_cols = await asyncio.to_thread(connector.get_columns, object_name, schema) if hasattr(connector, "get_columns") else None
                if source_cols and hasattr(connector, "_queryable_field_names"):
                    queryable_names = set(await asyncio.to_thread(connector._queryable_field_names, object_name))
                    source_cols = [c for c in source_cols if c.name in queryable_names]
                if source_cols:
                    await _log(job_id, "INFO", f"Creating target table '{dataset_name}' from source metadata ({len(source_cols)} column definitions).", target_id)
                    table_status = await asyncio.to_thread(
                        writer.ensure_table_from_columns,
                        None,
                        dataset_name,
                        source_cols,
                        recreate=load_config.drop_existing,
                        recreate_incompatible=load_config.truncate_first,
                    )
                else:
                    raise NodeExecutionError("Target preparation failed: source metadata is required before extraction for this load strategy.")
                await _log(job_id, "INFO", f"Target table '{dataset_name}' {table_status}.", target_id)
            elif not await asyncio.to_thread(writer.table_exists, None, dataset_name):
                raise NodeExecutionError(f"Target table '{dataset_name}' does not exist. Append mode requires an existing target unless create-table is explicitly enabled.")

            if load_config.truncate_first and table_status == "exists":
                await _log(job_id, "INFO", f"Truncating target table '{dataset_name}' before load.", target_id)
                await asyncio.to_thread(writer.truncate, None, dataset_name)
                await _log(job_id, "INFO", f"Target table '{dataset_name}' truncated successfully.", target_id)
            await _log(job_id, "INFO", "Target preparation complete.", target_id)
            target_prepared = True
        except NodeExecutionError:
            raise
        except Exception as exc:
            raise NodeExecutionError(f"Target preparation failed for '{dataset_name}': {target_connector._scrub(str(exc))}") from exc
    await _log(job_id, "INFO", "Reading source data...", source_id)
    await _log(job_id, "INFO", f"Connecting to source: {conn.connection_type.value} / {conn.name}", source_id)
    await _log(job_id, "INFO", f"Use Bulk API setting: {extraction_mode or 'auto'}", source_id)
    await _log(job_id, "INFO", f"Using streaming ETL for {conn.connection_type.value} to {target_conn.connection_type.value}...", source_id)

    try:
        select_sql = connector.build_select_all_sql(object_name, schema) if object_name else custom_sql
    except ConnectorUnsupportedError:
        return None

    if object_name:
        log_sql = f"SELECT * FROM {object_name}"
    else:
        one_line = " ".join((custom_sql or "").split())
        log_sql = one_line[:80] + "..." if len(one_line) > 80 else one_line

    await _log(job_id, "INFO", f"Starting streaming ETL: {log_sql}...", source_id)
    await _log(job_id, "INFO", f"Target: {dataset_name}, Batch size: {SOURCE_STREAM_CHUNK_SIZE}, Load: {load_config.load_mode} / {load_config.load_strategy}", target_id)

    main_loop = asyncio.get_running_loop()
    def _thread_log(level, msg):
        asyncio.run_coroutine_threadsafe(_log(job_id, level, msg, source_id), main_loop)

    try:
        stream = await asyncio.to_thread(
            connector.open_sql_stream,
            select_sql,
            params,
            extraction_mode=extraction_mode,
            log_fn=_thread_log,
            cancel_check=lambda: is_cancelled(job_id),
        )
    except ConnectorUnsupportedError:
        return None
    except ConnectorError as exc:
        raise NodeExecutionError(connector._scrub(str(exc))) from exc

    table_ready = target_prepared
    total_in = total_out = chunk_num = 0
    rejected_total: list[dict] = []
    transform_rows_in = {nid: 0 for nid in transform_ids}
    transform_rows_out = {nid: 0 for nid in transform_ids}
    cancelled = False
    source_t0 = datetime.now(timezone.utc)

    total_rows = None
    if object_name and hasattr(connector, "row_count"):
        try:
            total_rows = await asyncio.to_thread(connector.row_count, object_name, schema)
        except Exception:
            total_rows = None

    if total_rows is not None:
        await _log(job_id, "INFO", f"Estimated total records: {total_rows:,}", source_id)

    await _update_job_metrics(job_id, {
        "stage": "DOWNLOADING_BULK_RESULTS" if conn.connection_type.value == "SALESFORCE" else "FETCHING",
        "total_rows": total_rows,
        "extracted": 0,
        "processed": 0,
        "loaded": 0,
        "rejected": 0,
        "current_chunk": 0,
        "chunks_processed": 0,
        "current_chunk_rows": 0,
    })

    try:
        columns = list(stream.columns)
        await _log(job_id, "INFO", f"Source object '{object_name or 'Query'}' has {len(columns)} queryable columns", source_id)
        while True:
            if is_cancelled(job_id):
                cancelled = True
                await _log(job_id, "WARN", "Cancellation requested — stopping before the next chunk.", source_id)
                break
            raw_chunk = await asyncio.to_thread(stream.fetch_chunk, SOURCE_STREAM_CHUNK_SIZE)
            pop_parse_errors = getattr(stream, "pop_parse_errors", None)
            if pop_parse_errors:
                page_parse_errors = pop_parse_errors()
                if page_parse_errors:
                    rejected_total.extend(page_parse_errors)
                    await _log(
                        job_id, "WARN",
                        f"{len(page_parse_errors)} source row(s) skipped during extraction (malformed/oversized field) — see Rejected Records; extraction continues.",
                        source_id,
                    )
            if not raw_chunk:
                break
            chunk_num += 1
            total_in += len(raw_chunk)

            await _update_job_metrics(job_id, {
                "stage": "TRANSFORMING" if transform_ids else "FETCHING",
                "total_rows": total_rows,
                "extracted": total_in,
                "processed": total_in,
                "loaded": total_out,
                "rejected": len(rejected_total),
                "current_chunk": chunk_num,
                "chunks_processed": max(0, chunk_num - 1),
                "current_chunk_rows": len(raw_chunk),
            })

            await _log(job_id, "INFO", f"Producer Bulk batch {chunk_num}: {len(raw_chunk):,} records (Total: {total_in:,})", source_id)

            df = pd.DataFrame(raw_chunk, columns=columns)
            for nid in transform_ids:
                node = nodes[nid]
                transform_rows_in[nid] += len(df)
                df = _STREAMABLE_TRANSFORM_FNS[node["type"]](df, node.get("config", {}) or {})
                transform_rows_out[nid] += len(df)

            if not table_ready:
                await _log(job_id, "INFO", f"Connected to target database: {target_conn.connection_type.value} / {target_conn.name}", target_id)
                await _update_job_metrics(job_id, {
                    "stage": "PREPARING_TARGET",
                    "extracted": total_in,
                    "processed": total_in,
                    "loaded": total_out,
                    "total_rows": total_rows,
                    "current_chunk": chunk_num,
                    "current_chunk_rows": len(raw_chunk),
                })
                if load_config.drop_existing:
                    await _log(job_id, "WARN", f"Drop + Recreate selected for target table '{dataset_name}' — existing table will be dropped before load if it exists.", target_id)
                await _log(job_id, "INFO", f"Checking target table existence for '{dataset_name}'...", target_id)
                if load_config.create_table or load_config.drop_existing:
                    source_cols = None
                    s_obj = scfg.get("object") or scfg.get("table")
                    if s_obj and connector and hasattr(connector, "get_columns"):
                        try:
                            source_cols = await asyncio.to_thread(connector.get_columns, s_obj)
                        except Exception:
                            source_cols = None
                    source_cols = _metadata_columns_for_dataframe(source_cols, df)

                    if source_cols:
                        await _log(job_id, "INFO", f"Preparing target table '{dataset_name}' from source metadata ({len(source_cols)} column definitions).", target_id)
                        table_status = await asyncio.to_thread(
                            writer.ensure_table_from_columns,
                            None,
                            dataset_name,
                            source_cols,
                            recreate=load_config.drop_existing,
                            recreate_incompatible=load_config.truncate_first,
                        )
                    else:
                        await _log(job_id, "WARN", f"Source metadata was not available for all output columns; preparing target table '{dataset_name}' from DataFrame sample types.", target_id)
                        table_status = await asyncio.to_thread(writer.ensure_table, None, dataset_name, df, recreate=load_config.drop_existing)
                    if table_status != "exists":
                        await _log(job_id, "INFO", f"Target table '{dataset_name}' {table_status} from the pipeline's output columns.", target_id)
                    else:
                        await _log(job_id, "INFO", f"Target table '{dataset_name}' exists", target_id)
                elif not await asyncio.to_thread(writer.table_exists, None, dataset_name):
                    raise NodeExecutionError(
                        f"Target table '{dataset_name}' does not exist and \"Create table if missing\" is turned off for this node."
                    )
                else:
                    table_status = "exists"
                    await _log(job_id, "INFO", f"Target table '{dataset_name}' exists", target_id)

                if load_config.truncate_first and table_status == "exists":
                    await asyncio.to_thread(writer.truncate, None, dataset_name)
                    await _log(job_id, "INFO", f"Truncated table {dataset_name}", target_id)
                await _log(job_id, "INFO", "Insert method configured as: BULK (with automatic fallback to row-by-row if needed)", target_id)
                table_ready = True

            out_columns = list(df.columns)
            rows = df.to_dict(orient="records")
            for i in range(0, len(rows), 5000):
                if is_cancelled(job_id):
                    cancelled = True
                    await _log(job_id, "WARN", "Cancellation requested during load — stopping batch inserts.", target_id)
                    break
                batch = rows[i:i + 5000]
                batch_id = (chunk_num, (i // 5000) + 1)
                await _log(job_id, "INFO", f"[TARGET] {target_label} insert started: source_batch={batch_id[0]}, load_batch={batch_id[1]}, rows={len(batch):,}", target_id)
                batch_written, batch_rejects = await asyncio.to_thread(
                    write_batch_with_fallback, writer, None, dataset_name, out_columns, batch, key_columns=None, cancel_check=lambda: is_cancelled(job_id)
                )
                total_out += batch_written
                rejected_total.extend(batch_rejects)
                await _log(
                    job_id,
                    "INFO",
                    f"[TARGET] {target_label} commit completed: source_batch={batch_id[0]}, load_batch={batch_id[1]}, successful={batch_written:,}, rejected={len(batch_rejects):,}, loaded_total={total_out:,}",
                    target_id,
                )
            if cancelled:
                break

            elapsed_sec = max(0.1, (datetime.now(timezone.utc) - source_t0).total_seconds())
            rate = round(total_out / elapsed_sec)
            await _log(
                job_id, "INFO",
                f"Batch {chunk_num}: Inserted {len(rows):,} rows (Total: {total_out:,}, Rate: {rate:,} rows/sec)",
                target_id,
            )

            await _update_job_metrics(job_id, {
                "stage": "LOADING_TARGET" if target_id else "FETCHING",
                "total_rows": total_rows or (total_in if len(raw_chunk) < SOURCE_STREAM_CHUNK_SIZE else None),
                "extracted": total_in,
                "processed": total_in,
                "loaded": total_out,
                "rejected": len(rejected_total),
                "current_chunk": chunk_num,
                "chunks_processed": chunk_num,
                "current_chunk_rows": len(raw_chunk),
            })

            if len(raw_chunk) < SOURCE_STREAM_CHUNK_SIZE:
                break
    except Exception as exc:
        await _log(job_id, "ERROR", f"[TARGET] {target_label} insert failed for '{dataset_name}': {target_connector._scrub(str(exc))}", target_id)
        raise NodeExecutionError(target_connector._scrub(str(exc))) from exc
    finally:
        await asyncio.to_thread(stream.close)

    if rejected_total:
        await reject_service.record_rejects(
            organization_id=organization_id, source_type=RejectSourceType.PIPELINE_JOB, source_id=job_id,
            node_id=target_id, target_object=dataset_name, rejects=rejected_total,
        )
        await _log(
            job_id, "ERROR",
            f"{len(rejected_total)} row(s) rejected writing to '{dataset_name}' — see Rejected Records for each row and its real error.",
            target_id,
        )
        raise NodeExecutionError(f"Target load failed: {len(rejected_total)} row(s) were rejected writing to '{dataset_name}'.")

    duration_sec = (datetime.now(timezone.utc) - source_t0).total_seconds()
    hrs = int(duration_sec // 3600)
    mins = int((duration_sec % 3600) // 60)
    secs = int(duration_sec % 60)
    dur_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"

    await _log(job_id, "INFO", f"Streaming ETL complete in {duration_sec:.1f}s: {total_in:,} processed, {len(rejected_total):,} rejected", target_id)

    duration_ms = int(duration_sec * 1000)
    await _update_node_stats(job_id, source_id, {
        "label": source_node.get("label", "source"), "type": "source", "status": "success",
        "rows_in": 0, "rows_out": total_in, "duration_ms": duration_ms,
    })
    for nid in transform_ids:
        node = nodes[nid]
        await _update_node_stats(job_id, nid, {
            "label": node.get("label", node["type"]), "type": node["type"], "status": "success",
            "rows_in": transform_rows_in[nid], "rows_out": transform_rows_out[nid], "duration_ms": 0,
        })
    target_stats = {
        "label": target_node.get("label", "target"), "type": "target", "status": "success",
        "rows_in": total_in, "rows_out": total_out, "duration_ms": duration_ms,
    }
    if rejected_total:
        target_stats["rows_rejected"] = len(rejected_total)
    await _update_node_stats(job_id, target_id, target_stats)

    await _update_job_metrics(job_id, {
        "stage": "COMPLETED",
        "total_rows": total_rows or total_in,
        "extracted": total_in,
        "processed": total_in,
        "loaded": total_out,
        "rejected": len(rejected_total),
        "current_chunk": chunk_num,
        "chunks_processed": chunk_num,
        "total_chunks": chunk_num,
        "current_chunk_rows": 0,
    })

    await _log(job_id, "INFO", "=" * 60)
    await _log(job_id, "INFO", f"ETL Job Completed: Job #{job_id}")
    await _log(job_id, "INFO", f"Final Status: completed")
    await _log(job_id, "INFO", f"Rows Processed: {total_in:,}")
    await _log(job_id, "INFO", f"Rows Rejected: {len(rejected_total):,}")
    await _log(job_id, "INFO", f"Duration: {dur_str}")
    await _log(job_id, "INFO", "=" * 60)

    return total_in, total_out, cancelled


async def _execute(job_id: int, organization_id: int | None) -> None:
    try:
        sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
        async with sessionmaker() as db:
            job = await db.get(PipelineJob, job_id)
            if job is None:
                return
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

        async with sessionmaker() as db_user:
            user_name_map = await pipeline_job_service.user_name_map(db_user, {job.created_by}) if job.created_by else {}
        user_name = user_name_map.get(job.created_by, "System / Automated")
        start_str = job.started_at.strftime("%Y-%m-%d %H:%M:%S") if job.started_at else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        await _log(job_id, "INFO", "=" * 60)
        await _log(job_id, "INFO", f"ETL Job Started: Job #{job.id} — {job.pipeline_name}")
        await _log(job_id, "INFO", f"Operation: {job.trigger or 'db-to-db'}")
        await _log(job_id, "INFO", f"User: {user_name}")
        await _log(job_id, "INFO", f"Start Time: {start_str}")
        await _log(job_id, "INFO", "=" * 60)
    except Exception as exc:
        await _fail(job_id, f"Could not initialize job execution: {exc}"[:300])
        return

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
    column_metadata: dict[str, list[ColumnInfo]] = {}
    connector_cache: dict[int, tuple] = {}
    primary_connection_id: int | None = None
    total_in: int | None = None
    total_out: int | None = None

    # Streaming fast path (see the module note right above _execute_
    # streaming_chain) — only takes over when the ENTIRE pipeline is one
    # linear source -> [streamable transforms] -> dataset-target chain;
    # everything else falls through to the normal node-by-node loop below
    # completely unchanged.
    streaming_chain = _find_streaming_chain(nodes, edges, order)
    if streaming_chain:
        try:
            streaming_result = await _execute_streaming_chain(
                job_id, streaming_chain, nodes, organization_id, connector_cache, job.pipeline_id,
            )
        except NodeExecutionError as exc:
            await _fail(job_id, f"Pipeline: {exc}"[:300])
            return
        if streaming_result is not None:
            total_in, total_out, cancelled = streaming_result
            if cancelled:
                await _finish(job_id, JobStatus.CANCELLED)
                return
            sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
            async with sessionmaker() as db:
                job = await db.get(PipelineJob, job_id)
                job.records_in = total_in
                await db.commit()
            await _finish(job_id, JobStatus.SUCCEEDED, records_out=total_out)
            return
        # else: source connector doesn't actually support streaming (rare —
        # discovered only by calling it) — no side effects happened yet,
        # fall through to the normal engine below.

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
                elif ntype == "anomaly_detection_step":
                    df, status = await _run_anomaly_detection(job_id, node_id, input_df, cfg, organization_id, primary_connection_id, connector_cache)
                elif ntype == "column_mapping_step":
                    df, status, rejected = await _run_column_mapping(job_id, node_id, input_df, cfg, organization_id, connector_cache)
                elif ntype == "target":
                    source_columns = column_metadata.get(src_ids[0]) if src_ids else None
                    df, output_file, rejected = await _run_target(
                        job_id, node_id, input_df, cfg, organization_id, primary_connection_id,
                        connector_cache, job.pipeline_id, job_id, source_columns=source_columns,
                    )
                elif ntype == "notification":
                    df = input_df
                    await _run_notification(job_id, node_id, cfg)
                elif ntype == "invoice_intake":
                    df = await _run_invoice_intake(job_id, node_id, cfg, organization_id)
                elif ntype == "invoice_processing":
                    df, status = await _run_invoice_processing(job_id, node_id, input_df, organization_id)
                elif ntype == "invoice_validation":
                    df, status = await _run_invoice_validation(job_id, node_id, input_df, organization_id)
                elif ntype == "invoice_review_gate":
                    df = _run_invoice_review_gate(input_df, cfg)
                elif ntype == "invoice_erp_posting":
                    df, status = await _run_invoice_erp_posting(job_id, node_id, input_df, cfg, organization_id)
                elif ntype == "invoice_reconciliation":
                    df, status = await _run_invoice_reconciliation(job_id, node_id, input_df, organization_id)
                else:
                    df = input_df if input_df is not None else pd.DataFrame()
                    await _log(job_id, "WARN", f"Unknown node type '{ntype}' — skipped.", node_id)
                    status = "skipped"
            except NodeExecutionError as exc:
                await _update_node_stats(job_id, node_id, {"label": label, "type": ntype, "status": "error", "message": str(exc)})
                await _fail(job_id, f"Node '{label}': {exc}")
                return

            dataframes[node_id] = df
            if ntype == "source" and df.attrs.get("source_columns"):
                source_meta = _metadata_columns_for_dataframe(df.attrs.get("source_columns"), df)
                if source_meta:
                    column_metadata[node_id] = source_meta
            elif src_ids and column_metadata.get(src_ids[0]) is not None:
                source_meta = _metadata_columns_for_dataframe(column_metadata[src_ids[0]], df)
                if source_meta:
                    column_metadata[node_id] = source_meta
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
            if ntype == "column_mapping_step" and status != "skipped":
                # CM9 — a human-readable one-liner surfacing exactly which
                # (mapping, version) this run actually resolved and applied,
                # so Monitoring's Job Detail view doesn't require opening the
                # job log to answer "which mapping ran here". mapping_id/
                # mapping_version/df/rejected are already in scope from the
                # dispatch branch above — no extra plumbing needed.
                mapped_cols = len(df.columns) if df is not None else 0
                stats_patch["message"] = (
                    f"Column Mapping #{cfg.get('mappingId')} v{cfg.get('mappingVersion')} — {mapped_cols} column(s) mapped"
                    + (f", {len(rejected)} row(s) rejected" if rejected else "")
                )
            await _update_node_stats(job_id, node_id, stats_patch)
            await _update_job_metrics(job_id, {
                "stage": "FETCHING" if ntype == "source" else ("LOADING_TARGET" if ntype == "target" else "TRANSFORMING"),
                "extracted": rows_out if ntype == "source" else (total_in or 0),
                "processed": rows_out if ntype not in ("source", "target") else (rows_in or 0),
                "loaded": rows_out if ntype == "target" else 0,
                "rejected": len(rejected) if rejected else 0,
            })
            await _log(job_id, "INFO", f"'{label}' finished — {rows_in} in / {rows_out} out ({duration_ms} ms).", node_id)
            if total_in is None:
                total_in = rows_in
            total_out = rows_out

        sessionmaker = await tenant_engine_registry.get_background_sessionmaker(organization_id)
        async with sessionmaker() as db:
            job = await db.get(PipelineJob, job_id)
            job.records_in = total_in
            await db.commit()
        await _log(job_id, "INFO", "Job finished successfully.")
        await _finish(job_id, JobStatus.SUCCEEDED, records_out=total_out)
    except Exception as exc:  # last-resort safety net — never leave a job stuck RUNNING
        await _fail(job_id, f"Unexpected error: {exc}"[:300])
    finally:
        _cancelled.discard(job_id)
        _job_org_cache.pop(job_id, None)
