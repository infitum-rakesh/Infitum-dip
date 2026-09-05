/**
 * ObjectColumnMapping — /app/mapping(/:mappingId)
 * Full-screen Object & Column Mapping editor (spec section 34). Real,
 * connection-backed: source/target schema comes from connectors/database
 * (Phase 2), Auto Map/Validate/Preview run the real engine in
 * backend/app/mapping/ (Phase 4) — nothing here is mocked.
 *
 * Auto Map only ever proposes; nothing is applied until the user accepts
 * a suggestion or hits Save Mapping. Preview executes the mapping's real
 * SQL (including any lookup joins) against the live source connection.
 */
import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Wand2, ShieldCheck, Save, Play, Columns3, RotateCcw, Search, Plus,
  Pencil, X, Check, Loader2, AlertTriangle, ArrowLeftRight, UploadCloud,
} from 'lucide-react';
import { Button, PermissionGate, FullPageSpinner, EmptyState, StatusBadge } from '@/components/common/index.js';
import { MAPPING_PERMISSIONS } from '@/utils/constants.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { mappingApi } from '@/services/api/mappingApi.js';
import { MappingRowEditor } from './MappingRowEditor.jsx';
import { MappingVisual } from './MappingVisual.jsx';
import { RunLoadPanel } from './RunLoadPanel.jsx';
import './mapping.css';

function emptyRef() { return { connection_id: null, schema_name: '', object_name: '' }; }

function apiErrorMessage(err, fallback) { return err?.message || fallback; }

const STATUS_VARIANT = { MAPPED: 'success', SUGGESTED: 'warning', CONFLICT: 'danger', UNMAPPED: 'gray' };

// ── Reusable Connection/Schema/Object picker ─────────────────────

