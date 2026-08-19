/**
 * RunExtractionPanel — triggers and monitors a REAL backend extraction run
 * for a Source Dataset node (see backend/app/execution/pipeline_executor.py).
 * This is separate from the Pipeline Designer's whole-pipeline "Run
 * Pipeline" button, which stays a client-side design-time simulation —
 * transform/validate/target execution aren't real yet, so mixing a real
 * extraction with a fake downstream animation in one "Run" would be
 * misleading. This button only ever does the one real thing this build
 * can actually do: extract this node's source data to a staging file.
 */
import { useEffect, useRef, useState } from 'react';
import { Play, Square, RotateCcw, Loader2, CheckCircle2, XCircle, AlertTriangle } from 'lucide-react';
import { executionApi } from '@/services/api/executionApi.js';
import './pipeline.css';

const POLL_MS = 700;
const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);

const STATUS_META = {
  PENDING: { icon: Loader2, spin: true, label: 'Queued…', cls: 'running' },
  RUNNING: { icon: Loader2, spin: true, label: 'Running…', cls: 'running' },
  SUCCEEDED: { icon: CheckCircle2, spin: false, label: 'Succeeded', cls: 'success' },
  FAILED: { icon: XCircle, spin: false, label: 'Failed', cls: 'failure' },
  CANCELLED: { icon: AlertTriangle, spin: false, label: 'Cancelled', cls: 'unsupported' },
};

function apiErrorMessage(err, fallback) {
  return err?.message || fallback;
}

