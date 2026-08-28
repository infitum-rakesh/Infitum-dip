/**
 * ReconciliationList — /app/reconciliation
 * Real list of saved ReconciliationJob rows (reconciliationApi.listJobs()),
 * each with a one-click Run (reconciliationApi.runJob — the same real
 * comparison engine the editor's Run button uses) and Add Rule (opens the
 * job's editor, where key/compare columns and comparison types live)
 * action, plus a New Job button. This is the landing page for
 * Reconciliation; the per-job editor (Reconciliation.jsx) is now reached
 * via a row's Add Rule action or the New Job button, not shown inline here.
 *
 * Last Run is each job's real, persisted latest run (fetched via
 * reconciliationApi.listRuns(id) — the backend already returns runs
 * sorted newest-first, so runs[0] is it), not just something the user
 * happened to trigger from this list in this browser session — that only
 * updates the same state in place once it lands, via the same
 * summarizeRun() shared with the KPI cards above the table.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, Play, ListChecks, Trash2, GitCompare, BarChart3 } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { reconciliationApi } from '@/services/api/reconciliationApi.js';
import './reconciliation.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

/** run -> {kind: 'pass'|'fail'|'error', label, variant} | null (no run yet) */
function summarizeRun(run) {
  if (!run) return null;
  if (run.status !== 'SUCCEEDED') {
    return { kind: 'error', label: run.error_message || run.status, variant: 'danger' };
  }
  const clean = (run.missing_count || 0) + (run.extra_count || 0) + (run.mismatched_count || 0) === 0 && run.checksum_match !== false;
  const label = `${(run.matched_count ?? run.target_count ?? 0).toLocaleString()} / ${(run.source_count ?? 0).toLocaleString()} matched`;
  return { kind: clean ? 'pass' : 'fail', label, variant: clean ? 'success' : 'warning' };
}

