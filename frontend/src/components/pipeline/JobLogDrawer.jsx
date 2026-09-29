/**
 * JobLogDrawer — live view of one PipelineJob: status, per-node stats and
 * a real, live-tailed log (polls GET /pipeline-jobs/{id} and
 * .../logs?since_id= every 1.2s while the job is PENDING/RUNNING, stops
 * automatically once it reaches a terminal state). Used from Pipeline
 * Builder's Run action and from the global Pipeline Runs page.
 */
import { useEffect, useRef, useState } from 'react';
import { X, Square, Download, AlertTriangle } from 'lucide-react';
import { Button, StatusBadge, RejectedRecordsModal } from '@/components/common/index.js';
import { pipelineJobApi } from '@/services/api/pipelineApi.js';
import './pipeline.css';

const STATUS_VARIANT = { PENDING: 'gray', RUNNING: 'info', SUCCEEDED: 'success', FAILED: 'danger', CANCELLED: 'gray' };
const LEVEL_CLASS = { INFO: 'jld-log-info', WARN: 'jld-log-warn', ERROR: 'jld-log-error' };
const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);

function formatElapsed(seconds) {
  if (seconds == null || isNaN(seconds) || seconds < 0) return '00:00:00';
  const s = Math.floor(seconds);
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (hrs > 0) {
    return `${String(hrs).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

function formatEta(seconds) {
  if (seconds == null || isNaN(seconds) || seconds <= 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)} sec`;
  return formatElapsed(seconds);
}

function formatNumber(num) {
  if (num == null || isNaN(num)) return '0';
  return Number(num).toLocaleString();
}

function emptyLogMessage(job) {
  // logs.length === 0 doesn't always mean "hasn't started yet" — it's also
  // true for the brief window before the first poll tick resolves (any
  // status), and can be genuinely true forever for a job that failed
  // before its first _log() call ran. "Waiting for the job to start…" was
  // shown unconditionally here regardless of job.status, so it kept
  // reading as stuck/broken on an already-completed or actively-running
  // job that simply hadn't rendered its log lines yet on this tick.
  if (!job) return 'Loading job details…';
  if (job.status === 'PENDING') return 'Waiting for the job to start…';
  if (job.status === 'RUNNING') return 'Job is running — waiting for the first log entries…';
  if (TERMINAL.has(job.status)) return 'This job finished with no log entries recorded.';
  return 'Loading log entries…';
}

function formatStageLabel(stage) {
  if (!stage) return 'Processing...';
  const map = {
    PREPARING: 'Preparing pipeline execution...',
    PREPARING_BULK_QUERY: 'Preparing Salesforce Bulk API query...',
    DOWNLOADING_BULK_RESULTS: 'Fetching Salesforce Bulk API results...',
    FETCHING: 'Extracting data from source...',
    TRANSFORMING: 'Applying transformations...',
    PREPARING_TARGET: 'Preparing target table...',
    LOADING_TARGET: 'Loading data into target database...',
    COMPLETED: 'Pipeline execution completed',
    FAILED: 'Pipeline execution failed',
    ABORTED: 'Pipeline execution aborted',
    CANCELLED: 'Pipeline execution cancelled',
  };
  return map[stage] || stage.replace(/_/g, ' ');
}

function progressSubject(stage) {
  if (stage === 'PREPARING_TARGET') return 'extracted';
  if (stage === 'LOADING_TARGET') return 'loaded';
  if (stage === 'FETCHING' || stage === 'DOWNLOADING_BULK_RESULTS') return 'extracted';
  return 'processed';
}

export function JobLogDrawer({ jobId, onClose, onStatusChange }) {
  const [job, setJob] = useState(null);
  const [logs, setLogs] = useState([]);
  const [cancelling, setCancelling] = useState(false);
  const lastLogId = useRef(0);
  const logEndRef = useRef(null);
  const pollRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    lastLogId.current = 0;
    setJob(null);
    setLogs([]);

    const tick = async () => {
      try {
        const [jobData, logData] = await Promise.all([
          pipelineJobApi.get(jobId),
          pipelineJobApi.getLogs(jobId, lastLogId.current || undefined),
        ]);
        if (cancelled) return;
        setJob(jobData);
        onStatusChange?.(jobData);
        if (logData.items.length) {
          lastLogId.current = logData.items[logData.items.length - 1].id;
          setLogs((prev) => {
            const existingIds = new Set(prev.map((l) => l.id));
            const newItems = logData.items.filter((l) => !existingIds.has(l.id));
            return newItems.length ? [...prev, ...newItems] : prev;
          });
        }
        if (TERMINAL.has(jobData.status) && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch {
        // transient poll failure — try again on the next tick
      }
    };

    tick();
    pollRef.current = setInterval(tick, 1200);
    return () => { cancelled = true; if (pollRef.current) clearInterval(pollRef.current); };
  }, [jobId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ block: 'nearest' });
  }, [logs.length]);

  const handleCancel = async () => {
    setCancelling(true);
    try {
      await pipelineJobApi.cancel(jobId);
    } catch {
      // surfaced via the next poll tick's job.status if it actually didn't cancel
    } finally {
      setCancelling(false);
    }
  };

  const isRunning = job && !TERMINAL.has(job.status);
  const progressLabel = progressSubject(job?.stage);
  const progressValue = progressLabel === 'loaded'
    ? job?.records?.loaded
    : progressLabel === 'extracted'
      ? (job?.records?.extracted ?? job?.progress?.processed)
      : (job?.progress?.processed ?? job?.records?.processed);
  const nodeEntries = job
    ? Object.entries(job.node_stats || {}).filter(([k]) => k !== '__metrics__')
    : [];
  const [downloadingNode, setDownloadingNode] = useState(null);
  const [rejectsNode, setRejectsNode] = useState(null); // {nodeId, label} — which node's rejects modal is open

  const handleDownload = async (nodeId, outputFile) => {
    setDownloadingNode(nodeId);
    try {
      await pipelineJobApi.downloadOutputFile(jobId, nodeId, outputFile.split('/').pop());
    } catch {
      // surfaced implicitly — the browser just won't show a download
    } finally {
      setDownloadingNode(null);
    }
  };

  return (
    <div className="jld-overlay" onClick={onClose}>
      <div className="jld-panel" onClick={(e) => e.stopPropagation()}>
        <div className="jld-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span className="jld-title">Job #{jobId}{job ? ` — ${job.pipeline_name}` : ''}</span>
            {job && <StatusBadge variant={STATUS_VARIANT[job.status] ?? 'gray'} dot>{job.status}</StatusBadge>}
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {isRunning && (
              <Button
                variant="danger"
                size="sm"
                icon={<Square size={12} fill="currentColor" />}
                loading={cancelling}
                disabled={cancelling}
                onClick={handleCancel}
              >
                {cancelling ? 'Aborting...' : 'Abort'}
              </Button>
            )}
            <button className="jld-close" onClick={onClose}><X size={16} /></button>
          </div>
        </div>

        {job?.error_message && (
          <div className="jld-error-banner">{job.error_message}</div>
        )}

        {job && (
          <div className="jld-dashboard">
            <div className="jld-stage-row">
              <div className="jld-stage-info">
                <span className="jld-stage-label">Current Stage</span>
                <span className="jld-stage-val">{formatStageLabel(job.stage)}</span>
              </div>
              <div className="jld-progress-badge">
                {job.progress?.percentage != null
                  ? `${job.progress.percentage.toFixed(1)}%`
                  : (isRunning ? 'Calculating...' : '100%')}
              </div>
            </div>

            <div className="jld-progress-track">
              <div
                className={`jld-progress-fill ${job.progress?.percentage == null && isRunning ? 'indeterminate' : ''}`}
                style={{ width: `${job.progress?.percentage != null ? Math.min(100, Math.max(0, job.progress.percentage)) : 100}%` }}
              />
            </div>

            <div className="jld-progress-sub">
              <span>
                {formatNumber(progressValue)}
                {job.progress?.total != null ? ` / ${formatNumber(job.progress.total)}` : ''} rows
                {` ${progressLabel}`}
              </span>
              {(job.bulk_api?.chunks_processed > 0 || job.bulk_api?.current_chunk > 0) && (
                <span className="jld-chunk-meta">
                  Chunk {job.bulk_api.current_chunk || job.bulk_api.chunks_processed}
                  {job.bulk_api.total_chunks ? ` / ${job.bulk_api.total_chunks}` : ''}
                </span>
              )}
            </div>

            <div className="jld-metrics-grid">
              <div className="jld-metric-box">
                <span className="jld-metric-title">Throughput</span>
                <span className="jld-metric-value">{formatNumber(job.throughput?.rows_per_second ?? 0)} <small>rows/s</small></span>
              </div>
              <div className="jld-metric-box">
                <span className="jld-metric-title">ETA</span>
                <span className="jld-metric-value">{formatEta(job.timing?.eta_seconds)}</span>
              </div>
              <div className="jld-metric-box">
                <span className="jld-metric-title">Elapsed</span>
                <span className="jld-metric-value">{formatElapsed(job.timing?.elapsed_seconds)}</span>
              </div>
              <div className="jld-metric-box">
                <span className="jld-metric-title">Extraction Rate</span>
                <span className="jld-metric-value">{formatNumber(job.throughput?.extraction_rate ?? 0)} <small>rows/s</small></span>
              </div>
            </div>

            <div className="jld-counters-bar">
              <div className="jld-counter-item">
                <span className="jld-counter-lbl">Extracted</span>
                <span className="jld-counter-num">{formatNumber(job.records?.extracted)}</span>
              </div>
              <div className="jld-counter-item">
                <span className="jld-counter-lbl">Processed</span>
                <span className="jld-counter-num">{formatNumber(job.records?.processed)}</span>
              </div>
              <div className="jld-counter-item">
                <span className="jld-counter-lbl">Loaded</span>
                <span className="jld-counter-num success">{formatNumber(job.records?.loaded)}</span>
              </div>
              <div className={`jld-counter-item ${job.records?.rejected > 0 ? 'has-rejected' : ''}`}>
                <span className="jld-counter-lbl">Rejected</span>
                <span className="jld-counter-num danger">{formatNumber(job.records?.rejected)}</span>
              </div>
            </div>
          </div>
        )}

        {nodeEntries.length > 0 && (
          <div className="jld-node-stats">
            {nodeEntries.map(([nodeId, s]) => (
              <div key={nodeId} className={`jld-node-chip jld-node-${s.status}`}>
                <span style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                  <span className="jld-node-chip-label">{s.label ?? nodeId}</span>
                  <span className="jld-node-chip-meta">
                    {s.status === 'error' ? 'failed' : `${s.rows_in ?? 0}→${s.rows_out ?? 0} rows · ${s.duration_ms ?? 0}ms`}
                  </span>
                </span>
                {s.rows_rejected > 0 && (
                  <button
                    className="jld-node-chip-rejected"
                    title={`${s.rows_rejected} row(s) rejected writing to this target — click to view`}
                    onClick={() => setRejectsNode({ nodeId, label: s.label ?? nodeId })}
                  >
                    <AlertTriangle size={11} /> {s.rows_rejected}
                  </button>
                )}
                {s.output_file && (
                  <button
                    className="jld-node-chip-download"
                    title={`Download ${s.output_file.split('/').pop()}`}
                    disabled={downloadingNode === nodeId}
                    onClick={() => handleDownload(nodeId, s.output_file)}
                  >
                    <Download size={11} />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        <RejectedRecordsModal
          open={!!rejectsNode}
          onClose={() => setRejectsNode(null)}
          title={rejectsNode ? `Rejected records — ${rejectsNode.label}` : ''}
          exportFilename={`job_${jobId}_${rejectsNode?.nodeId}_rejected.csv`}
          fetchPage={(page, pageSize) =>
            pipelineJobApi.getRejectedRecords(jobId, { node_id: rejectsNode?.nodeId, page, page_size: pageSize })
          }
        />

        <div className="jld-log-body">
          {logs.length === 0 ? (
            <div className="jld-log-empty">{emptyLogMessage(job)}</div>
          ) : (
            logs.map((l) => (
              <div key={l.id} className={`jld-log-line ${LEVEL_CLASS[l.level] ?? ''}`}>
                <span className="jld-log-ts">{new Date(l.ts).toLocaleTimeString()}</span>
                <span className="jld-log-level">{l.level}</span>
                {l.node_id && <span className="jld-log-node">[{l.node_id}]</span>}
                <span className="jld-log-msg">{l.message}</span>
              </div>
            ))
          )}
          <div ref={logEndRef} />
        </div>
      </div>
    </div>
  );
}
