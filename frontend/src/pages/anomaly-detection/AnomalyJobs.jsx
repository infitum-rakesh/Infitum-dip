/**
 * AnomalyJobs — /app/anomaly/jobs
 * Real list of saved AnomalyJob rows (anomalyApi.listJobs()), each with a
 * one-click Run (anomalyApi.runJob — the same real detection engine the
 * editor's Run button uses) and Edit action, plus a New Job button. This
 * is the landing page for the Jobs nav item; the per-job editor
 * (AnomalyJobEditor.jsx) is reached via a row's name link, Edit action, or
 * the New Job button. Mirrors ReconciliationList.jsx's exact structure.
 *
 * Last Run is each job's real, persisted latest execution
 * (anomalyApi.listExecutions(id) — returns newest-first per
 * services/anomaly_service.list_executions — so runs[0] is it), not just
 * something triggered from this list in the current browser session.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, Play, Pencil, Trash2, ShieldAlert } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { anomalyApi } from '@/services/api/anomalyApi.js';
import './anomaly-detection.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

/** execution -> {kind: 'pass'|'warning'|'critical'|'error', label, variant} | null (no run yet) */
function summarizeExecution(exec) {
  if (!exec) return null;
  if (exec.status === 'FAILED') return { kind: 'error', label: exec.error_message || 'Failed', variant: 'danger' };
  if (exec.status === 'RUNNING' || exec.status === 'QUEUED') return { kind: 'running', label: exec.status, variant: 'info' };
  const status = exec.overall_status || 'PASS';
  const variant = status === 'CRITICAL' ? 'danger' : status === 'WARNING' ? 'warning' : status === 'ERROR' ? 'danger' : 'success';
  const label = `${(exec.anomalies_found ?? 0).toLocaleString()} anomal${exec.anomalies_found === 1 ? 'y' : 'ies'}`;
  return { kind: status.toLowerCase(), label, variant };
}