export default function ReconciliationList() {
  const navigate = useNavigate();

  const [rows, setRows] = useState([]);
  const [connectionsById, setConnectionsById] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [runningId, setRunningId] = useState(null);
  // Real latest run per job id — null once fetched-and-empty (genuinely
  // never run), undefined while still loading.
  const [lastRunById, setLastRunById] = useState({});
  const [historyLoaded, setHistoryLoaded] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setHistoryLoaded(false);
    reconciliationApi.listJobs()
      .then(async (r) => {
        setRows(r.items);
        setLoading(false);
        const entries = await Promise.all(r.items.map(async (job) => {
          try {
            // GET /reconciliation/jobs/{id}/runs returns a raw JSON array
            // (response_model=list[RunOut]) — not {items, total} like the
            // paginated list endpoints elsewhere in this app.
            const runs = await reconciliationApi.listRuns(job.id);
            return [job.id, runs[0] ?? null];
          } catch {
            return [job.id, null];
          }
        }));
        setLastRunById(Object.fromEntries(entries));
        setHistoryLoaded(true);
      })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load reconciliation jobs.')); setLoading(false); });
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => {
      setConnectionsById(Object.fromEntries(r.items.map((c) => [c.id, c])));
    }).catch(() => {});
  }, []);

  const handleRun = async (row) => {
    setRunningId(row.id);
    try {
      const run = await reconciliationApi.runJob(row.id);
      setLastRunById((m) => ({ ...m, [row.id]: run }));
    } catch (err) {
      setLastRunById((m) => ({ ...m, [row.id]: { status: 'FAILED', error_message: apiErrorMessage(err, 'Run failed.') } }));
    } finally {
      setRunningId(null);
    }
  };

  const handleDelete = async (row) => {
    if (!window.confirm(`Delete reconciliation job "${row.name}"? This cannot be undone.`)) return;
    try {
      await reconciliationApi.deleteJob(row.id);
      load();
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not delete this job.'));
    }
  };

  const summaries = rows.map((r) => summarizeRun(lastRunById[r.id]));
  const kpis = [
    { label: 'Jobs', value: rows.length },
    { label: 'Clean', value: historyLoaded ? summaries.filter((s) => s?.kind === 'pass').length : '—', variant: 'success' },
    { label: 'Discrepancies', value: historyLoaded ? summaries.filter((s) => s?.kind === 'fail' || s?.kind === 'error').length : '—', variant: 'danger' },
    { label: 'Never Run', value: historyLoaded ? rows.filter((r) => lastRunById[r.id] === null).length : '—' },
  ];

  const columns = [
    {
      key: 'name', header: 'Job Name', sortable: true,
      render: (v, row) => (
        <button className="rcn-list-name-link" onClick={() => navigate(`/app/reconciliation/${row.id}`)}>{v}</button>
      ),
    },
    {
      key: 'source_object', header: 'Source Object', sortable: true,
      render: (v, row) => (row.source_schema ? `${row.source_schema}.${v}` : v),
    },
    {
      key: 'target_object', header: 'Target Object', sortable: true,
      render: (v, row) => (row.target_schema ? `${row.target_schema}.${v}` : v),
    },
    {
      key: 'comparison_types', header: 'Comparison Types',
      render: (v) => (v?.length ? v.join(', ') : '—'),
    },
    {
      key: 'last_run', header: 'Last Run',
      render: (_v, row) => {
        if (runningId === row.id) return <StatusBadge variant="info" dot>Running…</StatusBadge>;
        const run = lastRunById[row.id];
        if (run === undefined) return <span style={{ color: 'var(--text-faint)' }}>Loading…</span>;
        const s = summarizeRun(run);
        if (!s) return <span style={{ color: 'var(--text-faint)' }}>Never run</span>;
        return <StatusBadge variant={s.variant} dot>{s.label}</StatusBadge>;
      },
    },
    { key: 'updated_at', header: 'Updated', sortable: true, render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="rcn-list-row-actions" onClick={(e) => e.stopPropagation()}>
          <button
            className="rcn-list-icon-btn rcn-list-icon-btn-run" title="Run" disabled={runningId === row.id}
            onClick={() => handleRun(row)}
          >
            {runningId === row.id ? <span className="rcn-list-icon-spinner" aria-hidden="true" /> : <Play size={14} fill="currentColor" />}
          </button>
          <button
            className="rcn-list-icon-btn rcn-list-icon-btn-addrule" title="Add Rule"
            onClick={() => navigate(`/app/reconciliation/${row.id}`)}
          >
            <ListChecks size={14} />
          </button>
          <button
            className="rcn-list-icon-btn rcn-list-icon-btn-view" title="View Result"
            onClick={() => navigate(`/app/reconciliation/${row.id}?tab=results`)}
          >
            <BarChart3 size={14} />
          </button>
          <button className="rcn-list-icon-btn" title="Delete" onClick={() => handleDelete(row)}>
            <Trash2 size={14} />
          </button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <div className="rcn-header">
        <div>
          <h1 className="rcn-list-title"><GitCompare size={20} /> Reconciliation</h1>
          <p className="rcn-subtitle">Real source-vs-target comparison jobs, run on demand.</p>
        </div>
        <div className="rcn-actions">
          <Button variant="primary" icon={<Plus size={14} />} onClick={() => navigate('/app/reconciliation/new')}>
            New Job
          </Button>
        </div>
      </div>

      <div className="rcn-list-kpis">
        {kpis.map((k) => (
          <div key={k.label} className="rcn-list-kpi-card">
            <div className={`rcn-list-kpi-val ${k.variant ? `rcn-list-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
            <div className="rcn-list-kpi-label">{k.label}</div>
          </div>
        ))}
      </div>

      <div className="rcn-list-table-panel">
        <DataTable
          columns={columns}
          data={rows}
          loading={loading}
          error={error}
          emptyState={
            <EmptyState
              icon={<GitCompare size={28} />}
              title="No reconciliation jobs yet"
              description="Create a job to compare a source and target dataset for real discrepancies."
              action={{ label: 'New Job', onClick: () => navigate('/app/reconciliation/new') }}
            />
          }
        />
      </div>
    </div>
  );
}
