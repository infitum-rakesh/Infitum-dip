/**
 * ExportsPanel — SQL Workspace sidebar tab. Background export of a
 * query's FULL result set — deliberately separate from Export CSV/JSON/
 * Excel on the Result Grid, which only ever covers whatever's already in
 * the browser (bounded by the query's row limit, max 1000 — see
 * ResultGrid.jsx's doExport). This streams the query server-side
 * (backend/app/execution/sql_export_executor.py) straight to a real
 * file with no row cap, tracked as a job this panel polls while it's
 * still running — a 5-million-row export really does take a while and
 * really does write 5 million rows, not a fabricated instant success.
 */
import { useEffect, useRef, useState } from 'react';
import { Download, Loader2, CheckCircle2, XCircle, X, RotateCcw, AlertTriangle } from 'lucide-react';
import { EmptyState } from '@/components/common/index.js';
import { sqlWorkspaceApi } from '@/services/api/sqlWorkspaceApi.js';

const FORMATS = ['CSV', 'JSON', 'XLSX', 'PARQUET'];
const POLL_MS = 2000;
const ACTIVE_STATUSES = new Set(['PENDING', 'RUNNING']);

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  } catch { return iso; }
}

function fmtBytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function safeName(name) {
  return (name || 'export').trim().replace(/[^a-z0-9_-]+/gi, '_') || 'export';
}

export default function ExportsPanel({ connectionId, sqlText, queryName, autoExportFormat, onAutoExportHandled }) {
  const [format, setFormat] = useState('CSV');
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState('');
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const pollTimer = useRef(null);
  const autoExportRef = useRef(null); // guards the effect below against StrictMode's dev-only double-invoke, which would otherwise fire two real export jobs for one click

  const load = () => {
    sqlWorkspaceApi.listExports({ page_size: 20 })
      .then((r) => setItems(r.items ?? []))
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, []);

  // Real polling while at least one job is still PENDING/RUNNING — stops
  // on its own once every job this panel knows about has reached a
  // terminal status, rather than polling forever in the background.
  useEffect(() => {
    const active = items.some((j) => ACTIVE_STATUSES.has(j.status));
    if (!active) return undefined;
    pollTimer.current = setTimeout(load, POLL_MS);
    return () => clearTimeout(pollTimer.current);
  }, [items]);

  const startExport = async (fmtOverride) => {
    const fmt = fmtOverride || format;
    if (!connectionId || !sqlText?.trim()) return;
    setStarting(true);
    setStartError('');
    try {
      const job = await sqlWorkspaceApi.createExport(connectionId, { sql: sqlText, format: fmt });
      setItems((prev) => [job, ...prev]);
    } catch (err) {
      setStartError(err?.message || 'Could not start the export.');
    } finally {
      setStarting(false);
    }
  };

  // Triggered by ResultGrid's "Export Parquet (background)" menu item —
  // Parquet has no honest client-side generator, so that button switches
  // the sidebar here and hands us the format to auto-start with. Guarded
  // by autoExportRef (not just the `if (!autoExportFormat)` check) because
  // React.StrictMode double-invokes effects in dev — without the ref, one
  // click would fire two real POST /export calls before the parent's
  // onAutoExportHandled() state update has a chance to flush.
  useEffect(() => {
    if (!autoExportFormat) {
      autoExportRef.current = null;
      return;
    }
    if (autoExportRef.current === autoExportFormat) return;
    autoExportRef.current = autoExportFormat;
    setFormat(autoExportFormat);
    startExport(autoExportFormat);
    onAutoExportHandled?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoExportFormat]);

  const cancelJob = async (id) => {
    try {
      await sqlWorkspaceApi.cancelExport(id);
    } finally {
      load();
    }
  };

  const downloadJob = (job) => {
    sqlWorkspaceApi.downloadExport(job.id, `${safeName(queryName)}.${job.format.toLowerCase()}`);
  };

  return (
    <div className="sqlw-export-panel">
      <div className="sqlw-export-start">
        <select className="sqlw-select sqlw-select-narrow" value={format} onChange={(e) => setFormat(e.target.value)}>
          {FORMATS.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
        <button className="sqlw-export-start-btn" onClick={() => startExport()} disabled={!connectionId || !sqlText?.trim() || starting}>
          {starting ? <Loader2 size={13} className="sqlw-spin" /> : <Download size={13} />}
          {starting ? 'Starting…' : 'Export Full Result'}
        </button>
        <button className="sqlw-history-refresh" onClick={load} title="Refresh"><RotateCcw size={13} /></button>
      </div>
      {(!connectionId || !sqlText?.trim()) && (
        <div className="sqlw-tree-empty small">Write a SELECT and pick a connection to enable background export.</div>
      )}
      {startError && <div className="sqlw-banner error small"><AlertTriangle size={13} /> {startError}</div>}

      {loading && <div className="sqlw-tree-empty"><Loader2 size={14} className="sqlw-spin" /> Loading…</div>}
      {!loading && items.length === 0 && (
        <EmptyState
          icon={<Download size={28} />}
          title="No exports yet"
          description="Export a query's full result — no row cap — to a real CSV/JSON/Excel file."
        />
      )}

      {!loading && items.map((job) => (
        <div key={job.id} className="sqlw-export-row">
          <div className="sqlw-export-row-top">
            {job.status === 'SUCCEEDED' && <CheckCircle2 size={13} className="sqlw-history-ok" />}
            {job.status === 'FAILED' && <XCircle size={13} className="sqlw-history-err" />}
            {job.status === 'CANCELLED' && <X size={13} className="sqlw-history-err" />}
            {ACTIVE_STATUSES.has(job.status) && <Loader2 size={13} className="sqlw-spin" />}
            <span className="sqlw-export-format">{job.format}</span>
            <span className="sqlw-history-time">{fmtTime(job.created_at)}</span>
          </div>
          <div className="sqlw-history-meta">
            <span>
              {job.row_count} row{job.row_count === 1 ? '' : 's'}
              {job.file_size_bytes ? ` · ${fmtBytes(job.file_size_bytes)}` : ''}
              {ACTIVE_STATUSES.has(job.status) ? ' · running…' : ''}
            </span>
            {job.status === 'FAILED' && <span className="sqlw-history-errtext">{job.error_message}</span>}
          </div>
          <div className="sqlw-export-actions">
            {job.status === 'SUCCEEDED' && (
              <button className="sqlw-export-action" onClick={() => downloadJob(job)}><Download size={12} /> Download</button>
            )}
            {ACTIVE_STATUSES.has(job.status) && (
              <button className="sqlw-export-action danger" onClick={() => cancelJob(job.id)}><X size={12} /> Cancel</button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
