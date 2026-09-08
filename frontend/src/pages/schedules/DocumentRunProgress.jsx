import { useEffect, useState } from 'react';
import { XCircle } from 'lucide-react';
import { Button, DataTable, StatusBadge } from '@/components/common/index.js';
import { formatDateTime, formatFileSize } from '@/utils/formatters.js';
import { scheduleApi } from '@/services/api/scheduleApi.js';

const DOC_STATUS_VARIANT = {
  DISCOVERED: 'gray', QUEUED: 'gray', PROCESSING: 'info', OCR_PROCESSING: 'info', AI_PROCESSING: 'info',
  VALIDATING: 'info', PROCESSED: 'success', REVIEW_REQUIRED: 'warning', FAILED: 'danger',
  SKIPPED: 'gray', UNSUPPORTED: 'gray',
};

const STAT_TILES = [
  { key: 'discovered', label: 'Discovered' },
  { key: 'queued', label: 'Queued' },
  { key: 'processed', label: 'Processed' },
  { key: 'success', label: 'Success', tone: 'success' },
  { key: 'review', label: 'Review Required', tone: 'warning' },
  { key: 'failed', label: 'Failed', tone: 'danger' },
  { key: 'skipped', label: 'Skipped' },
  { key: 'unsupported', label: 'Unsupported' },
];

// PAY17 — approval-outcome counters, populated only for document_type=
// "INVOICE" schedules (schedule_service._classify_invoice_approval_
// outcome(), spec §120's "job metrics show workflow creation counts").
// Every OTHER document schedule also carries these keys at 0 (the
// backend's stats dict always includes them), so this tile row is only
// rendered when the caller tells us this run's schedule is actually
// invoice-typed — a row of permanent zeros on a scanned-receipts
// schedule would just be noise.
const APPROVAL_STAT_TILES = [
  { key: 'auto_approved', label: 'Auto-Approved', tone: 'success' },
  { key: 'workflows_created', label: 'Workflows Created', tone: 'info' },
  { key: 'already_in_workflow', label: 'Already In Workflow' },
  { key: 'rule_conflicts', label: 'Rule Conflicts', tone: 'warning' },
  { key: 'approver_not_found', label: 'Approver Not Found', tone: 'danger' },
];

/** DocumentRunProgress — the Scheduled Document Intelligence Workflow's
 * run-detail progress view (spec section 15/16): a progress bar, per-
 * outcome stat tiles, current file/stage while RUNNING, Cancel, and a
 * per-file breakdown table backed by GET .../runs/{id}/documents. The
 * parent (ScheduleHistoryModal) owns the ~3s poll that keeps `run` fresh
 * while RUNNING; this component just renders whatever it's given and
 * fetches its own (much less frequently changing) per-file table. */
export function DocumentRunProgress({ scheduleId, run, onCancel, cancelling, isInvoiceSchedule }) {
  const [rows, setRows] = useState([]);
  const [loadingRows, setLoadingRows] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoadingRows(true);
    scheduleApi.getRunDocuments(scheduleId, run.id, { page_size: 200 })
      .then((r) => { if (!cancelled) setRows(r.items ?? []); })
      .finally(() => { if (!cancelled) setLoadingRows(false); });
    return () => { cancelled = true; };
    // Re-fetch whenever the run's aggregate `processed` count changes —
    // cheap signal that per-file rows have moved, without polling this
    // table on its own independent timer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scheduleId, run.id, run.document_stats?.processed, run.status]);

  const stats = run.document_stats ?? {};
  const total = stats.queued || 0;
  const done = stats.processed || 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : (run.status === 'RUNNING' ? 0 : 100);

  const columns = [
    { key: 'file_name', header: 'File' },
    { key: 'file_size', header: 'Size', render: (v) => formatFileSize(v) },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={DOC_STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'processing_attempts', header: 'Attempts' },
    { key: 'last_error', header: 'Error', render: (v) => v ? <span className="text-danger" title={v}>{v.length > 60 ? `${v.slice(0, 60)}…` : v}</span> : '—' },
    { key: 'last_processed_at', header: 'Last Processed', render: (v) => v ? formatDateTime(v) : '—' },
  ];

  return (
    <div className="sched-doc-progress">
      <div className="sched-doc-progress-header">
        <div className="sched-doc-progress-bar-track">
          <div className="sched-doc-progress-bar-fill" style={{ width: `${pct}%` }} />
        </div>
        <div className="sched-doc-progress-label">
          {done} / {total} {total === 1 ? 'file' : 'files'} ({pct}%)
        </div>
        {onCancel && (
          <Button variant="ghost" size="sm" icon={<XCircle size={14} />} loading={cancelling} onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>

      {run.status === 'RUNNING' && (stats.current_file || stats.current_stage) && (
        <div className="sched-doc-progress-current">
          Current: <strong>{stats.current_file ?? '—'}</strong>
          {stats.current_stage && <> — <span className="text-muted">{stats.current_stage}</span></>}
        </div>
      )}
      {run.cancel_requested && run.status === 'RUNNING' && (
        <div className="sched-doc-progress-current text-muted">Cancelling — remaining queued files will be skipped, files already in progress will finish.</div>
      )}

      <div className="sched-doc-stat-tiles">
        {STAT_TILES.map((t) => (
          <div key={t.key} className={`sched-doc-stat-tile${t.tone ? ` sched-doc-stat-tile--${t.tone}` : ''}`}>
            <div className="sched-doc-stat-value">{stats[t.key] ?? 0}</div>
            <div className="sched-doc-stat-label">{t.label}</div>
          </div>
        ))}
      </div>

      {isInvoiceSchedule && (
        <>
          <div className="card-title" style={{ marginTop: 16, marginBottom: 8 }}>Approval Outcomes</div>
          <div className="sched-doc-stat-tiles">
            {APPROVAL_STAT_TILES.map((t) => (
              <div key={t.key} className={`sched-doc-stat-tile${t.tone ? ` sched-doc-stat-tile--${t.tone}` : ''}`}>
                <div className="sched-doc-stat-value">{stats[t.key] ?? 0}</div>
                <div className="sched-doc-stat-label">{t.label}</div>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="card-title" style={{ marginTop: 16, marginBottom: 8 }}>Files</div>
      <DataTable columns={columns} data={rows} loading={loadingRows} />
    </div>
  );
}
