/**
 * BulkMigrationRunMonitor — /app/bulk-migration/runs/:runId
 * Live view of one BulkMigrationRun: polls GET /runs/{id} and
 * .../events?since_id= every 1.2s (same cadence/stop-on-terminal
 * convention as JobLogDrawer.jsx) while the run is in an active state,
 * stops automatically once it reaches PENDING/VALIDATING/RUNNING/PAUSING/
 * PAUSED/CANCELLING's terminal counterpart. Real pause/resume/cancel/
 * retry-failed actions hit the real cooperative-cancellation worker pool
 * (bulk_migration_service.py) — nothing here is simulated client-side.
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft, Pause, Play, Square, RotateCcw, Boxes } from 'lucide-react';
import { Button, DataTable, StatusBadge } from '@/components/common/index.js';
import { formatDateTime, formatNumber, formatDuration } from '@/utils/formatters.js';
import {
  bulkMigrationApi, RUN_STATUS_LABEL, RUN_STATUS_VARIANT, TASK_STATUSES, TASK_STATUS_VARIANT,
} from '@/services/api/bulkMigrationApi.js';
import './bulk-migration.css';

function apiErrorMessage(err, fallback) { return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback; }

const TERMINAL = new Set(['COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELLED']);

const COUNTER_SEGMENTS = [
  { key: 'success_objects', cls: 'success' },
  { key: 'running_objects', cls: 'info' },
  { key: 'queued_objects', cls: 'gray' },
  { key: 'pending_objects', cls: 'gray' },
  { key: 'blocked_objects', cls: 'warning' },
  { key: 'failed_objects', cls: 'danger' },
  { key: 'skipped_objects', cls: 'gray' },
  { key: 'cancelled_objects', cls: 'gray' },
];

export default function BulkMigrationRunMonitor() {
  const { runId } = useParams();

  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const [events, setEvents] = useState([]);
  const lastEventId = useRef(0);
  const pollRef = useRef(null);
  const logEndRef = useRef(null);

  const [acting, setActing] = useState(null); // 'pause'|'resume'|'cancel'|'retry'|null

  const [tasks, setTasks] = useState({ items: [], total: 0, page: 1, page_size: 50 });
  const [taskFilter, setTaskFilter] = useState('');
  const [tasksLoading, setTasksLoading] = useState(true);

  const loadTasks = useCallback((page = 1) => {
    setTasksLoading(true);
    bulkMigrationApi.listTasks(runId, { status: taskFilter, page, page_size: 50 })
      .then((r) => { setTasks(r); setTasksLoading(false); })
      .catch(() => setTasksLoading(false));
  }, [runId, taskFilter]);

  useEffect(() => { loadTasks(1); }, [loadTasks]);

  useEffect(() => {
    let cancelled = false;
    lastEventId.current = 0;
    setRun(null);
    setEvents([]);

    const tick = async () => {
      try {
        const [runData, eventData] = await Promise.all([
          bulkMigrationApi.getRun(runId),
          bulkMigrationApi.listEvents(runId, lastEventId.current || undefined),
        ]);
        if (cancelled) return;
        setRun(runData);
        if (eventData.items.length) {
          lastEventId.current = eventData.items[eventData.items.length - 1].id;
          setEvents((prev) => [...prev, ...eventData.items]);
        }
        if (TERMINAL.has(runData.status) && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
          loadTasks(1); // final task snapshot once the run settles
        }
      } catch (err) {
        if (!cancelled) setError(apiErrorMessage(err, 'Could not load this run.'));
      }
    };

    tick();
    pollRef.current = setInterval(tick, 1200);
    return () => { cancelled = true; if (pollRef.current) clearInterval(pollRef.current); };
  }, [runId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { logEndRef.current?.scrollIntoView({ block: 'nearest' }); }, [events.length]);

  const doAction = async (action, fn) => {
    setActing(action);
    try {
      await fn();
    } catch (err) {
      window.alert(apiErrorMessage(err, `Could not ${action} this run.`));
    } finally {
      setActing(null);
    }
  };

  if (error && !run) return <div className="bkm-page"><div className="bkm-error-banner">{error}</div></div>;
  if (!run) return <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</div>;

  const isTerminal = TERMINAL.has(run.status);
  const canPause = run.status === 'RUNNING' && !run.pause_requested;
  const canResume = run.status === 'PAUSED';
  const canCancel = !isTerminal && !run.cancel_requested;
  const canRetry = run.failed_objects > 0;

  const total = run.total_objects || 1;
  const elapsed = run.started_at
    ? Math.max(0, Math.round((new Date(run.completed_at || Date.now()) - new Date(run.started_at)) / 1000))
    : 0;

  const taskColumns = [
    { key: 'object_key', header: 'Object', render: (v) => <span className="bkm-mono">{v}</span> },
    { key: 'dependency_level', header: 'Level' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={TASK_STATUS_VARIANT[v] || 'gray'} dot>{v}</StatusBadge> },
    { key: 'attempt_number', header: 'Attempt' },
    { key: 'rows_processed', header: 'Rows', render: (v) => formatNumber(v || 0) },
    { key: 'throughput_rows_per_sec', header: 'Rows/sec', render: (v) => v ? Math.round(v).toLocaleString() : '—' },
    { key: 'error_message', header: 'Error', render: (v, row) => v ? <span title={v} style={{ color: 'var(--color-danger-dark)' }}>{row.error_code}: {v.length > 40 ? `${v.slice(0, 40)}…` : v}</span> : '—' },
  ];

  return (
    <div className="bkm-page">
      <Link to={`/app/bulk-migration/${run.pipeline_id}`} className="bkm-back-link"><ArrowLeft size={13} /> Pipeline</Link>

      <div className="bkm-detail-header">
        <div>
          <div className="bkm-detail-name">
            <Boxes size={20} /> {run.run_key}
            <StatusBadge variant={RUN_STATUS_VARIANT[run.status] || 'gray'} dot size="md">{RUN_STATUS_LABEL[run.status] || run.status}</StatusBadge>
          </div>
          <div className="bkm-detail-meta">
            <span><strong>Trigger:</strong> {run.trigger_type}</span>
            <span><strong>Workers:</strong> {run.effective_workers}</span>
            <span><strong>Batch:</strong> {formatNumber(run.effective_batch_size)}</span>
            <span><strong>Dep. Levels:</strong> {run.dependency_levels}</span>
            <span><strong>Elapsed:</strong> {formatDuration(elapsed)}</span>
            <span><strong>Started:</strong> {formatDateTime(run.started_at)}</span>
            {run.completed_at && <span><strong>Completed:</strong> {formatDateTime(run.completed_at)}</span>}
          </div>
        </div>
        <div className="bkm-actions">
          {canPause && <Button variant="secondary" size="sm" icon={<Pause size={14} />} loading={acting === 'pause'} onClick={() => doAction('pause', () => bulkMigrationApi.pauseRun(run.id))}>Pause</Button>}
          {canResume && <Button variant="secondary" size="sm" icon={<Play size={14} />} loading={acting === 'resume'} onClick={() => doAction('resume', () => bulkMigrationApi.resumeRun(run.id))}>Resume</Button>}
          {canCancel && <Button variant="danger" size="sm" icon={<Square size={14} />} loading={acting === 'cancel'} onClick={() => doAction('cancel', () => bulkMigrationApi.cancelRun(run.id))}>Cancel</Button>}
          {canRetry && <Button variant="secondary" size="sm" icon={<RotateCcw size={14} />} loading={acting === 'retry'} onClick={() => doAction('retry', async () => { await bulkMigrationApi.retryFailed(run.id, false); loadTasks(1); })}>Retry Failed ({run.failed_objects})</Button>}
        </div>
      </div>

      {run.error_message && <div className="bkm-error-banner">{run.error_message}</div>}

      <div className="bkm-run-summary">
        <div className="bkm-run-summary-card"><div className="bkm-run-summary-val">{formatNumber(run.total_objects)}</div><div className="bkm-run-summary-label">Total Objects</div></div>
        <div className="bkm-run-summary-card"><div className="bkm-run-summary-val success">{formatNumber(run.success_objects)}</div><div className="bkm-run-summary-label">Succeeded</div></div>
        <div className="bkm-run-summary-card"><div className="bkm-run-summary-val danger">{formatNumber(run.failed_objects)}</div><div className="bkm-run-summary-label">Failed</div></div>
        <div className="bkm-run-summary-card"><div className="bkm-run-summary-val info">{formatNumber(run.running_objects)}</div><div className="bkm-run-summary-label">Running</div></div>
        <div className="bkm-run-summary-card"><div className="bkm-run-summary-val">{formatNumber(run.pending_objects + run.queued_objects + run.blocked_objects)}</div><div className="bkm-run-summary-label">Pending/Blocked</div></div>
      </div>

      <div className="bkm-progress-track">
        {COUNTER_SEGMENTS.map((seg) => run[seg.key] > 0 && (
          <div key={seg.key} className={`bkm-progress-seg ${seg.cls}`} style={{ width: `${(run[seg.key] / total) * 100}%` }} title={`${seg.key}: ${run[seg.key]}`} />
        ))}
      </div>
      <div className="bkm-progress-label">
        <span>{formatNumber(run.rows_processed)} rows processed{run.rows_rejected > 0 ? ` (${formatNumber(run.rows_rejected)} rejected)` : ''} of ~{formatNumber(run.total_rows_source)} estimated</span>
        <span>{run.error_count > 0 ? `${run.error_count} error event(s)` : ''}</span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 20, alignItems: 'start' }}>
        <div>
          <div className="bkm-label" style={{ marginBottom: 8, fontSize: 13 }}>Tasks</div>
          <div className="bkm-filters" style={{ marginBottom: 8 }}>
            <select value={taskFilter} onChange={(e) => setTaskFilter(e.target.value)}>
              <option value="">All statuses</option>
              {TASK_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className="bkm-list-table-panel">
            <DataTable
              columns={taskColumns}
              data={tasks.items}
              loading={tasksLoading}
              pagination={{ page: tasks.page, pageSize: tasks.page_size, total: tasks.total, onChange: loadTasks }}
              emptyState={<div style={{ padding: 24, textAlign: 'center', color: 'var(--text-faint)' }}>No tasks{taskFilter ? ` with status ${taskFilter}` : ''} yet.</div>}
            />
          </div>
        </div>

        <div>
          <div className="bkm-label" style={{ marginBottom: 8, fontSize: 13 }}>Live Event Log</div>
          <div className="bkm-log-panel">
            {events.length === 0 ? (
              <div className="bkm-log-empty">Waiting for events…</div>
            ) : (
              events.map((e) => (
                <div key={e.id} className="bkm-log-row">
                  <span className="bkm-log-ts">{new Date(e.event_timestamp).toLocaleTimeString()}</span>
                  <span className={`bkm-log-type ${e.event_type}`}>{e.event_type}</span>
                  <span>{e.message || '—'}</span>
                </div>
              ))
            )}
            <div ref={logEndRef} />
          </div>
        </div>
      </div>
    </div>
  );
}
