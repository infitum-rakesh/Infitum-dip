/**
 * ValidationList — /app/validation
 * Real list of saved ValidationRuleSet rows (validationApi.listRuleSets()),
 * each with a one-click Run (validationApi.runRuleSet — the same real
 * aggregate-SQL engine the editor's Run button uses) and Add Rule (opens
 * the rule-set editor on its Rules tab) action, plus a New Rule Set button.
 * This is the landing page for Validation; the per-rule-set editor
 * (Validation.jsx) is now reached via a row's Add Rule action or the
 * New Rule Set button, not shown inline here.
 *
 * Last Run is each rule set's real, persisted latest run (fetched via
 * validationApi.listRuns(id) — the backend already returns runs sorted
 * newest-first, so runs[0] is it), not just something the user happened
 * to trigger from this list in this browser session — that only updates
 * the same state in place once it lands, via the same summarizeRun()
 * shared with the KPI cards above the table.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, Play, ListChecks, Trash2, ShieldCheck, BarChart3 } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState } from '@/components/common/index.js';
import { formatDateTime } from '@/utils/formatters.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { validationApi } from '@/services/api/validationApi.js';
import './validation.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

/** run -> {kind: 'pass'|'fail'|'error', label, variant} | null (no run yet) */
function summarizeRun(run) {
  if (!run) return null;
  if (run.status === 'FAILED') {
    return { kind: 'error', label: run.error_message || 'Run failed', variant: 'danger' };
  }
  const results = run.results ?? [];
  const passed = results.filter((r) => r.status === 'PASSED').length;
  const hasFail = results.some((r) => r.status === 'FAILED');
  return { kind: hasFail ? 'fail' : 'pass', label: `${passed}/${results.length} rules passed`, variant: hasFail ? 'danger' : 'success' };
}

export default function ValidationList() {
  const navigate = useNavigate();

  const [rows, setRows] = useState([]);
  const [connectionsById, setConnectionsById] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [runningId, setRunningId] = useState(null);
  // Real latest run per rule set id — null once fetched-and-empty (genuinely
  // never run), undefined while still loading. Populated from real history
  // on mount, then kept current in place whenever a Run completes (from
  // this list or, after a navigate-back, from the editor).
  const [lastRunById, setLastRunById] = useState({});
  const [historyLoaded, setHistoryLoaded] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setHistoryLoaded(false);
    validationApi.listRuleSets()
      .then(async (r) => {
        setRows(r.items);
        setLoading(false);
        const entries = await Promise.all(r.items.map(async (rs) => {
          try {
            // GET /validation/rulesets/{id}/runs returns a raw JSON array
            // (response_model=list[ValidationRunOut]) — not {items, total}
            // like the paginated list endpoints elsewhere in this app.
            const runs = await validationApi.listRuns(rs.id);
            return [rs.id, runs[0] ?? null];
          } catch {
            return [rs.id, null];
          }
        }));
        setLastRunById(Object.fromEntries(entries));
        setHistoryLoaded(true);
      })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load validation rule sets.')); setLoading(false); });
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
      const run = await validationApi.runRuleSet(row.id);
      setLastRunById((m) => ({ ...m, [row.id]: run }));
    } catch (err) {
      setLastRunById((m) => ({ ...m, [row.id]: { status: 'FAILED', error_message: apiErrorMessage(err, 'Run failed.') } }));
    } finally {
      setRunningId(null);
    }
  };

  const handleDelete = async (row) => {
    if (!window.confirm(`Delete rule set "${row.name}"? This cannot be undone.`)) return;
    try {
      await validationApi.deleteRuleSet(row.id);
      load();
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not delete this rule set.'));
    }
  };

  const summaries = rows.map((r) => summarizeRun(lastRunById[r.id]));
  const kpis = [
    { label: 'Rule Sets', value: rows.length },
    { label: 'Passing', value: historyLoaded ? summaries.filter((s) => s?.kind === 'pass').length : '—', variant: 'success' },
    { label: 'Failing', value: historyLoaded ? summaries.filter((s) => s?.kind === 'fail' || s?.kind === 'error').length : '—', variant: 'danger' },
    { label: 'Never Run', value: historyLoaded ? rows.filter((r) => lastRunById[r.id] === null).length : '—' },
  ];

  const columns = [
    {
      key: 'name', header: 'Job Name', sortable: true,
      render: (v, row) => (
        <button className="vld-list-name-link" onClick={() => navigate(`/app/validation/${row.id}`)}>{v}</button>
      ),
    },
    {
      key: 'object_name', header: 'Object Name', sortable: true,
      render: (v, row) => (row.schema_name ? `${row.schema_name}.${v}` : v),
    },
    {
      key: 'connection_id', header: 'Connection',
      render: (v) => connectionsById[v]?.name ?? `#${v}`,
    },
    {
      key: 'rules', header: 'Rules',
      render: (v) => {
        const total = v?.length ?? 0;
        const enabled = (v ?? []).filter((r) => r.enabled).length;
        return `${enabled} / ${total} enabled`;
      },
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
        <div className="vld-list-row-actions" onClick={(e) => e.stopPropagation()}>
          <button
            className="vld-list-icon-btn vld-list-icon-btn-run" title="Run" disabled={runningId === row.id}
            onClick={() => handleRun(row)}
          >
            {runningId === row.id ? <span className="vld-list-icon-spinner" aria-hidden="true" /> : <Play size={14} fill="currentColor" />}
          </button>
          <button
            className="vld-list-icon-btn vld-list-icon-btn-addrule" title="Add Rule"
            onClick={() => navigate(`/app/validation/${row.id}`)}
          >
            <ListChecks size={14} />
          </button>
          <button
            className="vld-list-icon-btn vld-list-icon-btn-view" title="View Result"
            onClick={() => navigate(`/app/validation/${row.id}?tab=results`)}
          >
            <BarChart3 size={14} />
          </button>
          <button className="vld-list-icon-btn" title="Delete" onClick={() => handleDelete(row)}>
            <Trash2 size={14} />
          </button>
        </div>
      ),
    },
  ];

  return (
    <div className="vld-page">
      <div className="vld-header">
        <div>
          <h1 className="vld-list-title"><ShieldCheck size={20} /> Validation</h1>
          <p className="vld-subtitle">Real data-quality rule sets, run on demand against a live connection.</p>
        </div>
        <div className="vld-actions">
          <Button variant="primary" icon={<Plus size={14} />} onClick={() => navigate('/app/validation/new')}>
            New Rule Set
          </Button>
        </div>
      </div>

      <div className="vld-list-kpis">
        {kpis.map((k) => (
          <div key={k.label} className="vld-list-kpi-card">
            <div className={`vld-list-kpi-val ${k.variant ? `vld-list-kpi-val-${k.variant}` : ''}`}>{k.value}</div>
            <div className="vld-list-kpi-label">{k.label}</div>
          </div>
        ))}
      </div>

      <div className="vld-list-table-panel">
        <DataTable
          columns={columns}
          data={rows}
          loading={loading}
          error={error}
          emptyState={
            <EmptyState
              icon={<ShieldCheck size={28} />}
              title="No validation rule sets yet"
              description="Create a rule set against a connection and object to start checking data quality."
              action={{ label: 'New Rule Set', onClick: () => navigate('/app/validation/new') }}
            />
          }
        />
      </div>
    </div>
  );
}
