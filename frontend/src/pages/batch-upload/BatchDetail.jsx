/**
 * BatchDetail (ZB11) — /app/doc-intel/documents/zip-batches/:batchId and
 * /app/invoice-intel/zip-batches/:batchId (same component, parameterized by
 * `basePath`/`backTo`/`backLabel`/`viewDocPath` props from router.jsx).
 *
 * Live view of one DocumentBatch: polls GET .../zip-batches/{id} and
 * .../events?since_id= every 1.2s (same cadence/stop-on-terminal convention
 * as JobLogDrawer.jsx / BulkMigrationRunMonitor.jsx) while the batch is
 * active, plus the current page of the file grid so processing shows up
 * live. Real pause/resume/cancel/retry-failed/retry-selected actions hit
 * the real cooperative-cancellation worker pool
 * (document_batch_service.py, ZB6/ZB7) — nothing here is simulated.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { ArrowLeft, Pause, Play, Square, RotateCcw, PlayCircle, FileArchive, Download, Eye, RefreshCw } from 'lucide-react';
import { Button, DataTable, StatusBadge, PermissionGate } from '@/components/common/index.js';
import { MODULES, ACTIONS } from '@/utils/constants.js';
import { formatDateTime, formatFileSize, formatDuration } from '@/utils/formatters.js';
import {
  createZipBatchApi, BATCH_STATUS_LABEL, BATCH_STATUS_VARIANT, BATCH_TERMINAL_STATUSES,
  FILE_STATUSES, FILE_STATUS_VARIANT,
} from '@/services/api/zipBatchApi.js';
import '@/components/batch-upload/batch-upload.css';

function apiErrorMessage(err, fallback) { return err?.data?.detail || err?.message || fallback; }

/** Part 21 — the resolved Processed Data Storage destination for this
 * batch's own documents. storage_type=null means no per-batch override was
 * ever set at upload time — this batch inherits the org-wide config
 * exactly as before this feature existed (see document_storage_service.py's
 * _effective_config()). */
function describeStorageOverride(batch) {
  if (!batch.storage_type || batch.storage_type === 'SQLITE') return 'SQLite (org default)';
  const dest = batch.storage_type === 'FILE'
    ? `File${batch.storage_target_table ? ` · ${batch.storage_target_table}` : ''}`
    : `Database${batch.storage_target_table ? ` · ${batch.storage_target_table}` : ''}`;
  return `${dest} (connection #${batch.storage_target_connection_id})`;
}

const PROGRESS_SEGMENTS = [
  { key: 'completed_files', cls: 'success' },
  { key: 'review_files', cls: 'warning' },
  { key: 'failed_files', cls: 'danger' },
  { key: 'duplicate_files', cls: 'gray' },
  { key: 'unsupported_files', cls: 'gray' },
];

