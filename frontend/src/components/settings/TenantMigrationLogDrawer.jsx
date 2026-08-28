/**
 * TenantMigrationLogDrawer — live view of one TenantMigrationJob: status,
 * a real tables_done/tables_total progress bar (not a faked smooth
 * percentage — see models/tenant_migration_job.py), and a live-tailed
 * log. Modeled directly on components/pipeline/JobLogDrawer.jsx: polls
 * GET .../migrations/{id} and .../migrations/{id}/logs?since_id= every
 * 1.2s while the job is in a non-terminal state, stops automatically
 * once COMPLETED/FAILED. Reuses that component's .jld-* CSS classes
 * (pipeline.css) — same drawer shape, different data source.
 */
import { useEffect, useRef, useState } from 'react';
import { X, AlertTriangle, CheckCircle2 } from 'lucide-react';
import { StatusBadge } from '@/components/common/index.js';
import '@/components/pipeline/pipeline.css';

const STATUS_VARIANT = {
  PENDING: 'gray', PROVISIONING_SCHEMA: 'info', COPYING: 'info', VERIFYING: 'info',
  CUTTING_OVER: 'warning', COMPLETED: 'success', FAILED: 'danger',
};
const LEVEL_CLASS = { INFO: 'jld-log-info', WARN: 'jld-log-warn', ERROR: 'jld-log-error' };
const TERMINAL = new Set(['COMPLETED', 'FAILED']);
const STEP_LABEL = {
  PENDING: 'Queued', PROVISIONING_SCHEMA: 'Provisioning schema on target', COPYING: 'Copying rows',
  VERIFYING: 'Verifying row counts', CUTTING_OVER: 'Cutting over — writes briefly paused',
  COMPLETED: 'Migration complete', FAILED: 'Migration failed',
};

export function TenantMigrationLogDrawer({ api, jobId, isPlatform = false, onClose, onStatusChange }) {
  const [job, setJob] = useState(null);
  const [logs, setLogs] = useState([]);
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
          api.get(jobId),
          api.getLogs(jobId, lastLogId.current || undefined),
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

  const progressPct = job && job.tables_total > 0 ? Math.round((job.tables_done / job.tables_total) * 100) : 0;

  return (
    <div className="jld-overlay" onClick={onClose}>
      <div className="jld-panel" onClick={(e) => e.stopPropagation()}>
        <div className="jld-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span className="jld-title">Migration #{jobId}</span>
            {job && <StatusBadge variant={STATUS_VARIANT[job.status] ?? 'gray'} dot>{job.status}</StatusBadge>}
          </div>
          <button className="jld-close" onClick={onClose}><X size={16} /></button>
        </div>

        {job?.status === 'CUTTING_OVER' && (
          <div className="jld-error-banner" style={{ background: 'var(--color-warning-bg)', color: 'var(--color-warning-dark)', borderColor: 'var(--color-warning)' }}>
            <AlertTriangle size={14} style={{ marginRight: 6, verticalAlign: -2 }} />
            {isPlatform
              ? 'Writes for every organization without its own individual database are briefly paused while the final delta copy lands. Reads keep working normally.'
              : 'Writes for this organization are briefly paused while the final delta copy lands. Reads keep working normally.'}
          </div>
        )}
        {job?.status === 'COMPLETED' && (
          <div className="jld-error-banner" style={{ background: 'var(--color-success-bg)', color: 'var(--color-success-dark)', borderColor: 'var(--color-success)' }}>
            <CheckCircle2 size={14} style={{ marginRight: 6, verticalAlign: -2 }} />
            {isPlatform
              ? 'Every organization without its own individual database now routes to the platform database.'
              : 'This organization now routes to its own database.'}
          </div>
        )}
        {job?.error_message && (
          <div className="jld-error-banner">{job.error_message}</div>
        )}

        {job && (
          <div style={{ padding: '10px 16px 0' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 'var(--fs-xs, 11px)', color: 'var(--text-muted)', marginBottom: 4 }}>
              <span>{STEP_LABEL[job.status] ?? job.status}{job.current_table ? ` — ${job.current_table}` : ''}</span>
              <span>{job.tables_done}/{job.tables_total} tables{job.rows_copied ? ` · ${job.rows_copied.toLocaleString()} rows` : ''}</span>
            </div>
            <div style={{ height: 6, borderRadius: 4, background: 'var(--bg-body)', overflow: 'hidden' }}>
              <div
                style={{
                  height: '100%', width: `${progressPct}%`, borderRadius: 4, transition: 'width 0.3s ease',
                  background: job.status === 'FAILED' ? 'var(--color-danger)' : job.status === 'COMPLETED' ? 'var(--color-success)' : 'var(--color-primary)',
                }}
              />
            </div>
          </div>
        )}

        <div className="jld-log-body">
          {logs.length === 0 ? (
            <div className="jld-log-empty">Waiting for the migration to start…</div>
          ) : (
            logs.map((l) => (
              <div key={l.id} className={`jld-log-line ${LEVEL_CLASS[l.level] ?? ''}`}>
                <span className="jld-log-ts">{new Date(l.ts).toLocaleTimeString()}</span>
                <span className="jld-log-level">{l.level}</span>
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
