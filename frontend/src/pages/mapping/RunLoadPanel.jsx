/**
 * RunLoadPanel — triggers and monitors a REAL backend target load for a
 * saved ColumnMapping (see backend/app/execution/load_executor.py). This
 * is the write-side counterpart to pipeline/RunExtractionPanel.jsx: it
 * streams the mapping's real transform SQL from the source and bulk-
 * writes each chunk to the target via a real vendor writer (Postgres/
 * MySQL/SQL Server) — nothing here is simulated.
 *
 * Only renders once the mapping is saved (mappingId set) — a load run
 * needs a persisted ColumnMapping row to reference, same reason
 * RunExtractionPanel needs a saved pipeline.
 */
import { useEffect, useRef, useState } from 'react';
import { Play, Square, Loader2, CheckCircle2, XCircle, AlertTriangle, UploadCloud } from 'lucide-react';
import { loadApi } from '@/services/api/loadApi.js';
import { RejectedRecordsModal } from '@/components/common/index.js';

const POLL_MS = 700;
const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);

const STATUS_META = {
  PENDING: { icon: Loader2, spin: true, label: 'Queued…', cls: 'running' },
  RUNNING: { icon: Loader2, spin: true, label: 'Running…', cls: 'running' },
  SUCCEEDED: { icon: CheckCircle2, spin: false, label: 'Succeeded', cls: 'success' },
  FAILED: { icon: XCircle, spin: false, label: 'Failed', cls: 'failure' },
  CANCELLED: { icon: AlertTriangle, spin: false, label: 'Cancelled', cls: 'running' },
};

const WRITE_MODES = [
  { value: 'APPEND', label: 'Append' },
  { value: 'TRUNCATE_INSERT', label: 'Truncate & Insert' },
  { value: 'UPSERT', label: 'Upsert' },
];

function apiErrorMessage(err, fallback) {
  return err?.message || fallback;
}

export function RunLoadPanel({ mappingId, mappingDirty, targetColumns, mappedTargetNames }) {
  const [writeMode, setWriteMode] = useState('APPEND');
  const [chunkSize, setChunkSize] = useState('');

  const [runId, setRunId] = useState(null);
  const [run, setRun] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [starting, setStarting] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState('');
  const [rejectsOpen, setRejectsOpen] = useState(false);
  const pollRef = useRef(null);
  const pollFailures = useRef(0);

  useEffect(() => {
    stopPolling();
    setRunId(null); setRun(null); setMetrics(null); setError('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mappingId]);

  useEffect(() => () => stopPolling(), []);

  function stopPolling() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }

  async function pollOnce(id) {
    try {
      const [status, m] = await Promise.all([loadApi.getStatus(id), loadApi.getMetrics(id)]);
      pollFailures.current = 0;
      setRun(status);
      setMetrics(m);
      if (TERMINAL.has(status.status)) stopPolling();
    } catch (err) {
      pollFailures.current += 1;
      if (pollFailures.current >= 3) {
        stopPolling();
        setError(apiErrorMessage(err, 'Lost connection while monitoring this load — its actual status may have changed on the server.'));
      }
    }
  }

  function startPolling(id) {
    stopPolling();
    pollFailures.current = 0;
    pollOnce(id);
    pollRef.current = setInterval(() => pollOnce(id), POLL_MS);
  }

  const handleStart = async () => {
    setStarting(true); setError('');
    try {
      const body = { write_mode: writeMode, chunk_size: chunkSize ? Number(chunkSize) : undefined };
      const created = await loadApi.startLoad(mappingId, body);
      setRunId(created.id);
      startPolling(created.id);
    } catch (err) {
      setError(apiErrorMessage(err, 'Could not start load.'));
    } finally {
      setStarting(false);
    }
  };

  const handleCancel = async () => {
    if (!runId) return;
    setActionBusy(true);
    try { await loadApi.cancel(runId); await pollOnce(runId); }
    catch (err) { setError(apiErrorMessage(err, 'Could not cancel load.')); }
    finally { setActionBusy(false); }
  };

  if (!mappingId) return null;

  const targetPkMapped = (targetColumns || []).some((c) => c.primary_key && mappedTargetNames?.has(c.name));
  const canStart = !mappingDirty && !starting && !(run && !TERMINAL.has(run.status));
  const meta = run ? STATUS_META[run.status] : null;
  const StatusIcon = meta?.icon;

  return (
    <div className="ocm-load-section">
      <div className="ocm-load-header"><UploadCloud size={14} /> Run Load</div>

      {mappingDirty && (
        <span className="ocm-inline-warning"><AlertTriangle size={11} /> You have unsaved mapping changes — Save first so the load reads the current definition.</span>
      )}

      <div className="ocm-load-controls">
        <select className="ocm-select" value={writeMode} onChange={(e) => setWriteMode(e.target.value)}>
          {WRITE_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
        </select>
        <input
          className="ocm-input" style={{ width: 130 }} type="number" min={1}
          placeholder="Chunk size (default 5000)" value={chunkSize} onChange={(e) => setChunkSize(e.target.value)}
        />
        <button type="button" className="ocm-view-toggle" disabled={!canStart} onClick={handleStart}>
          {starting ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
          {' '}{run && !TERMINAL.has(run.status) ? 'Running…' : 'Run Load'}
        </button>
        {run && run.status === 'RUNNING' && (
          <button type="button" className="ocm-view-toggle" disabled={actionBusy} onClick={handleCancel}>
            <Square size={12} /> Cancel
          </button>
        )}
      </div>

      {writeMode === 'UPSERT' && !targetPkMapped && (
        <span className="ocm-inline-warning">
          <AlertTriangle size={11} /> The target's primary key isn't part of this mapping's columns yet — Upsert will be refused (with a clear error) until it is.
        </span>
      )}

      {error && <span className="ocm-error-text">{error}</span>}

      {run && (
        <div className={`ocm-load-progress ${meta.cls}`}>
          <div className="ocm-load-progress-header">
            {StatusIcon && <StatusIcon size={13} className={meta.spin ? 'animate-spin' : ''} />}
            <span>{meta.label}</span>
            <span className="ocm-load-progress-count">{run.records_written.toLocaleString()} / {run.records_read.toLocaleString()} rows written</span>
          </div>
          {run.key_columns && <div className="ocm-load-key-note">Upsert key column(s) (auto-detected from the target's primary key): {run.key_columns.join(', ')}</div>}
          {metrics && (metrics.throughput_rps || metrics.elapsed_seconds) && (
            <div className="ocm-load-metrics">
              {metrics.throughput_rps != null && <span>{metrics.throughput_rps.toLocaleString()} rows/sec</span>}
              {metrics.elapsed_seconds != null && <span>{metrics.elapsed_seconds.toFixed(1)}s elapsed</span>}
            </div>
          )}
          {run.records_rejected > 0 && (
            <button type="button" className="ocm-inline-warning" style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }} onClick={() => setRejectsOpen(true)}>
              <AlertTriangle size={11} /> {run.records_rejected.toLocaleString()} row(s) rejected — click to view
            </button>
          )}
          {run.error_message && <div className="ocm-error-text">{run.error_message}</div>}
        </div>
      )}

      <RejectedRecordsModal
        open={rejectsOpen}
        onClose={() => setRejectsOpen(false)}
        title="Rejected records"
        exportFilename={`load_${runId}_rejected.csv`}
        fetchPage={(page, pageSize) => loadApi.getRejectedRecords(runId, { page, page_size: pageSize })}
      />
    </div>
  );
}
