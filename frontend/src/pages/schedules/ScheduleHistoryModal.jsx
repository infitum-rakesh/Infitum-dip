import { useCallback, useEffect, useState } from 'react';
import { RotateCw } from 'lucide-react';
import { Button, DataTable, EmptyState, Modal, PermissionGate, StatusBadge } from '@/components/common/index.js';
import { SCHEDULE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime, formatDuration, formatNumber } from '@/utils/formatters.js';
import { scheduleApi } from '@/services/api/scheduleApi.js';

const RUN_STATUS_VARIANT = { RUNNING: 'info', SUCCESS: 'success', WARNING: 'warning', FAILED: 'danger' };

/**
 * ScheduleHistoryModal — "click a schedule and show execution history".
 * Reads run data straight from ScheduleRun rows produced by
 * schedule_service.execute_schedule() — no separate history store.
 */
export function ScheduleHistoryModal({ open, onClose, schedule }) {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState(null);
  const [runDetail, setRunDetail] = useState(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [rerunningId, setRerunningId] = useState(null);

  const load = useCallback(() => {
    if (!schedule) return;
    setLoading(true);
    scheduleApi.getRuns(schedule.id).then((r) => setRuns(r.items)).finally(() => setLoading(false));
  }, [schedule]);

  useEffect(() => { if (open) load(); }, [open, load]);
  useEffect(() => { setExpandedId(null); setRunDetail(null); }, [schedule]);

  const toggleExpand = async (run) => {
    if (expandedId === run.id) { setExpandedId(null); setRunDetail(null); return; }
    setExpandedId(run.id);
    setLoadingDetail(true);
    try {
      const detail = await scheduleApi.getRun(schedule.id, run.id);
      setRunDetail(detail);
    } finally {
      setLoadingDetail(false);
    }
  };

  const handleRerun = async (run) => {
    setRerunningId(run.id);
    try {
      await scheduleApi.rerun(schedule.id, run.id);
      load();
    } finally {
      setRerunningId(null);
    }
  };

  if (!schedule) return null;

  const columns = [
    { key: 'id', header: 'Execution ID', render: (v) => `run_${v}` },
    { key: 'started_at', header: 'Start Time', render: (v) => formatDateTime(v) },
    { key: 'ended_at', header: 'End Time', render: (v) => v ? formatDateTime(v) : '—' },
    { key: 'duration_seconds', header: 'Duration', render: (v) => v != null ? formatDuration(v) : '—' },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={RUN_STATUS_VARIANT[v] ?? 'gray'} dot>{v}</StatusBadge> },
    { key: 'records_processed', header: 'Records Processed', render: (v) => formatNumber(v) },
    { key: 'records_failed', header: 'Errors', render: (v) => v > 0 ? <span className="text-danger">{v}</span> : '0' },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="sched-row-actions" onClick={(e) => e.stopPropagation()}>
          <Button variant="ghost" size="sm" onClick={() => toggleExpand(row)}>{expandedId === row.id ? 'Hide' : 'View Run'}</Button>
          <PermissionGate {...SCHEDULE_PERMISSIONS.SCHEDULE_RUN} fallback={null}>
            <Button variant="ghost" size="sm" icon={<RotateCw size={12} />} loading={rerunningId === row.id} onClick={() => handleRerun(row)}>Rerun</Button>
          </PermissionGate>
        </div>
      ),
    },
  ];

  return (
    <Modal open={open} onClose={onClose} title={`Execution History — ${schedule.name}`} size="lg">
      <DataTable
        columns={columns}
        data={runs}
        loading={loading}
        onRowClick={toggleExpand}
        emptyState={<EmptyState title="No executions yet" description="This schedule hasn't run yet — try Run Now." />}
      />

      {expandedId && (
        <div className="sched-log-panel">
          {loadingDetail ? (
            <div className="text-muted text-sm">Loading logs…</div>
          ) : (
            <>
              {runDetail?.error_message && <div className="sched-run-error">{runDetail.error_message}</div>}
              <div className="card-title" style={{ marginBottom: 8 }}>Logs</div>
              <pre className="sched-log-text">
                {(runDetail?.logs ?? []).map((l) => `[${formatDateTime(l.timestamp)}] ${l.level.padEnd(5)} ${l.message}`).join('\n')}
              </pre>
            </>
          )}
        </div>
      )}
    </Modal>
  );
}
