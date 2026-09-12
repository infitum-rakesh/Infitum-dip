/**
 * BulkMigrationPipelines — /app/bulk-migration
 * Real list of saved BulkMigrationPipeline rows (bulkMigrationApi.listPipelines()),
 * each showing its real object_count and its most recent real run status
 * (bulkMigrationApi.listRuns({ pipeline_id }) — backend already orders
 * newest-first, so items[0] is it). "Run" fires the real worker pool
 * (fire-and-forget asyncio.create_task on the backend) and immediately
 * navigates to the Run Monitor for live progress, since bulk migration
 * runs are long-running background jobs, not something to wait on inline
 * the way a short Anomaly/Reconciliation job run is.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, Play, Trash2, Boxes } from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState, Modal } from '@/components/common/index.js';
import { formatDateTime, formatNumber } from '@/utils/formatters.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { bulkMigrationApi, RUN_STATUS_LABEL, RUN_STATUS_VARIANT, RUN_ACTIVE_STATUSES } from '@/services/api/bulkMigrationApi.js';
import './bulk-migration.css';

function apiErrorMessage(err, fallback) { return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback; }

const DEFAULT_FORM = {
  name: '', description: '', source_connection_id: '', target_connection_id: '',
  max_workers: 8, batch_size: 5000, max_retries: 3, retry_backoff_seconds: 10, continue_on_error: true,
};

export default function BulkMigrationPipelines() {
  const navigate = useNavigate();

  const [rows, setRows] = useState([]);
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [runningId, setRunningId] = useState(null);
  const [lastRunById, setLastRunById] = useState({});
  const [historyLoaded, setHistoryLoaded] = useState(false);

  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState(DEFAULT_FORM);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setHistoryLoaded(false);
    bulkMigrationApi.listPipelines({ page_size: 200 })
      .then(async (r) => {
        setRows(r.items);
        setLoading(false);
        const entries = await Promise.all(r.items.map(async (p) => {
          try {
            const runs = await bulkMigrationApi.listRuns({ pipeline_id: p.id, page_size: 1 });
            return [p.id, runs.items[0] ?? null];
          } catch {
            return [p.id, null];
          }
        }));
        setLastRunById(Object.fromEntries(entries));
        setHistoryLoaded(true);
      })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load migration pipelines.')); setLoading(false); });
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setConnections(r.items)).catch(() => {});
  }, []);

  const connectionName = (id) => connections.find((c) => c.id === id)?.name || `#${id}`;

  const handleRun = async (row) => {
    setRunningId(row.id);
    try {
      const run = await bulkMigrationApi.runPipeline(row.id);
      navigate(`/app/bulk-migration/runs/${run.id}`);
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not start this run.'));
    } finally {
      setRunningId(null);
    }
  };

  const handleDelete = async (row) => {
    if (!window.confirm(`Delete pipeline "${row.name}"? This removes its object configuration too. This cannot be undone.`)) return;
    try {
      await bulkMigrationApi.deletePipeline(row.id);
      load();
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not delete this pipeline.'));
    }
  };

  const openCreate = () => { setForm(DEFAULT_FORM); setFormError(null); setShowCreate(true); };

  const handleCreate = async (e) => {
    e.preventDefault();
    if (!form.name.trim() || !form.source_connection_id || !form.target_connection_id) {
      setFormError('Name, source connection and target connection are required.');
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      const pipeline = await bulkMigrationApi.createPipeline({
        ...form,
        source_connection_id: Number(form.source_connection_id),
        target_connection_id: Number(form.target_connection_id),
        max_workers: Number(form.max_workers),
        batch_size: Number(form.batch_size),
        max_retries: Number(form.max_retries),
        retry_backoff_seconds: Number(form.retry_backoff_seconds),
      });
      setShowCreate(false);
      navigate(`/app/bulk-migration/${pipeline.id}`);
    } catch (err) {
      setFormError(apiErrorMessage(err, 'Could not create pipeline.'));
    } finally {
      setSaving(false);
    }
  };

  const activeRuns = historyLoaded ? Object.values(lastRunById).filter((r) => r && RUN_ACTIVE_STATUSES.includes(r.status)).length : '—';
  const failedRuns = historyLoaded ? Object.values(lastRunById).filter((r) => r && (r.status === 'FAILED' || r.status === 'COMPLETED_WITH_ERRORS')).length : '—';
  const totalObjects = rows.reduce((sum, p) => sum + (p.object_count || 0), 0);

  const kpis = [
    { label: 'Pipelines', value: rows.length },
    { label: 'Objects Configured', value: formatNumber(totalObjects) },
    { label: 'Active Runs', value: activeRuns, variant: 'info' },
    { label: 'Failed Last Run', value: failedRuns, variant: 'danger' },
  ];

  const columns = [
    {
      key: 'name', header: 'Pipeline', sortable: true,
      render: (v, row) => <button className="bkm-name-link" onClick={() => navigate(`/app/bulk-migration/${row.id}`)}>{v}</button>,
    },
    {
      key: 'source_connection_id', header: 'Source',
      render: (v) => connections.length ? connectionName(v) : <span className="bkm-mono">#{v}</span>,
    },
    {
      key: 'target_connection_id', header: 'Target',
      render: (v) => connections.length ? connectionName(v) : <span className="bkm-mono">#{v}</span>,
    },
    { key: 'object_count', header: 'Objects', sortable: true, render: (v) => formatNumber(v || 0) },
    {
      key: 'last_run', header: 'Last Run',
      render: (_v, row) => {
        if (runningId === row.id) return <StatusBadge variant="info" dot>Starting…</StatusBadge>;
        const run = lastRunById[row.id];
        if (run === undefined) return <span style={{ color: 'var(--text-faint)' }}>Loading…</span>;
        if (!run) return <span style={{ color: 'var(--text-faint)' }}>Never run</span>;
        return (
          <button
            style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer' }}
            onClick={(e) => { e.stopPropagation(); navigate(`/app/bulk-migration/runs/${run.id}`); }}
          >
            <StatusBadge variant={RUN_STATUS_VARIANT[run.status] || 'gray'} dot>{RUN_STATUS_LABEL[run.status] || run.status}</StatusBadge>
          </button>
        );
      },
    },
    { key: 'updated_at', header: 'Updated', sortable: true, render: (v) => formatDateTime(v) },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="bkm-row-actions" onClick={(e) => e.stopPropagation()}>
          <button
            className="bkm-icon-btn bkm-icon-btn-run" title="Run" disabled={runningId === row.id || !row.object_count}
            onClick={() => handleRun(row)}
          >
            {runningId === row.id ? <span className="bkm-spinner" aria-hidden="true" /> : <Play size={14} fill="currentColor" />}
          </button>
          <button className="bkm-icon-btn" title="Delete" onClick={() => handleDelete(row)}>
            <Trash2 size={14} />
          </button>
        </div>
      ),
    },
  ];

  return (
    <div className="bkm-page">
      <div className="bkm-header">
        <div>
          <h1 className="bkm-title"><Boxes size={20} /> Bulk Object Migration</h1>
          <p className="bkm-subtitle">One reusable pipeline moves thousands of source objects to a target system — dependency-ordered, checkpointed, retried.</p>
        </div>
        <div className="bkm-actions">
          <Button variant="primary" icon={<Plus size={14} />} onClick={openCreate}>New Pipeline</Button>
        </div>
      </div>

      {error && <div className="bkm-error-banner">{error}</div>}

      <div className="bkm-kpis">
        {kpis.map((k) => (
          <div key={k.label} className={`bkm-kpi-card ${k.variant ? `bkm-kpi-${k.variant}` : ''}`}>
            <div className="bkm-kpi-value">{k.value}</div>
            <div className="bkm-kpi-label">{k.label}</div>
          </div>
        ))}
      </div>

      <div className="bkm-list-table-panel">
        <DataTable
          columns={columns}
          data={rows}
          loading={loading}
          error={null}
          emptyState={
            <EmptyState
              icon={<Boxes size={28} />}
              title="No migration pipelines yet"
              description="Create a pipeline, configure the objects it should move (or import a CSV of thousands), then validate and run."
              action={{ label: 'New Pipeline', onClick: openCreate }}
            />
          }
        />
      </div>

      <Modal
        open={showCreate}
        onClose={() => !saving && setShowCreate(false)}
        title="New Migration Pipeline"
        size="md"
        footer={
          <>
            <Button variant="secondary" onClick={() => setShowCreate(false)} disabled={saving}>Cancel</Button>
            <Button variant="primary" onClick={handleCreate} loading={saving}>Create Pipeline</Button>
          </>
        }
      >
        <form onSubmit={handleCreate}>
          {formError && <div className="bkm-error-banner">{formError}</div>}
          <div className="bkm-field">
            <label className="bkm-label">Name</label>
            <input className="bkm-input bkm-input-full" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="e.g. Legacy Warehouse Cutover" />
          </div>
          <div className="bkm-field">
            <label className="bkm-label">Description</label>
            <input className="bkm-input bkm-input-full" value={form.description} onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} placeholder="Optional" />
          </div>
          <div className="bkm-field-row">
            <div className="bkm-field">
              <label className="bkm-label">Source Connection</label>
              <select className="bkm-input bkm-select-full" value={form.source_connection_id} onChange={(e) => setForm((f) => ({ ...f, source_connection_id: e.target.value }))}>
                <option value="">Select…</option>
                {connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </div>
            <div className="bkm-field">
              <label className="bkm-label">Target Connection</label>
              <select className="bkm-input bkm-select-full" value={form.target_connection_id} onChange={(e) => setForm((f) => ({ ...f, target_connection_id: e.target.value }))}>
                <option value="">Select…</option>
                {connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </div>
          </div>
          <div className="bkm-field-row">
            <div className="bkm-field">
              <label className="bkm-label">Max Workers</label>
              <input type="number" min="1" max="64" className="bkm-input bkm-input-full" value={form.max_workers} onChange={(e) => setForm((f) => ({ ...f, max_workers: e.target.value }))} />
              <span className="bkm-hint">Bounded further by each connection's own pool limit.</span>
            </div>
            <div className="bkm-field">
              <label className="bkm-label">Batch Size</label>
              <input type="number" min="100" className="bkm-input bkm-input-full" value={form.batch_size} onChange={(e) => setForm((f) => ({ ...f, batch_size: e.target.value }))} />
            </div>
          </div>
          <div className="bkm-field-row">
            <div className="bkm-field">
              <label className="bkm-label">Max Retries (per object)</label>
              <input type="number" min="0" max="10" className="bkm-input bkm-input-full" value={form.max_retries} onChange={(e) => setForm((f) => ({ ...f, max_retries: e.target.value }))} />
            </div>
            <div className="bkm-field">
              <label className="bkm-label">Retry Backoff (seconds)</label>
              <input type="number" min="1" step="1" className="bkm-input bkm-input-full" value={form.retry_backoff_seconds} onChange={(e) => setForm((f) => ({ ...f, retry_backoff_seconds: e.target.value }))} />
            </div>
          </div>
          <label className="bkm-checkbox-row">
            <input type="checkbox" checked={form.continue_on_error} onChange={(e) => setForm((f) => ({ ...f, continue_on_error: e.target.checked }))} />
            Continue running other objects if one fails (isolate failures instead of aborting the whole run)
          </label>
        </form>
      </Modal>
    </div>
  );
}
