/**
 * AnomalyResults — /app/anomaly/results
 * Cross-job executions list — every real AnomalyExecution across every
 * saved job, newest first, with job/status filters and a link into the
 * full drill-down (AnomalyExecutionDetail.jsx). Same fan-out pattern
 * AnomalyOverview.jsx uses (listJobs() then listExecutions(jobId) per
 * job) but unbounded (no top-25 cap) since this page's whole purpose is
 * being the complete execution history, not a dashboard summary.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ListChecks } from 'lucide-react';
import { DataTable, StatusBadge, EmptyState } from '@/components/common/index.js';
import { formatDateTime, formatNumber } from '@/utils/formatters.js';
import { anomalyApi } from '@/services/api/anomalyApi.js';
import './anomaly-detection.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

const RUN_STATUS_VARIANT = { SUCCESS: 'success', PARTIAL_SUCCESS: 'warning', FAILED: 'danger', RUNNING: 'info', QUEUED: 'gray', CANCELLED: 'gray' };
const OVERALL_STATUS_VARIANT = { PASS: 'success', WARNING: 'warning', CRITICAL: 'danger', ERROR: 'gray' };

export default function AnomalyResults() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState([]);
  const [executions, setExecutions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [jobFilter, setJobFilter] = useState('ALL');
  const [statusFilter, setStatusFilter] = useState('ALL');

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    anomalyApi.listJobs()
      .then(async (jobRows) => {
        setJobs(jobRows);
        const perJob = await Promise.all(
          jobRows.map((job) =>
            anomalyApi.listExecutions(job.id)
              .then((runs) => runs.map((r) => ({ ...r, job_id: job.id, job_name: job.name })))
              .catch(() => [])
          )
        );
        setExecutions(perJob.flat().sort((a, b) => new Date(b.created_at) - new Date(a.created_at)));
        setLoading(false);
      })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load executions.')); setLoading(false); });
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() => executions.filter((e) => {
    if (jobFilter !== 'ALL' && String(e.job_id) !== jobFilter) return false;
    if (statusFilter !== 'ALL' && e.overall_status !== statusFilter) return false;
    return true;
  }), [executions, jobFilter, statusFilter]);

  const columns = [
    {
      key: 'job_name', header: 'Job', sortable: true,
      render: (v, row) => (
        <button className="anm-list-name-link" onClick={() => navigate(`/app/anomaly/executions/${row.id}`)}>{v}</button>
      ),
    },
    { key: 'status', header: 'Status', render: (v) => <StatusBadge variant={RUN_STATUS_VARIANT[v] || 'gray'}>{v}</StatusBadge> },
    {
      key: 'overall_status', header: 'Overall',
      render: (v) => (v ? <StatusBadge variant={OVERALL_STATUS_VARIANT[v] || 'gray'} dot>{v}</StatusBadge> : '—'),
    },
    { key: 'anomalies_found', header: 'Anomalies', sortable: true, render: (v) => formatNumber(v || 0) },
    { key: 'critical_count', header: 'Critical', sortable: true, render: (v) => formatNumber(v || 0) },
    { key: 'warning_count', header: 'Warning', sortable: true, render: (v) => formatNumber(v || 0) },
    { key: 'records_scanned', header: 'Records', render: (v) => (v != null ? formatNumber(v) : '—') },
    { key: 'trigger', header: 'Trigger' },
    { key: 'started_at', header: 'Started', sortable: true, render: (v) => (v ? formatDateTime(v) : '—') },
  ];

  return (
    <div>
      <div className="anm-header">
        <div>
          <h1 className="anm-list-title"><ListChecks size={20} /> Anomaly Results</h1>
          <p className="anm-subtitle">Every real detection run across every job, newest first.</p>
        </div>
      </div>

      <div className="anm-filters">
        <label>
          Job
          <select value={jobFilter} onChange={(e) => setJobFilter(e.target.value)}>
            <option value="ALL">All jobs</option>
            {jobs.map((j) => <option key={j.id} value={j.id}>{j.name}</option>)}
          </select>
        </label>
        <label>
          Overall status
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="ALL">All statuses</option>
            <option value="PASS">Pass</option>
            <option value="WARNING">Warning</option>
            <option value="CRITICAL">Critical</option>
            <option value="ERROR">Error</option>
          </select>
        </label>
      </div>

      <div className="anm-list-table-panel">
        <DataTable
          columns={columns}
          data={filtered}
          loading={loading}
          error={error}
          emptyState={
            <EmptyState
              icon={<ListChecks size={28} />}
              title="No executions yet"
              description="Run an anomaly job to see its results here."
              action={{ label: 'Go to Jobs', onClick: () => navigate('/app/anomaly/jobs') }}
            />
          }
        />
      </div>
    </div>
  );
}
