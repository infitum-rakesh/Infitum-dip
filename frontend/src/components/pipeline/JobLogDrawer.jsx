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
          setLogs((prev) => [...prev, ...logData.items]);
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
  const nodeEntries = job ? Object.entries(job.node_stats || {}) : [];
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
              <Button variant="danger" size="sm" icon={<Square size={12} fill="currentColor" />} loading={cancelling} onClick={handleCancel}>
                Abort
              </Button>
            )}
            <button className="jld-close" onClick={onClose}><X size={16} /></button>
          </div>
        </div>

        {job?.error_message && (
          <div className="jld-error-banner">{job.error_message}</div>
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
            <div className="jld-log-empty">Waiting for the job to start…</div>
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