export function RunExtractionPanel({ pipelineId, nodeId, pipelineDirty, objectType, ready, columns }) {
  const [keyColumn, setKeyColumn] = useState('');
  const [workerCount, setWorkerCount] = useState(4);
  const [partitionCount, setPartitionCount] = useState(4);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const [runId, setRunId] = useState(null);
  const [run, setRun] = useState(null);
  const [partitions, setPartitions] = useState([]);
  const [metrics, setMetrics] = useState(null);
  const [starting, setStarting] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState('');
  const pollRef = useRef(null);
  const pollFailures = useRef(0);

  const isTableOrView = objectType === 'TABLE' || objectType === 'VIEW';
  const isFile = objectType === 'FILE';

  // Default the key column to the first primary key found once columns load.
  useEffect(() => {
    if (isTableOrView && columns && !keyColumn) {
      const pk = columns.find((c) => c.primary_key);
      if (pk) setKeyColumn(pk.name);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [columns]);

  // A different node was selected — drop any stale run state from before.
  useEffect(() => {
    stopPolling();
    setRunId(null); setRun(null); setPartitions([]); setMetrics(null); setError('');
    setKeyColumn('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId]);

  useEffect(() => () => stopPolling(), []);

  function stopPolling() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }

  async function pollOnce(id) {
    try {
      const [status, m] = await Promise.all([executionApi.getStatus(id), executionApi.getMetrics(id)]);
      pollFailures.current = 0;
      setRun(status.run);
      setPartitions(status.partitions);
      setMetrics(m);
      if (TERMINAL.has(status.run.status)) stopPolling();
    } catch (err) {
      // A single transient poll failure shouldn't freeze the UI on stale
      // data forever — only give up (and say so) after a few in a row.
      pollFailures.current += 1;
      if (pollFailures.current >= 3) {
        stopPolling();
        setError(apiErrorMessage(err, 'Lost connection while monitoring this run — its actual status may have changed on the server.'));
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
      const body = {
        node_id: nodeId,
        key_column: isTableOrView ? keyColumn : undefined,
        chunk_size: undefined, // defaults to the node's Batch Size server-side
        // FILE mode has one partition per matched file (planned server-side,
        // not user-chosen) but still runs up to worker_count of them
        // concurrently, so Workers still applies; Partitions doesn't.
        worker_count: (isTableOrView || isFile) ? workerCount : undefined,
        partition_count: isTableOrView ? partitionCount : undefined,
      };
      const created = await executionApi.startRun(pipelineId, body);
      setRunId(created.id);
      startPolling(created.id);
    } catch (err) {
      setError(apiErrorMessage(err, 'Could not start extraction.'));
    } finally {
      setStarting(false);
    }
  };

  const handleCancel = async () => {
    if (!runId) return;
    setActionBusy(true);
    try { await executionApi.cancel(runId); await pollOnce(runId); }
    catch (err) { setError(apiErrorMessage(err, 'Could not cancel run.')); }
    finally { setActionBusy(false); }
  };

  const handleResume = async () => {
    if (!runId) return;
    setActionBusy(true); setError('');
    try { await executionApi.resume(runId); startPolling(runId); }
    catch (err) { setError(apiErrorMessage(err, 'Could not resume run.')); }
    finally { setActionBusy(false); }
  };

  if (!ready) return null;

  const canStart = pipelineId && !pipelineDirty && (!isTableOrView || keyColumn) && !starting && !(run && !TERMINAL.has(run.status));
  const meta = run ? STATUS_META[run.status] : null;
  const StatusIcon = meta?.icon;

  return (
    <div className="pc-config-row sdc-run-section">
      <label className="pc-config-label">Run Extraction</label>

      {!pipelineId && (
        <span className="sdc-inline-warning"><AlertTriangle size={11} /> Save the pipeline before running a real extraction.</span>
      )}
      {pipelineId && pipelineDirty && (
        <span className="sdc-inline-warning"><AlertTriangle size={11} /> You have unsaved changes — Save first so the run reads the current config.</span>
      )}

      {isTableOrView && (
        <>
          <div className="sdc-run-field-row">
            <div className="sdc-run-field">
              <span className="sdc-run-field-label">Key Column *</span>
              <select className="pc-config-select" value={keyColumn} onChange={(e) => setKeyColumn(e.target.value)} disabled={!columns}>
                <option value="">{columns ? 'Select…' : 'Browse Columns first'}</option>
                {(columns || []).map((c) => <option key={c.name} value={c.name}>{c.name}{c.primary_key ? ' (PK)' : ''}</option>)}
              </select>
            </div>
          </div>
          <button type="button" className="sdc-advanced-toggle" onClick={() => setShowAdvanced((s) => !s)}>
            {showAdvanced ? 'Hide' : 'Show'} worker/partition settings
          </button>
          {showAdvanced && (
            <div className="sdc-run-field-row">
              <div className="sdc-run-field">
                <span className="sdc-run-field-label">Workers</span>
                <input type="number" className="pc-config-input" min={1} max={16} value={workerCount} onChange={(e) => setWorkerCount(Number(e.target.value))} />
              </div>
              <div className="sdc-run-field">
                <span className="sdc-run-field-label">Partitions</span>
                <input type="number" className="pc-config-input" min={1} max={64} value={partitionCount} onChange={(e) => setPartitionCount(Number(e.target.value))} />
              </div>
            </div>
          )}
        </>
      )}

      {isFile && (
        <div className="sdc-run-field-row">
          <div className="sdc-run-field">
            <span className="sdc-run-field-label">Concurrent Files (Workers)</span>
            <input type="number" className="pc-config-input" min={1} max={16} value={workerCount} onChange={(e) => setWorkerCount(Number(e.target.value))} />
          </div>
        </div>
      )}

      <div className="sdc-run-actions">
        <button type="button" className="sdc-secondary-btn" onClick={handleStart} disabled={!canStart}>
          {starting ? <Loader2 size={12} className="sdc-spin" /> : <Play size={12} />}
          {run && !TERMINAL.has(run.status) ? 'Running…' : 'Run Extraction'}
        </button>
        {run && run.status === 'RUNNING' && (
          <button type="button" className="sdc-secondary-btn sdc-danger" onClick={handleCancel} disabled={actionBusy}>
            <Square size={12} /> Cancel
          </button>
        )}
        {run && (run.status === 'FAILED' || run.status === 'CANCELLED') && (
          <button type="button" className="sdc-secondary-btn" onClick={handleResume} disabled={actionBusy}>
            <RotateCcw size={12} /> Resume
          </button>
        )}
      </div>

      {error && <span className="sdc-error-text">{error}</span>}

      {run && (
        <div className={`sdc-run-progress ${meta.cls}`}>
          <div className="sdc-run-progress-header">
            {StatusIcon && <StatusIcon size={13} className={meta.spin ? 'sdc-spin' : ''} />}
            <span>{meta.label}</span>
            <span className="sdc-run-progress-count">
              {run.records_read.toLocaleString()} rows
            </span>
          </div>

          {run.status === 'RUNNING' && partitions.length > 0 && (
            <div className="sdc-partition-bars">
              {partitions.map((p) => (
                <div key={p.partition_index} className={`sdc-partition-bar-track status-${p.status.toLowerCase()}`} title={`Partition ${p.partition_index}: ${p.status} — ${p.records_read.toLocaleString()} rows`} />
              ))}
            </div>
          )}

          {metrics && (metrics.throughput_rps || metrics.elapsed_seconds) && (
            <div className="sdc-run-metrics">
              {metrics.throughput_rps != null && <span>{metrics.throughput_rps.toLocaleString()} rows/sec</span>}
              {metrics.elapsed_seconds != null && <span>{metrics.elapsed_seconds.toFixed(1)}s elapsed</span>}
              {metrics.eta_seconds != null && <span>~{metrics.eta_seconds.toFixed(1)}s remaining</span>}
              <span>{metrics.partitions_complete}/{metrics.partitions_total} partitions</span>
            </div>
          )}

          {/* FILE mode: one row per matched file — real per-file outcome
              (checksum, whether the archive/error move actually
              happened), not just an aggregate row count. */}
          {isFile && TERMINAL.has(run.status) && partitions.length > 0 && (
            <div className="sdc-columns-table-wrap">
              <table className="sdc-columns-table">
                <thead><tr><th>File</th><th>Status</th><th>Rows</th><th>Moved</th></tr></thead>
                <tbody>
                  {partitions.map((p) => (
                    <tr key={p.partition_index}>
                      <td className="sdc-col-name" title={p.file_checksum ? `sha256:${p.file_checksum}` : ''}>{p.file_name}</td>
                      <td>{p.status}</td>
                      <td>{p.records_read.toLocaleString()}</td>
                      <td>{p.archived ? 'Archived' : (p.status === 'FAILED' ? 'To error dir (if configured)' : '—')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {run.error_message && <div className="sdc-error-text">{run.error_message}</div>}
        </div>
      )}
    </div>
  );
}