export default function AnomalyJobs() {
  const navigate = useNavigate();

  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [runningId, setRunningId] = useState(null);
  // Real latest execution per job id — null once fetched-and-empty
  // (genuinely never run), undefined while still loading.
  const [lastExecById, setLastExecById] = useState({});
  const [historyLoaded, setHistoryLoaded] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setHistoryLoaded(false);
    anomalyApi.listJobs()
      .then(async (items) => {
        setRows(items);
        setLoading(false);
        const entries = await Promise.all(items.map(async (job) => {
          try {
            const execs = await anomalyApi.listExecutions(job.id);
            return [job.id, execs[0] ?? null];
          } catch {
            return [job.id, null];
          }
        }));
        setLastExecById(Object.fromEntries(entries));
        setHistoryLoaded(true);
      })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load anomaly jobs.')); setLoading(false); });
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleRun = async (row) => {
    setRunningId(row.id);
    try {
      const exec = await anomalyApi.runJob(row.id);
      setLastExecById((m) => ({ ...m, [row.id]: exec }));
    } catch (err) {
      setLastExecById((m) => ({ ...m, [row.id]: { status: 'FAILED', error_message: apiErrorMessage(err, 'Run failed.') } }));
    } finally {
      setRunningId(null);
    }
  };

  const handleDelete = async (row) => {
    if (!window.confirm(`Delete anomaly job "${row.name}"? This cannot be undone.`)) return;
    try {
      await anomalyApi.deleteJob(row.id);
      load();
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not delete this job.'));
    }
  };

  const summaries = rows.map((r) => summarizeExecution(lastExecById[r.id]));
  const kpis = [
    { label: 'Jobs', value: rows.length },
    { label: 'Clean', value: historyLoaded ? summaries.filter((s) => s?.kind === 'pass').length : '—', variant: 'success' },
    { label: 'Critical', value: historyLoaded ? summaries.filter((s) => s?.kind === 'critical' || s?.kind === 'error').length : '—', variant: 'danger' },
    { label: 'Never Run', value: historyLoaded ? rows.filter((r) => lastExecById[r.id] === null).length : '—' },
  ];

  const columns = [
    {
      key: 'name', header: 'Job Name', sortable: true,
      render: (v, row) => (
        <button className="anm-list-name-link" onClick={() => navigate(`/app/anomaly/jobs/${row.id}`)}>{v}</button>
      ),
    },
    {
      key: 'object_name', header: 'Object', sortable: true,
      render: (v, row) => (row.uploaded_file_id ? `📄 ${v}` : (row.schema_name ? `${row.schema_name}.${v}` : v)),
    },
    {
      key: 'columns', header: 'Columns',
      render: (v) => (v?.length ? `${v.length} selected` : '—'),
    },
    {
      key: 'detection_config', header: 'Methods',
      render: (v) => (v?.methods?.length ? v.methods.join(', ') : '—'),
    },
    {
      key: 'last_run', header: 'Last Run',
      render: (_v, row) => {
        if (runningId === row.id) return <StatusBadge variant="info" dot>Running…</StatusBadge>;
        const exec = lastExecById[row.id];
        if (exec === undefined) return <span style={{ color: 'var(--text-faint)' }}>Loading…</span>;
        const s = summarizeExecution(exec);
        if (!s) return <span style={{ color: 'var(--text-faint)' }}>Never run</span>;
        return (
          <button
            style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer' }}
            onClick={(e) => { e.stopPropagation(); navigate(`/app/anomaly/executions/${exec.id}`); }}
          >
            <StatusBadge variant={s.variant} dot>{s.label}</StatusBadge>
          </button>
        );
      },
    },
    { key: 'updated_at', header: 'Updated', sortable: true, render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="anm-list-row-actions" onClick={(e) => e.stopPropagation()}>
          <button
            className="anm-list-icon-btn anm-list-icon-btn-run" title="Run" disabled={runningId === row.id}
            onClick={() => handleRun(row)}
          >
            {runningId === row.id ? <span className="anm-list-icon-spinner" aria-hidden="true" /> : <Play size={14} fill="currentColor" />}
          </button>
          <button
            className="anm-list-icon-btn anm-list-icon-btn-edit" title="Edit"
            onClick={() => navigate(`/app/anomaly/jobs/${row.id}`)}
          >
            <Pencil size={14} />
          </button>
          <button className="anm-list-icon-btn" title="Delete" onClick={() => handleDelete(row)}>
            <Trash2 size={14} />
          </button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <div className="anm-header">
        <div>
          <h1 className="anm-list-title"><ShieldAlert size={20} /> Anomaly Jobs</h1>
          <p className="anm-subtitle">Real statistical/rule-based detection jobs, run on demand or on a schedule.</p>
        </div>
        <div className="anm-actions">
          <Button variant="primary" icon={<Plus size={14} />} onClick={() => navigate('/app/anomaly/jobs/new')}>
            New Job
          </Button>
        </div>
      </div>

      <div className="anm-kpis">
        {kpis.map((k) => (
          <div key={k.label} className={`anm-kpi-card ${k.variant === 'danger' ? 'anm-kpi-danger' : ''}`}>
            <div className="anm-kpi-value">{k.value}</div>
            <div className="anm-kpi-label">{k.label}</div>
          </div>
        ))}
      </div>

      <div className="anm-list-table-panel">
        <DataTable
          columns={columns}
          data={rows}
          loading={loading}
          error={error}
          emptyState={
            <EmptyState
              icon={<ShieldAlert size={28} />}
              title="No anomaly jobs yet"
              description="Create a job to scan a real object's columns for statistical, format, categorical, duplicate, distribution or time-series anomalies."
              action={{ label: 'New Job', onClick: () => navigate('/app/anomaly/jobs/new') }}
            />
          }
        />
      </div>
    </div>
  );
}
