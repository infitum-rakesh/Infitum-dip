/**
 * BulkMigrationPipelineDetail — /app/bulk-migration/:pipelineId
 * The objects-grid workspace for one pipeline: configure/import thousands
 * of object rows (bulk_migration_config_service.py), then Validate/Dry Run
 * (bulk_migration_planner_service.py's dependency-graph + connectivity
 * checks — writes nothing) and Run (fires the real bounded worker pool,
 * bulk_migration_service.py, and redirects to the live Run Monitor).
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams, Link } from 'react-router-dom';
import {
  ArrowLeft, Plus, Play, ShieldCheck, FlaskConical, Upload, Download,
  Pencil, Trash2, Settings, Boxes, CheckCircle2, XCircle, AlertTriangle,
} from 'lucide-react';
import { Button, DataTable, StatusBadge, EmptyState, Modal } from '@/components/common/index.js';
import { formatDateTime, formatNumber } from '@/utils/formatters.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import {
  bulkMigrationApi, OBJECT_TYPES, LOAD_TYPES, RUN_STATUS_LABEL, RUN_STATUS_VARIANT,
} from '@/services/api/bulkMigrationApi.js';
import './bulk-migration.css';

function apiErrorMessage(err, fallback) { return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback; }

const DEFAULT_OBJECT_FORM = {
  source_schema: '', source_object: '', target_schema: '', target_object: '',
  object_type: 'TABLE', load_type: 'FULL_REPLACE', primary_key: '', filter_condition: '',
  priority: 100, dependency_group: '', dependency_order: 0, depends_on: '',
  enabled: true, truncate_before_load: true, create_target_if_not_exists: true,
};

export default function BulkMigrationPipelineDetail() {
  const { pipelineId } = useParams();
  const navigate = useNavigate();

  const [pipeline, setPipeline] = useState(null);
  const [connections, setConnections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [objects, setObjects] = useState({ items: [], total: 0, page: 1, page_size: 50 });
  const [objLoading, setObjLoading] = useState(true);
  const [filters, setFilters] = useState({ search: '', enabled: '', object_type: '', load_type: '' });
  const [selected, setSelected] = useState(new Set());

  const [recentRuns, setRecentRuns] = useState([]);

  const [objModal, setObjModal] = useState(null); // { mode: 'create'|'edit', form, editingId }
  const [savingObj, setSavingObj] = useState(false);
  const [objFormError, setObjFormError] = useState(null);

  // Real schema/object browsing for the Add/Edit Object modal — same
  // connectionApi.getSchemas()/getObjects() cascade Reconciliation's
  // ObjectPicker uses, sourced from the PIPELINE's own fixed source/target
  // connections (never a separate connection picker here). Source must
  // already exist so it's a strict dropdown; target may not exist yet
  // (create_target_if_not_exists defaults on), so it stays a free-text
  // input with a <datalist> of real existing objects for convenience.
  const [srcSchemas, setSrcSchemas] = useState([]);
  const [srcObjects, setSrcObjects] = useState([]);
  const [tgtSchemas, setTgtSchemas] = useState([]);
  const [tgtObjects, setTgtObjects] = useState([]);
  const [loadingSrcSchemas, setLoadingSrcSchemas] = useState(false);
  const [loadingSrcObjects, setLoadingSrcObjects] = useState(false);
  const [loadingTgtSchemas, setLoadingTgtSchemas] = useState(false);
  const [loadingTgtObjects, setLoadingTgtObjects] = useState(false);

  const [configModal, setConfigModal] = useState(false);
  const [configForm, setConfigForm] = useState(null);
  const [savingConfig, setSavingConfig] = useState(false);

  const [importModal, setImportModal] = useState(false);
  const [importFile, setImportFile] = useState(null);
  const [importDryRun, setImportDryRun] = useState(true);
  const [importResult, setImportResult] = useState(null);
  const [importing, setImporting] = useState(false);

  const [vResult, setVResult] = useState(null); // { kind: 'validate'|'dry-run', data }
  const [vLoading, setVLoading] = useState(null); // 'validate'|'dry-run'|'run'|null

  const loadPipeline = useCallback(() => {
    setLoading(true);
    bulkMigrationApi.getPipeline(pipelineId)
      .then((p) => { setPipeline(p); setLoading(false); })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load this pipeline.')); setLoading(false); });
  }, [pipelineId]);

  const loadObjects = useCallback((page = 1) => {
    setObjLoading(true);
    bulkMigrationApi.listObjects(pipelineId, {
      search: filters.search, enabled: filters.enabled === '' ? undefined : filters.enabled === 'true',
      object_type: filters.object_type, load_type: filters.load_type, page, page_size: 50,
    })
      .then((r) => { setObjects(r); setObjLoading(false); setSelected(new Set()); })
      .catch(() => setObjLoading(false));
  }, [pipelineId, filters]);

  useEffect(() => { loadPipeline(); }, [loadPipeline]);
  useEffect(() => { loadObjects(1); }, [loadObjects]);
  useEffect(() => {
    connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setConnections(r.items)).catch(() => {});
  }, []);
  useEffect(() => {
    bulkMigrationApi.listRuns({ pipeline_id: pipelineId, page_size: 10 }).then((r) => setRecentRuns(r.items)).catch(() => {});
  }, [pipelineId]);

  const connectionName = (id) => connections.find((c) => c.id === id)?.name || `#${id}`;

  // ── Object modal: real schema/object browsing off the pipeline's own
  // fixed source/target connections (same cascade as Reconciliation's
  // ObjectPicker). Deps use Boolean(objModal) rather than objModal itself
  // so typing in other fields doesn't re-trigger a fetch — only opening/
  // closing the modal or changing the schema does.
  useEffect(() => {
    if (!objModal || !pipeline) { setSrcSchemas([]); return; }
    setLoadingSrcSchemas(true);
    connectionApi.getSchemas(pipeline.source_connection_id)
      .then((r) => {
        setSrcSchemas(r.schemas);
        if (r.schemas.length === 1 && !objModal.form.source_schema) {
          setObjModal((m) => (m ? { ...m, form: { ...m.form, source_schema: r.schemas[0] } } : m));
        }
      })
      .catch(() => setSrcSchemas([])).finally(() => setLoadingSrcSchemas(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Boolean(objModal), pipeline?.source_connection_id]);

  useEffect(() => {
    if (!objModal || !pipeline) { setTgtSchemas([]); return; }
    setLoadingTgtSchemas(true);
    connectionApi.getSchemas(pipeline.target_connection_id)
      .then((r) => {
        setTgtSchemas(r.schemas);
        if (r.schemas.length === 1 && !objModal.form.target_schema) {
          setObjModal((m) => (m ? { ...m, form: { ...m.form, target_schema: r.schemas[0] } } : m));
        }
      })
      .catch(() => setTgtSchemas([])).finally(() => setLoadingTgtSchemas(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Boolean(objModal), pipeline?.target_connection_id]);

  useEffect(() => {
    if (!objModal || !pipeline || !objModal.form.source_schema) { setSrcObjects([]); return; }
    setLoadingSrcObjects(true);
    connectionApi.getObjects(pipeline.source_connection_id, { schema: objModal.form.source_schema })
      .then((r) => setSrcObjects(r.items)).catch(() => setSrcObjects([])).finally(() => setLoadingSrcObjects(false));
  }, [Boolean(objModal), pipeline?.source_connection_id, objModal?.form.source_schema]);

  useEffect(() => {
    if (!objModal || !pipeline || !objModal.form.target_schema) { setTgtObjects([]); return; }
    setLoadingTgtObjects(true);
    connectionApi.getObjects(pipeline.target_connection_id, { schema: objModal.form.target_schema })
      .then((r) => setTgtObjects(r.items)).catch(() => setTgtObjects([])).finally(() => setLoadingTgtObjects(false));
  }, [Boolean(objModal), pipeline?.target_connection_id, objModal?.form.target_schema]);

  // ── Object CRUD ──────────────────────────────────────────────
  const openAddObject = () => { setObjModal({ mode: 'create', form: DEFAULT_OBJECT_FORM }); setObjFormError(null); };
  const openEditObject = (row) => {
    setObjModal({
      mode: 'edit', editingId: row.id,
      form: {
        ...DEFAULT_OBJECT_FORM,
        ...row,
        primary_key: (row.primary_key || []).join(', '),
        depends_on: (row.depends_on || []).join(', '),
      },
    });
    setObjFormError(null);
  };

  const submitObjectForm = async (e) => {
    e.preventDefault();
    const f = objModal.form;
    if (!f.source_object.trim() || !f.target_object.trim()) {
      setObjFormError('Source object and target object are required.');
      return;
    }
    const payload = {
      source_schema: f.source_schema || null, source_object: f.source_object,
      target_schema: f.target_schema || null, target_object: f.target_object,
      object_type: f.object_type, load_type: f.load_type,
      primary_key: f.primary_key ? f.primary_key.split(',').map((s) => s.trim()).filter(Boolean) : null,
      filter_condition: f.filter_condition || null,
      priority: Number(f.priority) || 100,
      dependency_group: f.dependency_group || null,
      dependency_order: Number(f.dependency_order) || 0,
      depends_on: f.depends_on ? f.depends_on.split(',').map((s) => s.trim()).filter(Boolean) : null,
      enabled: !!f.enabled,
      truncate_before_load: !!f.truncate_before_load,
      create_target_if_not_exists: !!f.create_target_if_not_exists,
    };
    setSavingObj(true);
    setObjFormError(null);
    try {
      if (objModal.mode === 'create') await bulkMigrationApi.createObject(pipelineId, payload);
      else await bulkMigrationApi.updateObject(pipelineId, objModal.editingId, payload);
      setObjModal(null);
      loadObjects(objects.page);
      loadPipeline();
    } catch (err) {
      setObjFormError(apiErrorMessage(err, 'Could not save this object.'));
    } finally {
      setSavingObj(false);
    }
  };

  const handleDeleteObject = async (row) => {
    if (!window.confirm(`Remove object "${row.object_key}" from this pipeline?`)) return;
    try {
      await bulkMigrationApi.deleteObject(pipelineId, row.id);
      loadObjects(objects.page);
      loadPipeline();
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not delete this object.'));
    }
  };

  const toggleEnabled = async (row) => {
    try {
      await bulkMigrationApi.updateObject(pipelineId, row.id, { enabled: !row.enabled });
      loadObjects(objects.page);
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not update this object.'));
    }
  };

  // ── Bulk actions ─────────────────────────────────────────────
  const toggleSelected = (id) => setSelected((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const toggleSelectAll = () => setSelected((s) => (s.size === objects.items.length ? new Set() : new Set(objects.items.map((o) => o.id))));

  const bulkAction = async (patch) => {
    try {
      await bulkMigrationApi.bulkUpdateObjects(pipelineId, [...selected], patch);
      loadObjects(objects.page);
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Bulk update failed.'));
    }
  };
  const bulkDelete = async () => {
    if (!window.confirm(`Remove ${selected.size} selected object(s) from this pipeline?`)) return;
    try {
      await bulkMigrationApi.bulkDeleteObjects(pipelineId, [...selected]);
      loadObjects(objects.page);
      loadPipeline();
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Bulk delete failed.'));
    }
  };

  // ── Pipeline config edit ────────────────────────────────────
  const openConfig = () => {
    setConfigForm({
      name: pipeline.name, description: pipeline.description || '',
      source_connection_id: pipeline.source_connection_id, target_connection_id: pipeline.target_connection_id,
      max_workers: pipeline.max_workers, batch_size: pipeline.batch_size, max_retries: pipeline.max_retries,
      retry_backoff_seconds: pipeline.retry_backoff_seconds, continue_on_error: pipeline.continue_on_error,
    });
    setConfigModal(true);
  };
  const saveConfig = async (e) => {
    e.preventDefault();
    setSavingConfig(true);
    try {
      const p = await bulkMigrationApi.updatePipeline(pipelineId, {
        ...configForm,
        source_connection_id: Number(configForm.source_connection_id),
        target_connection_id: Number(configForm.target_connection_id),
        max_workers: Number(configForm.max_workers),
        batch_size: Number(configForm.batch_size),
        max_retries: Number(configForm.max_retries),
        retry_backoff_seconds: Number(configForm.retry_backoff_seconds),
      });
      setPipeline(p);
      setConfigModal(false);
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not save configuration.'));
    } finally {
      setSavingConfig(false);
    }
  };

  // ── Import ───────────────────────────────────────────────────
  const submitImport = async () => {
    if (!importFile) return;
    setImporting(true);
    try {
      const result = await bulkMigrationApi.importConfig(pipelineId, importFile, { dryRun: importDryRun });
      setImportResult(result);
      if (!importDryRun) { loadObjects(1); loadPipeline(); }
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Import failed.'));
    } finally {
      setImporting(false);
    }
  };
  const closeImport = () => { setImportModal(false); setImportFile(null); setImportResult(null); setImportDryRun(true); };

  const handleExport = async () => {
    try {
      const { blob, filename } = await bulkMigrationApi.exportConfig(pipelineId, 'csv');
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = filename; document.body.appendChild(a); a.click();
      a.remove(); URL.revokeObjectURL(url);
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Export failed.'));
    }
  };

  // ── Validate / Dry Run / Run ─────────────────────────────────
  const runValidate = async () => {
    setVLoading('validate');
    try {
      const data = await bulkMigrationApi.validatePipeline(pipelineId);
      setVResult({ kind: 'validate', data });
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Validation failed to run.'));
    } finally { setVLoading(null); }
  };
  const runDryRun = async () => {
    setVLoading('dry-run');
    try {
      const data = await bulkMigrationApi.dryRunPipeline(pipelineId);
      setVResult({ kind: 'dry-run', data });
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Dry run failed to run.'));
    } finally { setVLoading(null); }
  };
  const runNow = async () => {
    if (!objects.total) { window.alert('Add at least one object before running this pipeline.'); return; }
    if (!window.confirm(`Run "${pipeline.name}" now against ${formatNumber(pipeline.object_count)} configured object(s)?`)) return;
    setVLoading('run');
    try {
      const run = await bulkMigrationApi.runPipeline(pipelineId);
      navigate(`/app/bulk-migration/runs/${run.id}`);
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not start this run.'));
      setVLoading(null);
    }
  };

  if (loading) return <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</div>;
  if (error || !pipeline) return <div className="bkm-error-banner">{error || 'Pipeline not found.'}</div>;

  const objColumns = [
    {
      key: '_select', header: <input type="checkbox" checked={selected.size > 0 && selected.size === objects.items.length} onChange={toggleSelectAll} />,
      render: (_v, row) => <input type="checkbox" checked={selected.has(row.id)} onChange={() => toggleSelected(row.id)} onClick={(e) => e.stopPropagation()} />,
    },
    { key: 'object_key', header: 'Object', render: (v) => <span className="bkm-mono">{v}</span> },
    { key: 'object_type', header: 'Type' },
    { key: 'load_type', header: 'Load Type' },
    {
      key: 'dependency_group', header: 'Dependency',
      render: (v, row) => v ? `${v} (${row.dependency_order})` : (row.depends_on?.length ? `${row.depends_on.length} dep(s)` : '—'),
    },
    { key: 'priority', header: 'Priority' },
    {
      key: 'enabled', header: 'Enabled',
      render: (v, row) => (
        <button onClick={(e) => { e.stopPropagation(); toggleEnabled(row); }} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }} title="Toggle enabled">
          {v ? <CheckCircle2 size={16} color="var(--color-success, #22C55E)" /> : <XCircle size={16} color="var(--text-faint)" />}
        </button>
      ),
    },
    {
      key: 'actions', header: 'Actions',
      render: (_v, row) => (
        <div className="bkm-row-actions" onClick={(e) => e.stopPropagation()}>
          <button className="bkm-icon-btn bkm-icon-btn-edit" title="Edit" onClick={() => openEditObject(row)}><Pencil size={14} /></button>
          <button className="bkm-icon-btn" title="Delete" onClick={() => handleDeleteObject(row)}><Trash2 size={14} /></button>
        </div>
      ),
    },
  ];

  return (
    <div className="bkm-page">
      <Link to="/app/bulk-migration" className="bkm-back-link"><ArrowLeft size={13} /> Pipelines</Link>

      <div className="bkm-detail-header">
        <div>
          <div className="bkm-detail-name"><Boxes size={20} /> {pipeline.name} <StatusBadge variant="gray">v{pipeline.version}</StatusBadge></div>
          {pipeline.description && <div className="bkm-detail-desc">{pipeline.description}</div>}
          <div className="bkm-detail-meta">
            <span><strong>Source:</strong> {connectionName(pipeline.source_connection_id)}</span>
            <span><strong>Target:</strong> {connectionName(pipeline.target_connection_id)}</span>
            <span><strong>Workers:</strong> {pipeline.max_workers}</span>
            <span><strong>Batch:</strong> {formatNumber(pipeline.batch_size)}</span>
            <span><strong>Objects:</strong> {formatNumber(pipeline.object_count)}</span>
          </div>
        </div>
        <Button variant="secondary" icon={<Settings size={14} />} onClick={openConfig}>Edit Config</Button>
      </div>

      <div className="bkm-toolbar">
        <Button variant="secondary" size="sm" icon={<Plus size={14} />} onClick={openAddObject}>Add Object</Button>
        <Button variant="secondary" size="sm" icon={<Upload size={14} />} onClick={() => setImportModal(true)}>Import</Button>
        <Button variant="secondary" size="sm" icon={<Download size={14} />} onClick={handleExport} disabled={!objects.total}>Export</Button>
        <div className="bkm-toolbar-spacer" />
        <Button variant="secondary" size="sm" icon={<ShieldCheck size={14} />} loading={vLoading === 'validate'} onClick={runValidate}>Validate</Button>
        <Button variant="secondary" size="sm" icon={<FlaskConical size={14} />} loading={vLoading === 'dry-run'} onClick={runDryRun}>Dry Run</Button>
        <Button variant="primary" size="sm" icon={<Play size={14} />} loading={vLoading === 'run'} onClick={runNow}>Run Pipeline</Button>
      </div>

      {selected.size > 0 && (
        <div className="bkm-toolbar" style={{ background: 'var(--color-primary-light)', borderRadius: 'var(--radius-md)', padding: '8px 12px' }}>
          <span style={{ fontSize: 12.5, fontWeight: 600 }}>{selected.size} selected</span>
          <Button variant="secondary" size="sm" onClick={() => bulkAction({ enabled: true })}>Enable</Button>
          <Button variant="secondary" size="sm" onClick={() => bulkAction({ enabled: false })}>Disable</Button>
          <Button variant="danger" size="sm" onClick={bulkDelete}>Delete</Button>
        </div>
      )}

      <div className="bkm-filters">
        <input className="bkm-input" placeholder="Search objects…" value={filters.search} onChange={(e) => setFilters((f) => ({ ...f, search: e.target.value }))} />
        <select value={filters.enabled} onChange={(e) => setFilters((f) => ({ ...f, enabled: e.target.value }))}>
          <option value="">All (enabled + disabled)</option>
          <option value="true">Enabled only</option>
          <option value="false">Disabled only</option>
        </select>
        <select value={filters.object_type} onChange={(e) => setFilters((f) => ({ ...f, object_type: e.target.value }))}>
          <option value="">Any object type</option>
          {OBJECT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <select value={filters.load_type} onChange={(e) => setFilters((f) => ({ ...f, load_type: e.target.value }))}>
          <option value="">Any load type</option>
          {LOAD_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>

      <div className="bkm-list-table-panel">
        <DataTable
          columns={objColumns}
          data={objects.items}
          loading={objLoading}
          pagination={{ page: objects.page, pageSize: objects.page_size, total: objects.total, onChange: loadObjects }}
          emptyState={
            <EmptyState
              icon={<Boxes size={28} />}
              title="No objects configured"
              description="Add an object, or import a CSV/Excel/JSON/YAML file of thousands of objects at once."
              action={{ label: 'Add Object', onClick: openAddObject }}
            />
          }
        />
      </div>

      {recentRuns.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <div className="bkm-label" style={{ marginBottom: 8, fontSize: 13 }}>Recent Runs</div>
          <div className="bkm-list-table-panel">
            <table className="data-table" style={{ width: '100%' }}>
              <thead>
                <tr>
                  <th style={{ textAlign: 'left', padding: '8px 12px' }}>Run</th>
                  <th style={{ textAlign: 'left', padding: '8px 12px' }}>Status</th>
                  <th style={{ textAlign: 'left', padding: '8px 12px' }}>Objects</th>
                  <th style={{ textAlign: 'left', padding: '8px 12px' }}>Rows</th>
                  <th style={{ textAlign: 'left', padding: '8px 12px' }}>Started</th>
                </tr>
              </thead>
              <tbody>
                {recentRuns.map((r) => (
                  <tr key={r.id} className="row-clickable" onClick={() => navigate(`/app/bulk-migration/runs/${r.id}`)}>
                    <td style={{ padding: '8px 12px' }} className="bkm-mono">{r.run_key}</td>
                    <td style={{ padding: '8px 12px' }}><StatusBadge variant={RUN_STATUS_VARIANT[r.status] || 'gray'} dot>{RUN_STATUS_LABEL[r.status] || r.status}</StatusBadge></td>
                    <td style={{ padding: '8px 12px' }}>{r.success_objects}/{r.total_objects} ok{r.failed_objects ? `, ${r.failed_objects} failed` : ''}</td>
                    <td style={{ padding: '8px 12px' }}>{formatNumber(r.rows_processed)}</td>
                    <td style={{ padding: '8px 12px' }}>{formatDateTime(r.started_at || r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Add/Edit Object modal ─────────────────────────────── */}
      <Modal
        open={!!objModal} onClose={() => !savingObj && setObjModal(null)}
        title={objModal?.mode === 'create' ? 'Add Object' : 'Edit Object'} size="lg"
        footer={<><Button variant="secondary" onClick={() => setObjModal(null)} disabled={savingObj}>Cancel</Button><Button variant="primary" onClick={submitObjectForm} loading={savingObj}>Save</Button></>}
      >
        {objModal && (
          <form onSubmit={submitObjectForm}>
            {objFormError && <div className="bkm-error-banner">{objFormError}</div>}
            <div className="bkm-field-row">
              <div className="bkm-field">
                <label className="bkm-label">Source Schema</label>
                <select
                  className="bkm-input bkm-select-full" value={objModal.form.source_schema || ''}
                  onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, source_schema: e.target.value, source_object: '' } }))}
                >
                  <option value="">{loadingSrcSchemas ? 'Loading…' : 'Select schema…'}</option>
                  {srcSchemas.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div className="bkm-field">
                <label className="bkm-label">Source Object</label>
                <select
                  className="bkm-input bkm-select-full" value={objModal.form.source_object || ''}
                  disabled={!objModal.form.source_schema}
                  onChange={(e) => setObjModal((m) => ({
                    ...m,
                    form: {
                      ...m.form, source_object: e.target.value,
                      // Convenience default for a brand-new object: same
                      // name on the target side unless already typed.
                      target_object: m.mode === 'create' && !m.form.target_object ? e.target.value : m.form.target_object,
                    },
                  }))}
                >
                  <option value="">{loadingSrcObjects ? 'Loading…' : (objModal.form.source_schema ? 'Select object…' : 'Pick a schema first')}</option>
                  {srcObjects.map((o) => <option key={o.name} value={o.name}>{o.name}</option>)}
                </select>
              </div>
            </div>
            <div className="bkm-field-row">
              <div className="bkm-field">
                <label className="bkm-label">Target Schema</label>
                <select
                  className="bkm-input bkm-select-full" value={objModal.form.target_schema || ''}
                  onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, target_schema: e.target.value } }))}
                >
                  <option value="">{loadingTgtSchemas ? 'Loading…' : 'Select schema…'}</option>
                  {tgtSchemas.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div className="bkm-field">
                <label className="bkm-label">Target Object</label>
                <input
                  className="bkm-input bkm-input-full" value={objModal.form.target_object || ''}
                  list="bkm-target-objects" placeholder={loadingTgtObjects ? 'Loading existing tables…' : 'Existing table, or a new name to create'}
                  onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, target_object: e.target.value } }))}
                />
                <datalist id="bkm-target-objects">
                  {tgtObjects.map((o) => <option key={o.name} value={o.name} />)}
                </datalist>
                <span className="bkm-hint">Pick an existing table to load into, or type a new name — "Create target if not exists" below will create it.</span>
              </div>
            </div>
            <div className="bkm-field-row">
              <div className="bkm-field">
                <label className="bkm-label">Object Type</label>
                <select className="bkm-input bkm-select-full" value={objModal.form.object_type} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, object_type: e.target.value } }))}>
                  {OBJECT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              <div className="bkm-field">
                <label className="bkm-label">Load Type</label>
                <select className="bkm-input bkm-select-full" value={objModal.form.load_type} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, load_type: e.target.value } }))}>
                  {LOAD_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
                {['SCD1', 'SCD2', 'DELTA'].includes(objModal.form.load_type) && <span className="bkm-hint">Accepted at config time; the executor doesn't implement this load type yet (disclosed gap) — the task will fail with UNSUPPORTED_LOAD_TYPE if run.</span>}
              </div>
            </div>
            <div className="bkm-field"><label className="bkm-label">Primary Key (comma-separated, used for MERGE)</label><input className="bkm-input bkm-input-full" value={objModal.form.primary_key || ''} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, primary_key: e.target.value } }))} placeholder="id, tenant_id" /></div>
            <div className="bkm-field"><label className="bkm-label">Filter Condition (optional SQL WHERE)</label><input className="bkm-input bkm-input-full" value={objModal.form.filter_condition || ''} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, filter_condition: e.target.value } }))} placeholder="updated_at >= '2025-01-01'" /></div>
            <div className="bkm-field-row">
              <div className="bkm-field"><label className="bkm-label">Dependency Group</label><input className="bkm-input bkm-input-full" value={objModal.form.dependency_group || ''} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, dependency_group: e.target.value } }))} placeholder="e.g. dimensions" /></div>
              <div className="bkm-field"><label className="bkm-label">Dependency Order</label><input type="number" className="bkm-input bkm-input-full" value={objModal.form.dependency_order} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, dependency_order: e.target.value } }))} /></div>
              <div className="bkm-field"><label className="bkm-label">Priority</label><input type="number" className="bkm-input bkm-input-full" value={objModal.form.priority} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, priority: e.target.value } }))} /></div>
            </div>
            <div className="bkm-field"><label className="bkm-label">Depends On (comma-separated object keys, overrides group ordering)</label><input className="bkm-input bkm-input-full" value={objModal.form.depends_on || ''} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, depends_on: e.target.value } }))} /></div>
            <label className="bkm-checkbox-row"><input type="checkbox" checked={objModal.form.enabled} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, enabled: e.target.checked } }))} /> Enabled</label>
            <label className="bkm-checkbox-row"><input type="checkbox" checked={objModal.form.create_target_if_not_exists} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, create_target_if_not_exists: e.target.checked } }))} /> Create target table if it doesn't exist</label>
            <label className="bkm-checkbox-row"><input type="checkbox" checked={objModal.form.truncate_before_load} onChange={(e) => setObjModal((m) => ({ ...m, form: { ...m.form, truncate_before_load: e.target.checked } }))} /> Truncate target before load (FULL_REPLACE only)</label>
          </form>
        )}
      </Modal>

      {/* ── Edit pipeline config modal ───────────────────────── */}
      <Modal
        open={configModal} onClose={() => !savingConfig && setConfigModal(false)} title="Pipeline Configuration" size="md"
        footer={<><Button variant="secondary" onClick={() => setConfigModal(false)} disabled={savingConfig}>Cancel</Button><Button variant="primary" onClick={saveConfig} loading={savingConfig}>Save</Button></>}
      >
        {configForm && (
          <form onSubmit={saveConfig}>
            <div className="bkm-field"><label className="bkm-label">Name</label><input className="bkm-input bkm-input-full" value={configForm.name} onChange={(e) => setConfigForm((f) => ({ ...f, name: e.target.value }))} /></div>
            <div className="bkm-field"><label className="bkm-label">Description</label><input className="bkm-input bkm-input-full" value={configForm.description} onChange={(e) => setConfigForm((f) => ({ ...f, description: e.target.value }))} /></div>
            <div className="bkm-field-row">
              <div className="bkm-field"><label className="bkm-label">Source Connection</label><select className="bkm-input bkm-select-full" value={configForm.source_connection_id} onChange={(e) => setConfigForm((f) => ({ ...f, source_connection_id: e.target.value }))}>{connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select></div>
              <div className="bkm-field"><label className="bkm-label">Target Connection</label><select className="bkm-input bkm-select-full" value={configForm.target_connection_id} onChange={(e) => setConfigForm((f) => ({ ...f, target_connection_id: e.target.value }))}>{connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select></div>
            </div>
            <div className="bkm-field-row">
              <div className="bkm-field"><label className="bkm-label">Max Workers</label><input type="number" className="bkm-input bkm-input-full" value={configForm.max_workers} onChange={(e) => setConfigForm((f) => ({ ...f, max_workers: e.target.value }))} /></div>
              <div className="bkm-field"><label className="bkm-label">Batch Size</label><input type="number" className="bkm-input bkm-input-full" value={configForm.batch_size} onChange={(e) => setConfigForm((f) => ({ ...f, batch_size: e.target.value }))} /></div>
            </div>
            <div className="bkm-field-row">
              <div className="bkm-field"><label className="bkm-label">Max Retries</label><input type="number" className="bkm-input bkm-input-full" value={configForm.max_retries} onChange={(e) => setConfigForm((f) => ({ ...f, max_retries: e.target.value }))} /></div>
              <div className="bkm-field"><label className="bkm-label">Retry Backoff (s)</label><input type="number" className="bkm-input bkm-input-full" value={configForm.retry_backoff_seconds} onChange={(e) => setConfigForm((f) => ({ ...f, retry_backoff_seconds: e.target.value }))} /></div>
            </div>
            <label className="bkm-checkbox-row"><input type="checkbox" checked={configForm.continue_on_error} onChange={(e) => setConfigForm((f) => ({ ...f, continue_on_error: e.target.checked }))} /> Continue on error</label>
          </form>
        )}
      </Modal>

      {/* ── Import modal ─────────────────────────────────────── */}
      <Modal open={importModal} onClose={closeImport} title="Import Objects" size="md" footer={<><Button variant="secondary" onClick={closeImport}>Close</Button><Button variant="primary" onClick={submitImport} loading={importing} disabled={!importFile}>{importDryRun ? 'Preview' : 'Import'}</Button></>}>
        <div className="bkm-field">
          <label className="bkm-label">File (CSV, Excel, JSON or YAML)</label>
          <input type="file" accept=".csv,.xlsx,.xls,.json,.yaml,.yml" onChange={(e) => { setImportFile(e.target.files?.[0] || null); setImportResult(null); }} />
        </div>
        <label className="bkm-checkbox-row"><input type="checkbox" checked={importDryRun} onChange={(e) => { setImportDryRun(e.target.checked); setImportResult(null); }} /> Preview only (validate without writing) — uncheck to actually import</label>
        {importResult && (
          <div className="bkm-vresult">
            <div className="bkm-vresult-stats">
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{importResult.total}</div><div className="bkm-vresult-stat-label">Rows</div></div>
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{importResult.valid}</div><div className="bkm-vresult-stat-label">Valid</div></div>
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val danger">{importResult.invalid}</div><div className="bkm-vresult-stat-label">Invalid</div></div>
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{importResult.imported}</div><div className="bkm-vresult-stat-label">Imported</div></div>
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{importResult.updated}</div><div className="bkm-vresult-stat-label">Updated</div></div>
            </div>
            {importResult.unmapped_columns?.length > 0 && (
              <div className="bkm-error-banner" style={{ background: 'var(--color-warning-bg, #FFFBEB)', color: 'var(--color-warning-dark, #B45309)', borderColor: 'var(--color-warning, #F59E0B)' }}>
                Column{importResult.unmapped_columns.length > 1 ? 's' : ''} not recognized and ignored on every row: <strong>{importResult.unmapped_columns.join(', ')}</strong>.
                Check for a typo — e.g. use <code>source_schema</code>/<code>target_schema</code>, not <code>source_sch</code>/<code>target_sch</code>.
              </div>
            )}
            {importResult.errors.length > 0 && (
              <div className="bkm-vresult-list">
                {importResult.errors.map((e, i) => <div key={i} className="bkm-vresult-row"><AlertTriangle size={13} color="var(--color-danger)" /><span className="bkm-vresult-row-msg">{e}</span></div>)}
              </div>
            )}
          </div>
        )}
      </Modal>

      {/* ── Validate / Dry Run result modal ──────────────────── */}
      <Modal open={!!vResult} onClose={() => setVResult(null)} title={vResult?.kind === 'validate' ? 'Validation Result' : 'Dry Run Result'} size="lg" footer={<Button variant="primary" onClick={() => setVResult(null)}>Close</Button>}>
        {vResult?.kind === 'validate' && (
          <div className="bkm-vresult">
            <div className="bkm-vresult-stats">
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{vResult.data.total}</div><div className="bkm-vresult-stat-label">Objects</div></div>
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{vResult.data.valid}</div><div className="bkm-vresult-stat-label">Valid</div></div>
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val warning">{vResult.data.warnings}</div><div className="bkm-vresult-stat-label">Warnings</div></div>
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val danger">{vResult.data.errors}</div><div className="bkm-vresult-stat-label">Errors</div></div>
              <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{vResult.data.dependency_levels}</div><div className="bkm-vresult-stat-label">Dep. Levels</div></div>
            </div>
            {vResult.data.details.length > 0 ? (
              <div className="bkm-vresult-list">
                {vResult.data.details.map((d, i) => (
                  <div key={i} className="bkm-vresult-row">
                    {d.level === 'ERROR' ? <XCircle size={13} color="var(--color-danger)" /> : <AlertTriangle size={13} color="var(--color-warning-dark, #B45309)" />}
                    <span className="bkm-vresult-row-code">{d.code}</span>
                    {d.object_key && <span className="bkm-vresult-row-obj">{d.object_key}</span>}
                    <span className="bkm-vresult-row-msg">{d.message}</span>
                  </div>
                ))}
              </div>
            ) : <div className="bkm-vresult-list" style={{ padding: 16, textAlign: 'center', color: 'var(--text-muted)' }}>No issues found — ready to run.</div>}
          </div>
        )}
        {vResult?.kind === 'dry-run' && (
          <div className="bkm-vresult">
            {vResult.data.error ? (
              <div className="bkm-error-banner">{vResult.data.error.message || 'Dry run could not complete.'}</div>
            ) : (
              <>
                <div className="bkm-vresult-stats">
                  <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{vResult.data.object_count}</div><div className="bkm-vresult-stat-label">Objects</div></div>
                  <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{vResult.data.dependency_levels}</div><div className="bkm-vresult-stat-label">Dep. Levels</div></div>
                  <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{vResult.data.effective_workers}</div><div className="bkm-vresult-stat-label">Effective Workers</div></div>
                  <div className="bkm-vresult-stat"><div className="bkm-vresult-stat-val">{formatNumber(vResult.data.estimated_total_rows)}</div><div className="bkm-vresult-stat-label">Est. Rows</div></div>
                </div>
                <div className="bkm-vresult-list">
                  {vResult.data.actions.map((a, i) => (
                    <div key={i} className="bkm-vresult-row">
                      <span className="bkm-vresult-row-obj">{a.object_key}</span>
                      <span className="bkm-vresult-row-code">{a.load_type}</span>
                      <span className="bkm-vresult-row-msg">{a.target_action}</span>
                      {a.estimated_rows != null && <span style={{ marginLeft: 'auto', color: 'var(--text-muted)' }}>{formatNumber(a.estimated_rows)} rows</span>}
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