function ObjectPicker({ label, value, onChange, disabled }) {
  const [connections, setConnections] = useState([]);
  const [schemas, setSchemas] = useState([]);
  const [objects, setObjects] = useState([]);
  const [search, setSearch] = useState('');
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [loadingObjects, setLoadingObjects] = useState(false);

  useEffect(() => { connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setConnections(r.items)).catch(() => {}); }, []);

  useEffect(() => {
    if (!value.connection_id) { setSchemas([]); return; }
    setLoadingSchemas(true);
    connectionApi.getSchemas(value.connection_id).then((r) => setSchemas(r.schemas)).catch(() => setSchemas([])).finally(() => setLoadingSchemas(false));
  }, [value.connection_id]);

  useEffect(() => {
    if (!value.connection_id || !value.schema_name) { setObjects([]); return; }
    setLoadingObjects(true);
    const t = setTimeout(() => {
      connectionApi.getObjects(value.connection_id, { schema: value.schema_name, q: search || undefined })
        .then((r) => setObjects(r.items)).catch(() => setObjects([])).finally(() => setLoadingObjects(false));
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value.connection_id, value.schema_name, search]);

  return (
    <div className="ocm-picker">
      <div className="ocm-picker-label">{label}</div>
      <div className="ocm-picker-row">
        <select className="ocm-select" disabled={disabled} value={value.connection_id ?? ''}
          onChange={(e) => onChange({ connection_id: e.target.value ? Number(e.target.value) : null, schema_name: '', object_name: '' })}>
          <option value="">Connection…</option>
          {connections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <select className="ocm-select" disabled={disabled || !value.connection_id} value={value.schema_name}
          onChange={(e) => onChange({ ...value, schema_name: e.target.value, object_name: '' })}>
          <option value="">{loadingSchemas ? 'Loading…' : 'Schema…'}</option>
          {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select className="ocm-select" disabled={disabled || !value.schema_name} value={value.object_name}
          onChange={(e) => onChange({ ...value, object_name: e.target.value })}>
          <option value="">{loadingObjects ? 'Loading…' : 'Object…'}</option>
          {objects.map((o) => <option key={o.name} value={o.name}>{o.name} ({o.object_type})</option>)}
        </select>
        {value.connection_id && value.schema_name && (
          <div className="ocm-picker-search">
            <Search size={12} />
            <input placeholder="Search objects…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
        )}
      </div>
    </div>
  );
}

export default function ObjectColumnMapping() {
  const { mappingId } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  // Pipeline Builder's "Create New Mapping" round-trip (CM5): a
  // column_mapping_step node opens this page in a new tab pre-populated
  // with its upstream Source Dataset / downstream Target node's object
  // refs, and listens for a postMessage once this mapping is published —
  // see ColumnMappingConfig.jsx and the `publishForPipelineNode` handler
  // below. `returnMode=pipelineNode` is the only signal this page needs;
  // it never talks to the pipeline/node ids directly.
  const isPipelineNodeReturn = searchParams.get('returnMode') === 'pipelineNode';

  const [id, setId] = useState(mappingId ? Number(mappingId) : null);
  const [name, setName] = useState('Untitled Mapping');
  const [source, setSource] = useState(() => {
    if (mappingId) return emptyRef();
    const connection_id = searchParams.get('sourceConnectionId');
    return connection_id
      ? { connection_id: Number(connection_id), schema_name: searchParams.get('sourceSchema') || '', object_name: searchParams.get('sourceObject') || '' }
      : emptyRef();
  });
  const [target, setTarget] = useState(() => {
    if (mappingId) return emptyRef();
    const connection_id = searchParams.get('targetConnectionId');
    return connection_id
      ? { connection_id: Number(connection_id), schema_name: searchParams.get('targetSchema') || '', object_name: searchParams.get('targetObject') || '' }
      : emptyRef();
  });
  const [sourceColumns, setSourceColumns] = useState([]);
  const [targetColumns, setTargetColumns] = useState([]);
  const [mappings, setMappings] = useState([]);
  const [savedSnapshot, setSavedSnapshot] = useState(null);
  const [versionInfo, setVersionInfo] = useState(null); // { latest_version, current_published_version, status }
  const [publishing, setPublishing] = useState(false);
  const [publishNotice, setPublishNotice] = useState('');
  const [usage, setUsage] = useState(null); // CM11 — { items, total } from GET /mappings/{id}/usage

  const [loading, setLoading] = useState(!!mappingId);
  const [loadError, setLoadError] = useState('');

  const [autoMapping, setAutoMapping] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validation, setValidation] = useState(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewData, setPreviewData] = useState(null);
  const [previewError, setPreviewError] = useState('');
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState('');
  const [saveNotice, setSaveNotice] = useState('');

  const [viewMode, setViewMode] = useState('grid');
  const [editingRow, setEditingRow] = useState(undefined); // undefined = closed, null = new, row = edit
  const [gridSearch, setGridSearch] = useState('');
  const [highlightRowId, setHighlightRowId] = useState(null);

  const locked = !!mappingId; // an already-saved mapping's source/target objects are fixed — re-map by creating a new one

  // ── Load an existing mapping ──────────────────────────────────
  useEffect(() => {
    if (!mappingId) return;
    setLoading(true);
    mappingApi.get(mappingId).then((d) => {
      setId(d.id);
      setName(d.name);
      setSource({ connection_id: d.source_connection_id, schema_name: d.source_schema || '', object_name: d.source_object || '' });
      setTarget({ connection_id: d.target_connection_id, schema_name: d.target_schema || '', object_name: d.target_object || '' });
      setMappings(d.mappings);
      setSourceColumns(d.source_columns);
      setTargetColumns(d.target_columns);
      setSavedSnapshot(JSON.stringify({ name: d.name, mappings: d.mappings }));
      setVersionInfo({ latest_version: d.latest_version, current_published_version: d.current_published_version, status: d.status });
    }).catch((err) => setLoadError(apiErrorMessage(err, 'Could not load this mapping.'))).finally(() => setLoading(false));
  }, [mappingId]);

  // CM11 — usage warning: purely informational, never blocks Save/Publish.
  // A pipeline attached to an older version keeps running that exact
  // version untouched regardless of what happens here — see
  // ColumnMappingConfig.jsx's "Upgrade Mapping" button for the only
  // action that actually moves a pipeline node onto a newer version.
  useEffect(() => {
    if (!mappingId) return;
    mappingApi.getUsage(mappingId).then(setUsage).catch(() => setUsage(null));
  }, [mappingId]);

  const sourceReady = !!(source.connection_id && source.object_name);
  const targetReady = !!(target.connection_id && target.object_name);

  // ── Live column fetch for a brand-new (unsaved) mapping ───────
  useEffect(() => {
    if (mappingId || !sourceReady) return;
    connectionApi.getColumns(source.connection_id, source.object_name, { schema: source.schema_name || undefined })
      .then((r) => setSourceColumns(r.columns)).catch(() => setSourceColumns([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source.connection_id, source.schema_name, source.object_name, mappingId]);

  useEffect(() => {
    if (mappingId || !targetReady) return;
    connectionApi.getColumns(target.connection_id, target.object_name, { schema: target.schema_name || undefined })
      .then((r) => setTargetColumns(r.columns)).catch(() => setTargetColumns([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target.connection_id, target.schema_name, target.object_name, mappingId]);

  const bothReady = sourceReady && targetReady;
  const dirty = savedSnapshot !== null
    ? JSON.stringify({ name, mappings }) !== savedSnapshot
    : mappings.length > 0;

  // ── Actions ────────────────────────────────────────────────────

  const runAutoMap = async () => {
    setAutoMapping(true); setActionError('');
    try {
      const r = await mappingApi.autoMap({ source, target, existing_mappings: mappings });
      if (r.proposals.length === 0) {
        setActionError('No new suggestions — every plausible column is already mapped or nothing matched confidently enough.');
      }
      setMappings((prev) => [...prev, ...r.proposals]);
      setValidation(null);
    } catch (err) { setActionError(apiErrorMessage(err, 'Auto Map failed.')); }
    finally { setAutoMapping(false); }
  };

  const runValidate = async () => {
    setValidating(true); setActionError('');
    try {
      const r = await mappingApi.validate({ source, target, mappings });
      setValidation(r);
    } catch (err) { setActionError(apiErrorMessage(err, 'Validation failed.')); }
    finally { setValidating(false); }
  };

  const runPreview = async () => {
    setPreviewing(true); setPreviewError(''); setPreviewData(null);
    try {
      const r = await mappingApi.preview({ source, mappings, limit: 50 });
      setPreviewData(r);
    } catch (err) { setPreviewError(apiErrorMessage(err, 'Preview failed.')); }
    finally { setPreviewing(false); }
  };

  const runSave = async () => {
    setSaving(true); setActionError(''); setSaveNotice('');
    try {
      if (id) {
        // Editing an existing mapping's rows never overwrites in place —
        // it creates a new immutable version (mirrors Pipeline Builder's
        // own Save-always-versions behavior). The header's `name` is a
        // separate, lightweight edit, only sent when it actually changed.
        const prevName = savedSnapshot ? JSON.parse(savedSnapshot).name : name;
        if (name !== prevName) await mappingApi.update(id, { name });

        const v = await mappingApi.createVersion(id, { mappings, change_description: 'Edited via Column Mapping editor.' });
        setSavedSnapshot(JSON.stringify({ name, mappings: v.mappings }));
        setValidation(null);
        setVersionInfo((prev) => ({ ...prev, latest_version: v.version_number, status: v.status }));
        setSaveNotice(`Saved as version ${v.version_number} (${v.status}) — Validate, then Publish to make it available to production pipelines.`);
      } else {
        const r = await mappingApi.create({ name, source, target, mappings });
        setId(r.id);
        setSavedSnapshot(JSON.stringify({ name: r.name, mappings: r.mappings }));
        setVersionInfo({ latest_version: r.latest_version, current_published_version: r.current_published_version, status: r.status });
        navigate(`/app/mapping/${r.id}${window.location.search}`, { replace: true });
      }
    } catch (err) { setActionError(apiErrorMessage(err, 'Could not save mapping.')); }
    finally { setSaving(false); }
  };

  // ── Version lifecycle: Validate then Publish the latest saved version ──

  const runValidateAndPublish = async () => {
    if (!id || !versionInfo) return;
    const versionNumber = versionInfo.latest_version;
    setPublishing(true); setActionError(''); setPublishNotice('');
    try {
      const result = await mappingApi.validateVersion(id, versionNumber);
      setValidation(result);
      if (result.overall_status === 'CONFLICT') {
        setActionError(`Version ${versionNumber} has blocking errors — fix them before it can be published.`);
        setVersionInfo((prev) => ({ ...prev, status: 'DRAFT' }));
        return;
      }
      const published = await mappingApi.publishVersion(id, versionNumber, {});
      setVersionInfo({ latest_version: published.version_number, current_published_version: published.version_number, status: published.status });
      setPublishNotice(`Version ${published.version_number} is now PUBLISHED — available to production pipelines.`);

      if (isPipelineNodeReturn && window.opener) {
        window.opener.postMessage(
          { type: 'column-mapping-published', mappingId: id, mappingVersion: published.version_number, mappingName: name },
          window.location.origin,
        );
        setPublishNotice(`Version ${published.version_number} is now PUBLISHED. The Pipeline Builder tab has been notified — you can close this tab and return there.`);
      }
    } catch (err) { setActionError(apiErrorMessage(err, 'Validate/Publish failed.')); }
    finally { setPublishing(false); }
  };

  const runReset = () => {
    if (!window.confirm('Discard unsaved mapping changes?')) return;
    if (savedSnapshot) {
      const snap = JSON.parse(savedSnapshot);
      setName(snap.name); setMappings(snap.mappings);
    } else {
      setMappings([]);
    }
    setValidation(null); setPreviewData(null); setActionError('');
  };

  // ── Row operations ────────────────────────────────────────────

  const upsertRow = (row) => {
    setMappings((prev) => {
      const idx = prev.findIndex((m) => m.id === row.id);
      if (idx === -1) return [...prev, row];
      const next = [...prev]; next[idx] = row; return next;
    });
    setEditingRow(undefined);
    setValidation(null);
  };
  const removeRow = (rowId) => { setMappings((prev) => prev.filter((m) => m.id !== rowId)); setValidation(null); };
  const acceptSuggestion = (rowId) => { setMappings((prev) => prev.map((m) => (m.id === rowId ? { ...m, status: 'MAPPED' } : m))); setValidation(null); };
  const acceptAllSuggested = () => { setMappings((prev) => prev.map((m) => (m.status === 'SUGGESTED' ? { ...m, status: 'MAPPED' } : m))); setValidation(null); };
  const rejectAllSuggested = () => { setMappings((prev) => prev.filter((m) => m.status !== 'SUGGESTED')); setValidation(null); };

  const mappedTargetNames = useMemo(() => new Set(mappings.map((m) => m.target_column)), [mappings]);
  const mappedSourceNames = useMemo(() => new Set(mappings.flatMap((m) => m.source_columns || [])), [mappings]);
  const unmappedTargets = targetColumns.filter((c) => !mappedTargetNames.has(c.name));
  const unmappedSources = sourceColumns.filter((c) => !mappedSourceNames.has(c.name));
  const suggestedCount = mappings.filter((m) => m.status === 'SUGGESTED').length;

  const filteredMappings = gridSearch
    ? mappings.filter((m) => `${(m.source_columns || []).join(',')} ${m.target_column}`.toLowerCase().includes(gridSearch.toLowerCase()))
    : mappings;

  const columnTypeOf = (list, name) => list.find((c) => c.name === name)?.data_type ?? '';

  if (loading) return <FullPageSpinner />;
  if (loadError) {
    return (
      <div className="ocm-page">
        <EmptyState icon={<ArrowLeftRight size={28} />} title="Mapping not found" description={loadError}
          action={{ label: 'Back to Column Mapping', onClick: () => navigate('/app/mapping') }} />
      </div>
    );
  }

  return (
    <PermissionGate {...MAPPING_PERMISSIONS.MAPPING_VIEW} showReason>
      <div className="ocm-page">
        {/* Header */}
        <div className="ocm-header">
          <div>
            <input className="ocm-name-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Mapping name…" />
            <p className="ocm-subtitle">Object & Column Mapping{id ? ` · saved` : ' · unsaved'}{dirty ? ' · unsaved changes' : ''}</p>
          </div>
        </div>

        {/* Source / Target pickers */}
        <div className="ocm-pickers-row">
          <ObjectPicker label="SOURCE" value={source} onChange={setSource} disabled={locked} />
          <ArrowLeftRight size={18} className="ocm-picker-arrow" />
          <ObjectPicker label="TARGET" value={target} onChange={setTarget} disabled={locked} />
        </div>

        {/* Action bar */}
        <div className="ocm-action-bar">
          <PermissionGate {...MAPPING_PERMISSIONS.MAPPING_RUN} fallback={null}>
            <Button variant="secondary" size="sm" icon={autoMapping ? <Loader2 size={13} className="sdc-spin" /> : <Wand2 size={13} />} disabled={!bothReady || autoMapping} onClick={runAutoMap}>
              Auto Map
            </Button>
            <Button variant="secondary" size="sm" icon={validating ? <Loader2 size={13} className="sdc-spin" /> : <ShieldCheck size={13} />} disabled={!bothReady || mappings.length === 0 || validating} onClick={runValidate}>
              Validate Mapping
            </Button>
            <Button variant="secondary" size="sm" icon={previewing ? <Loader2 size={13} className="sdc-spin" /> : <Play size={13} />} disabled={!sourceReady || mappings.length === 0 || previewing} onClick={runPreview}>
              Preview
            </Button>
          </PermissionGate>
          <button type="button" className={`ocm-view-toggle ${viewMode === 'grid' ? 'active' : ''}`} onClick={() => setViewMode('grid')}>Grid</button>
          <button type="button" className={`ocm-view-toggle ${viewMode === 'visual' ? 'active' : ''}`} onClick={() => setViewMode('visual')}>Visual</button>
          <button type="button" className={`ocm-view-toggle ${viewMode === 'compare' ? 'active' : ''}`} onClick={() => setViewMode('compare')}>Compare Schema</button>
          <div className="ocm-action-bar-spacer" />
          <Button variant="secondary" size="sm" icon={<RotateCcw size={13} />} disabled={!dirty} onClick={runReset}>Reset</Button>
          <PermissionGate {...MAPPING_PERMISSIONS.MAPPING_EDIT} fallback={null}>
            <Button variant="primary" size="sm" icon={<Save size={13} />} loading={saving} disabled={!bothReady || !dirty} onClick={runSave}>Save Mapping</Button>
          </PermissionGate>
        </div>

        {/* Version & Publish — only PUBLISHED versions can be attached to a
            production pipeline (spec section 13). Editing/Save always
            creates a new DRAFT version (see runSave above); this is the
            explicit action that advances it to VALIDATED then PUBLISHED. */}
        {id && versionInfo && (
          <div className="ocm-action-bar" style={{ marginBottom: 4 }}>
            <span className="ocm-muted" style={{ fontSize: 12.5 }}>
              Version {versionInfo.latest_version} — <strong>{versionInfo.status}</strong>
              {versionInfo.current_published_version != null && versionInfo.current_published_version !== versionInfo.latest_version && (
                <> (v{versionInfo.current_published_version} is the currently published version)</>
              )}
            </span>
            <div className="ocm-action-bar-spacer" />
            <PermissionGate {...MAPPING_PERMISSIONS.MAPPING_EDIT} fallback={null}>
              <Button
                variant="primary" size="sm" icon={publishing ? <Loader2 size={13} className="sdc-spin" /> : <UploadCloud size={13} />}
                disabled={dirty || publishing || versionInfo.status === 'PUBLISHED'}
                onClick={runValidateAndPublish}
              >
                {versionInfo.status === 'PUBLISHED' ? 'Published' : `Validate & Publish v${versionInfo.latest_version}`}
              </Button>
            </PermissionGate>
          </div>
        )}
        {isPipelineNodeReturn && (
          <div className="ocm-banner info">
            Opened from a Pipeline Builder Column Mapping node — publish this mapping to attach it back to that pipeline.
          </div>
        )}
        {usage && usage.total > 0 && (
          <div className="ocm-banner info">
            Used by {usage.total} pipeline{usage.total === 1 ? '' : 's'} —{' '}
            {usage.items.map((u, i) => (
              <span key={u.id}>
                {i > 0 && ', '}
                <a href={`/app/transform?pipelineId=${u.pipeline_id}`} target="_blank" rel="noreferrer">
                  {u.pipeline_name || `Pipeline #${u.pipeline_id}`} (v{u.mapping_version})
                </a>
              </span>
            ))}
            . Editing here never affects those pipelines until someone explicitly clicks Upgrade Mapping on that node.
          </div>
        )}

        {actionError && <div className="ocm-banner error">{actionError}</div>}
        {saveNotice && <div className="ocm-banner success">{saveNotice}</div>}
        {publishNotice && <div className="ocm-banner success">{publishNotice}</div>}
        {!bothReady && <div className="ocm-banner info">Select a source and target object above to start mapping.</div>}

        {validation && (
          <div className={`ocm-banner ${validation.overall_status === 'CONFLICT' ? 'error' : validation.overall_status === 'WARNING' ? 'warning' : 'success'}`}>
            <strong>{validation.overall_status}</strong> — {validation.issues.filter((i) => i.severity !== 'INFO').length} issue(s)
            {validation.issues.length > 0 && (
              <ul className="ocm-issue-list">
                {validation.issues.map((iss, i) => (
                  <li key={i} className={`ocm-issue-${iss.severity.toLowerCase()}`}>{iss.severity}: {iss.message}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {bothReady && viewMode === 'grid' && (
          <>
            {suggestedCount > 0 && (
              <div className="ocm-banner warning">
                {suggestedCount} suggested mapping{suggestedCount === 1 ? '' : 's'} need approval.
                <span className="ocm-bulk-actions">
                  <button type="button" onClick={acceptAllSuggested}><Check size={12} /> Accept all</button>
                  <button type="button" onClick={rejectAllSuggested}><X size={12} /> Reject all</button>
                </span>
              </div>
            )}

            <div className="ocm-grid-toolbar">
              <div className="ocm-search-box">
                <Search size={12} />
                <input placeholder="Search mappings…" value={gridSearch} onChange={(e) => setGridSearch(e.target.value)} />
              </div>
              <PermissionGate {...MAPPING_PERMISSIONS.MAPPING_EDIT} fallback={null}>
                <Button variant="secondary" size="sm" icon={<Plus size={13} />} onClick={() => setEditingRow(null)}>Add Mapping</Button>
              </PermissionGate>
            </div>

            <div className="ocm-grid-wrap">
              <table className="ocm-grid">
                <thead>
                  <tr>
                    <th>Source Column</th><th>Source Type</th><th>Transformation</th>
                    <th>Target Column</th><th>Target Type</th><th>Status</th><th>Confidence</th><th>Validation</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {filteredMappings.map((m) => {
                    const rowValidation = validation?.row_status?.[m.id];
                    return (
                      <tr key={m.id} id={`ocm-row-${m.id}`} className={highlightRowId === m.id ? 'highlighted' : ''}>
                        <td className="ocm-mono">{m.lookup ? `${m.source_columns[0]} → JOIN` : (m.source_columns || []).join(', ') || (m.constant_value != null ? <em>constant</em> : '—')}</td>
                        <td className="ocm-mono ocm-muted">{m.source_columns?.[0] ? columnTypeOf(sourceColumns, m.source_columns[0]) : '—'}</td>
                        <td className="ocm-mono ocm-transformation">
                          {m.lookup ? `LOOKUP ${m.lookup.table}.${m.lookup.return_column}` : (m.transformation || (m.constant_value != null ? `= '${m.constant_value}'` : '—'))}
                        </td>
                        <td className="ocm-mono">{m.target_column}</td>
                        <td className="ocm-mono ocm-muted">{columnTypeOf(targetColumns, m.target_column)}</td>
                        <td><StatusBadge variant={STATUS_VARIANT[m.status] ?? 'gray'} dot size="sm">{m.status}</StatusBadge></td>
                        <td>{m.confidence != null ? `${Math.round(m.confidence)}%` : '—'}</td>
                        <td>
                          {rowValidation === 'CONFLICT' && <span className="ocm-val-badge error"><AlertTriangle size={11} /> Conflict</span>}
                          {rowValidation === 'WARNING' && <span className="ocm-val-badge warning"><AlertTriangle size={11} /> Warning</span>}
                          {rowValidation === 'OK' && <span className="ocm-val-badge ok"><Check size={11} /> OK</span>}
                          {!rowValidation && <span className="ocm-muted">Not validated</span>}
                        </td>
                        <td className="ocm-row-actions">
                          {m.status === 'SUGGESTED' && (
                            <button className="ocm-icon-btn" title="Accept" onClick={() => acceptSuggestion(m.id)}><Check size={13} /></button>
                          )}
                          <button className="ocm-icon-btn" title="Edit" onClick={() => setEditingRow(m)}><Pencil size={13} /></button>
                          <button className="ocm-icon-btn danger" title="Unmap" onClick={() => removeRow(m.id)}><X size={13} /></button>
                        </td>
                      </tr>
                    );
                  })}
                  {filteredMappings.length === 0 && (
                    <tr><td colSpan={9} className="ocm-empty-row">No mappings yet — try Auto Map or Add Mapping.</td></tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="ocm-unmapped-cols">
              <div className="ocm-unmapped-list">
                <div className="ocm-unmapped-title">Unmapped Source Columns ({unmappedSources.length})</div>
                {unmappedSources.map((c) => <span key={c.name} className="ocm-unmapped-pill">{c.name}</span>)}
              </div>
              <div className="ocm-unmapped-list">
                <div className="ocm-unmapped-title">Unmapped Target Columns ({unmappedTargets.length})</div>
                {unmappedTargets.map((c) => (
                  <button key={c.name} className="ocm-unmapped-pill clickable" onClick={() => setEditingRow({ target_column: c.name, source_columns: [], id: null })}>
                    <Plus size={10} /> {c.name}
                  </button>
                ))}
              </div>
            </div>
          </>
        )}

        {bothReady && viewMode === 'visual' && (
          <MappingVisual
            sourceColumns={sourceColumns} targetColumns={targetColumns} mappings={mappings}
            onSelectRow={(rowId) => { setViewMode('grid'); setHighlightRowId(rowId); setTimeout(() => document.getElementById(`ocm-row-${rowId}`)?.scrollIntoView({ block: 'center' }), 50); }}
          />
        )}

        {bothReady && viewMode === 'compare' && (
          <div className="ocm-grid-wrap">
            <table className="ocm-grid">
              <thead><tr><th>Source</th><th>Source Type</th><th></th><th>Target</th><th>Target Type</th><th>Status</th></tr></thead>
              <tbody>
                {mappings.map((m) => {
                  const rowValidation = validation?.row_status?.[m.id] ?? 'Not validated';
                  return (
                    <tr key={m.id}>
                      <td className="ocm-mono">{(m.source_columns || []).join(', ') || '—'}</td>
                      <td className="ocm-mono ocm-muted">{m.source_columns?.[0] ? columnTypeOf(sourceColumns, m.source_columns[0]) : '—'}</td>
                      <td className="ocm-mono">→</td>
                      <td className="ocm-mono">{m.target_column}</td>
                      <td className="ocm-mono ocm-muted">{columnTypeOf(targetColumns, m.target_column)}</td>
                      <td>
                        {rowValidation === 'CONFLICT' && <span className="ocm-val-badge error">Conflict</span>}
                        {rowValidation === 'WARNING' && <span className="ocm-val-badge warning">Warning</span>}
                        {rowValidation === 'OK' && <span className="ocm-val-badge ok">Compatible</span>}
                        {rowValidation === 'Not validated' && <span className="ocm-muted">Run Validate Mapping</span>}
                      </td>
                    </tr>
                  );
                })}
                {unmappedTargets.map((c) => (
                  <tr key={`missing-${c.name}`}>
                    <td className="ocm-mono ocm-muted">—</td><td className="ocm-mono ocm-muted">—</td><td>→</td>
                    <td className="ocm-mono">{c.name}</td><td className="ocm-mono ocm-muted">{c.data_type}</td>
                    <td><span className="ocm-val-badge missing">Missing</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Run Load — real backend bulk load, separate from Preview (which
            only ever samples via a bounded SELECT, never writes anywhere) */}
        <PermissionGate {...MAPPING_PERMISSIONS.MAPPING_RUN} fallback={null}>
          <RunLoadPanel mappingId={id} mappingDirty={dirty} targetColumns={targetColumns} mappedTargetNames={mappedTargetNames} />
        </PermissionGate>

        {(previewing || previewData || previewError) && (
          <div className="ocm-preview-panel">
            <div className="ocm-preview-title"><Columns3 size={14} /> Preview{previewData ? ` — ${previewData.rows.length} rows` : ''}</div>
            {previewing && <p className="ocm-muted">Running…</p>}
            {previewError && <div className="ocm-banner error">{previewError}</div>}
            {previewData && (
              <div className="ocm-grid-wrap">
                <table className="ocm-grid">
                  <thead><tr>{previewData.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                  <tbody>
                    {previewData.rows.map((row, i) => (
                      <tr key={i}>{row.map((v, j) => <td key={j} className="ocm-mono">{v === null ? <em className="ocm-muted">null</em> : String(v)}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {editingRow !== undefined && (
          <MappingRowEditor
            open={editingRow !== undefined}
            onClose={() => setEditingRow(undefined)}
            onSave={upsertRow}
            row={editingRow}
            sourceColumns={sourceColumns}
            targetColumns={targetColumns}
            mappedTargetNames={mappedTargetNames}
          />
        )}
      </div>
    </PermissionGate>
  );
}