export default function BatchDetail({ basePath, backTo, backLabel, viewDocPath }) {
  const { batchId } = useParams();
  const navigate = useNavigate();
  const zipBatchApi = useRef(createZipBatchApi(basePath)).current;
  const module = basePath === 'invoices' ? MODULES.INVOICE_INTELLIGENCE : MODULES.DOCUMENT_INTELLIGENCE;
  const viewPermission = { module, action: ACTIONS.VIEW };
  const executePermission = { module, action: ACTIONS.EXECUTE };

  const [batch, setBatch] = useState(null);
  const [error, setError] = useState(null);
  const [events, setEvents] = useState([]);
  const lastEventId = useRef(0);
  const pollRef = useRef(null);
  const logEndRef = useRef(null);

  const [acting, setActing] = useState(null); // 'start'|'pause'|'resume'|'cancel'|'retry-failed'|'retry-selected'|null
  const [actionError, setActionError] = useState(null);

  const [files, setFiles] = useState({ items: [], total: 0, page: 1, page_size: 50 });
  const [fileFilter, setFileFilter] = useState('');
  const [filesLoading, setFilesLoading] = useState(true);
  const [selectedFileIds, setSelectedFileIds] = useState(new Set());

  const loadFiles = useCallback((page = 1) => {
    setFilesLoading(true);
    zipBatchApi.listFiles(batchId, { status: fileFilter, page, page_size: 50 })
      .then((r) => { setFiles(r); setFilesLoading(false); })
      .catch(() => setFilesLoading(false));
  }, [zipBatchApi, batchId, fileFilter]);

  useEffect(() => { loadFiles(1); setSelectedFileIds(new Set()); }, [loadFiles]);

  useEffect(() => {
    let cancelled = false;
    lastEventId.current = 0;
    setBatch(null);
    setEvents([]);

    const tick = async () => {
      try {
        const [batchData, eventData] = await Promise.all([
          zipBatchApi.getBatch(batchId),
          zipBatchApi.listEvents(batchId, lastEventId.current || undefined),
        ]);
        if (cancelled) return;
        setBatch(batchData);
        if (eventData.items.length) {
          lastEventId.current = eventData.items[eventData.items.length - 1].id;
          setEvents((prev) => [...prev, ...eventData.items]);
        }
        if (BATCH_TERMINAL_STATUSES.includes(batchData.status)) {
          if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
          loadFiles(files.page); // final grid snapshot once the batch settles
        } else {
          loadFiles(files.page); // keep the grid live while processing
        }
      } catch (err) {
        if (!cancelled) setError(apiErrorMessage(err, 'Could not load this batch.'));
      }
    };

    tick();
    pollRef.current = setInterval(tick, 1200);
    return () => { cancelled = true; if (pollRef.current) clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchId]);

  useEffect(() => { logEndRef.current?.scrollIntoView({ block: 'nearest' }); }, [events.length]);

  const doAction = async (action, fn) => {
    setActing(action);
    setActionError(null);
    try {
      const updated = await fn();
      if (updated) setBatch(updated);
    } catch (err) {
      setActionError(apiErrorMessage(err, `Could not ${action.replace('-', ' ')} this batch.`));
    } finally {
      setActing(null);
    }
  };

  if (error && !batch) return <div className="zb-page"><div className="zb-error-banner">{error}</div></div>;
  if (!batch) return <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</div>;

  const isTerminal = BATCH_TERMINAL_STATUSES.includes(batch.status);
  const canStart = batch.status === 'QUEUED';
  const canPause = batch.status === 'PROCESSING' && !batch.pause_requested;
  const canResume = batch.status === 'PAUSED';
  const canCancel = !isTerminal && !batch.cancel_requested;
  const canRetryFailed = batch.failed_files > 0 && (batch.status === 'COMPLETED_WITH_ERRORS' || batch.status === 'FAILED');
  const canRetrySelected = selectedFileIds.size > 0;

  const elapsed = batch.started_at
    ? Math.max(0, Math.round((new Date(batch.completed_at || Date.now()) - new Date(batch.started_at)) / 1000))
    : 0;
  const total = batch.total_entries || 1;

  const toggleFile = (id) => setSelectedFileIds((s) => {
    const next = new Set(s);
    next.has(id) ? next.delete(id) : next.add(id);
    return next;
  });

  const fileColumns = [
    {
      key: '_select', header: '', width: 30,
      render: (_v, row) => <input type="checkbox" checked={selectedFileIds.has(row.id)} onChange={() => toggleFile(row.id)} onClick={(e) => e.stopPropagation()} />,
    },
    { key: 'original_filename', header: 'File', render: (v, row) => <span className="zb-mono" title={row.zip_relative_path}>{v}</span> },
    { key: 'file_size', header: 'Size', render: (v) => formatFileSize(v || 0) },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={FILE_STATUS_VARIANT[v] || 'gray'} dot>{v?.replace(/_/g, ' ')}</StatusBadge> },
    { key: 'attempt_number', header: 'Attempt' },
    {
      key: 'error_message', header: 'Error',
      render: (v, row) => v ? <span className="zb-error-cell" title={v}>{row.error_code ? `${row.error_code}: ` : ''}{v.length > 40 ? `${v.slice(0, 40)}…` : v}</span> : '—',
    },
    { key: 'completed_at', header: 'Completed', render: (v) => (v ? formatDateTime(v) : '—') },
    {
      key: '_actions', header: '', width: 140,
      render: (_v, row) => {
        if (row.document_id && viewDocPath) {
          return <button className="doc-icon-btn" title="View document" onClick={(e) => { e.stopPropagation(); navigate(viewDocPath(row.document_id)); }}><Eye size={14} /></button>;
        }
        if (row.status === 'SKIPPED_DUPLICATE' && row.duplicate_of_document_id && viewDocPath) {
          return (
            <button
              className="zb-duplicate-link"
              title="This file's content matches an existing document — no new document was created"
              onClick={(e) => { e.stopPropagation(); navigate(viewDocPath(row.duplicate_of_document_id)); }}
            >
              <Eye size={12} /> View existing #{row.duplicate_of_document_id}
            </button>
          );
        }
        return null;
      },
    },
  ];

  return (
    <div className="zb-page">
      <Link to={backTo} className="zb-back-link"><ArrowLeft size={13} /> {backLabel}</Link>

      <div className="zb-detail-header">
        <div>
          <div className="zb-detail-name">
            <FileArchive size={20} /> {batch.original_filename}
            <StatusBadge variant={BATCH_STATUS_VARIANT[batch.status] || 'gray'} dot size="md">{BATCH_STATUS_LABEL[batch.status] || batch.status}</StatusBadge>
          </div>
          <div className="zb-detail-meta">
            <span><strong>Pipeline:</strong> {batch.pipeline_type === 'INVOICE_INTELLIGENCE' ? 'Invoice Intelligence' : 'Document Intelligence'}</span>
            <span><strong>Workers:</strong> {batch.effective_workers}</span>
            <span><strong>Duplicate Policy:</strong> {batch.duplicate_policy?.replace(/_/g, ' ')}</span>
            <span><strong>Storage:</strong> {describeStorageOverride(batch)}</span>
            <span><strong>Elapsed:</strong> {formatDuration(elapsed)}</span>
            {batch.started_at && <span><strong>Started:</strong> {formatDateTime(batch.started_at)}</span>}
            {batch.completed_at && <span><strong>Completed:</strong> {formatDateTime(batch.completed_at)}</span>}
          </div>
        </div>
        <PermissionGate {...executePermission} fallback={null}>
          <div className="zb-actions">
            {canStart && <Button variant="primary" size="sm" icon={<PlayCircle size={14} />} loading={acting === 'start'} onClick={() => doAction('start', () => zipBatchApi.startBatch(batch.id))}>Start</Button>}
            {canPause && <Button variant="secondary" size="sm" icon={<Pause size={14} />} loading={acting === 'pause'} onClick={() => doAction('pause', () => zipBatchApi.pauseBatch(batch.id))}>Pause</Button>}
            {canResume && <Button variant="secondary" size="sm" icon={<Play size={14} />} loading={acting === 'resume'} onClick={() => doAction('resume', () => zipBatchApi.resumeBatch(batch.id))}>Resume</Button>}
            {canCancel && <Button variant="danger" size="sm" icon={<Square size={14} />} loading={acting === 'cancel'} onClick={() => doAction('cancel', () => zipBatchApi.cancelBatch(batch.id))}>Cancel</Button>}
            {canRetryFailed && <Button variant="secondary" size="sm" icon={<RotateCcw size={14} />} loading={acting === 'retry-failed'} onClick={() => doAction('retry-failed', async () => { const b = await zipBatchApi.retryFailed(batch.id); loadFiles(1); return b; })}>Retry Failed ({batch.failed_files})</Button>}
            {canRetrySelected && <Button variant="secondary" size="sm" icon={<RotateCcw size={14} />} loading={acting === 'retry-selected'} onClick={() => doAction('retry-selected', async () => { const b = await zipBatchApi.retrySelected(batch.id, Array.from(selectedFileIds)); setSelectedFileIds(new Set()); loadFiles(1); return b; })}>Retry Selected ({selectedFileIds.size})</Button>}
          </div>
        </PermissionGate>
      </div>

      {actionError && <div className="zb-error-banner">{actionError}</div>}
      {batch.error_message && <div className="zb-error-banner">{batch.error_message}</div>}

      <div className="zb-kpis">
        <div className="zb-kpi-card"><div className="zb-kpi-val">{batch.total_entries}</div><div className="zb-kpi-label">Total Entries</div></div>
        <div className="zb-kpi-card"><div className="zb-kpi-val zb-kpi-val-success">{batch.completed_files}</div><div className="zb-kpi-label">Completed</div></div>
        <div className="zb-kpi-card"><div className="zb-kpi-val zb-kpi-val-warning">{batch.review_files}</div><div className="zb-kpi-label">Needs Review</div></div>
        <div className="zb-kpi-card"><div className="zb-kpi-val zb-kpi-val-danger">{batch.failed_files}</div><div className="zb-kpi-label">Failed</div></div>
        <div className="zb-kpi-card"><div className="zb-kpi-val">{batch.duplicate_files}</div><div className="zb-kpi-label">Duplicate</div></div>
        <div className="zb-kpi-card"><div className="zb-kpi-val">{batch.unsupported_files}</div><div className="zb-kpi-label">Unsupported</div></div>
      </div>

      <div className="zb-progress-track">
        {PROGRESS_SEGMENTS.map((seg) => batch[seg.key] > 0 && (
          <div key={seg.key} className={`zb-progress-seg ${seg.cls}`} style={{ width: `${(batch[seg.key] / total) * 100}%` }} title={`${seg.key}: ${batch[seg.key]}`} />
        ))}
      </div>
      <div className="zb-progress-label">
        <span>{batch.completed_files + batch.failed_files + batch.review_files + batch.duplicate_files + batch.unsupported_files} of {total} entries settled</span>
      </div>

      <div className="zb-body-grid">
        <div>
          <div className="zb-section-label">
            Files
            <div className="zb-filters">
              <select className="zb-select" value={fileFilter} onChange={(e) => setFileFilter(e.target.value)}>
                <option value="">All statuses</option>
                {FILE_STATUSES.map((s) => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
              </select>
              <Button variant="ghost" size="sm" icon={<RefreshCw size={13} />} onClick={() => loadFiles(files.page)}>Refresh</Button>
              <PermissionGate {...viewPermission} fallback={null}>
                <Button variant="ghost" size="sm" icon={<Download size={13} />} onClick={() => zipBatchApi.downloadErrors(batch.id)}>Error Report</Button>
              </PermissionGate>
            </div>
          </div>
          <div className="zb-panel">
            <DataTable
              columns={fileColumns}
              data={files.items}
              loading={filesLoading}
              pagination={{ page: files.page, pageSize: files.page_size, total: files.total, onChange: loadFiles }}
              emptyState={<div style={{ padding: 24, textAlign: 'center', color: 'var(--text-faint)' }}>No files{fileFilter ? ` with status ${fileFilter}` : ''} yet.</div>}
            />
          </div>
        </div>

        <div>
          <div className="zb-section-label">Live Event Log</div>
          <div className="zb-panel">
            <div className="zb-log-panel">
              {events.length === 0 ? (
                <div className="zb-log-empty">Waiting for events…</div>
              ) : (
                events.map((e) => (
                  <div key={e.id} className="zb-log-row">
                    <span className="zb-log-ts">{new Date(e.event_timestamp).toLocaleTimeString()}</span>
                    <span className="zb-log-type">{e.event_type}</span>
                    <span className="zb-log-msg">{e.message || (e.file_id ? `file #${e.file_id}` : '—')}</span>
                  </div>
                ))
              )}
              <div ref={logEndRef} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
