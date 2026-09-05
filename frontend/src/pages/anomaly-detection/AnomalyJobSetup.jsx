/**
 * AnomalyJobSetup — the create/edit wizard body for one AnomalyJob:
 * connection/file object picker (mirrors ReconciliationRules.jsx's
 * ObjectPicker), a real column table with select-all/by-type + identifier
 * column pick, an on-demand profiling display (anomalyApi.previewProfile —
 * the real two-tier profiler, no execution row created), detection method
 * selection, baseline source config, and on-critical/retention options.
 * Props come from the parent AnomalyJobEditor.jsx page orchestrator.
 */
import { useEffect, useMemo, useState } from 'react';
import { Loader2, Trash2, Upload, Search } from 'lucide-react';
import { connectionApi } from '@/services/api/connectionApi.js';
import { uploadedFileApi } from '@/services/api/uploadedFileApi.js';
import { anomalyApi, DETECTION_METHODS, BASELINE_SOURCES } from '@/services/api/anomalyApi.js';
import './anomaly-detection.css';

// 0 is the real "not a real Connection — see uploaded_file_id instead"
// sentinel this whole platform shares (Reconciliation/Validation/Anomaly).
const FILE_BACKED_SENTINEL = 0;

function ObjectPicker({ value, onChange, disabled }) {
  const [connections, setConnections] = useState([]);
  const [schemas, setSchemas] = useState([]);
  const [objects, setObjects] = useState([]);
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [loadingObjects, setLoadingObjects] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');

  const mode = value.uploaded_file_id ? 'file' : (value.mode || 'connection');

  useEffect(() => { connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setConnections(r.items)).catch(() => {}); }, []);

  useEffect(() => {
    if (mode !== 'connection' || !value.connection_id) { setSchemas([]); return; }
    setLoadingSchemas(true);
    connectionApi.getSchemas(value.connection_id).then((r) => {
      setSchemas(r.schemas);
      if (r.schemas.length === 1 && !value.schema_name) onChange({ ...value, schema_name: r.schemas[0] });
    }).catch(() => setSchemas([])).finally(() => setLoadingSchemas(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, value.connection_id]);

  useEffect(() => {
    if (mode !== 'connection' || !value.connection_id || !value.schema_name) { setObjects([]); return; }
    setLoadingObjects(true);
    connectionApi.getObjects(value.connection_id, { schema: value.schema_name })
      .then((r) => setObjects(r.items)).catch(() => setObjects([])).finally(() => setLoadingObjects(false));
  }, [mode, value.connection_id, value.schema_name]);

  const switchMode = (nextMode) => {
    if (disabled || nextMode === mode) return;
    onChange({ connection_id: null, schema_name: '', object_name: '', uploaded_file_id: null, mode: nextMode });
  };

  const handleUpload = async (file) => {
    if (!file) return;
    setUploading(true); setUploadError('');
    try {
      const record = await uploadedFileApi.upload(file);
      onChange({ connection_id: FILE_BACKED_SENTINEL, schema_name: '', object_name: record.filename, uploaded_file_id: record.id, mode: 'file' });
    } catch (err) {
      setUploadError(err?.message || 'Upload failed.');
    } finally {
      setUploading(false);
    }
  };

  const clearUpload = () => onChange({ connection_id: null, schema_name: '', object_name: '', uploaded_file_id: null, mode: 'file' });

  return (
    <div className="anm-ds-card">
      <div className="anm-mode-toggle" role="tablist">
        <button type="button" role="tab" aria-selected={mode === 'connection'}
          className={`anm-mode-btn ${mode === 'connection' ? 'active' : ''}`}
          disabled={disabled} onClick={() => switchMode('connection')}>
          Connection
        </button>
        <button type="button" role="tab" aria-selected={mode === 'file'}
          className={`anm-mode-btn ${mode === 'file' ? 'active' : ''}`}
          disabled={disabled} onClick={() => switchMode('file')}>
          Upload File
        </button>
      </div>

      {mode === 'connection' ? (
        <>
          <div className="anm-field">
            <label className="anm-label">Connection</label>
            <select className="anm-select anm-select-full" disabled={disabled} value={value.connection_id ?? ''}
              onChange={(e) => onChange({ connection_id: e.target.value ? Number(e.target.value) : null, schema_name: '', object_name: '', uploaded_file_id: null, mode: 'connection' })}>
              <option value="">Select connection…</option>
              {connections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
            </select>
          </div>
          <div className="anm-field-row">
            <div className="anm-field">
              <label className="anm-label">Schema</label>
              <select className="anm-select anm-select-full" disabled={disabled || !value.connection_id} value={value.schema_name}
                onChange={(e) => onChange({ ...value, schema_name: e.target.value, object_name: '' })}>
                <option value="">{loadingSchemas ? 'Loading…' : 'Select schema…'}</option>
                {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div className="anm-field">
              <label className="anm-label">Table / View</label>
              <select className="anm-select anm-select-full" disabled={disabled || !value.schema_name} value={value.object_name}
                onChange={(e) => onChange({ ...value, object_name: e.target.value })}>
                <option value="">{loadingObjects ? 'Loading…' : 'Select object…'}</option>
                {objects.map((o) => <option key={o.name} value={o.name}>{o.name}</option>)}
              </select>
            </div>
          </div>
        </>
      ) : (
        <div className="anm-field">
          <label className="anm-label">File</label>
          {value.uploaded_file_id ? (
            <div className="anm-uploaded-file-card">
              <span className="anm-uploaded-file-name">{value.object_name}</span>
              {!disabled && (
                <button type="button" className="anm-icon-btn" title="Remove file" onClick={clearUpload}>
                  <Trash2 size={12} />
                </button>
              )}
            </div>
          ) : (
            <>
              <label className="anm-upload-btn" style={{ cursor: disabled || uploading ? 'default' : 'pointer' }}>
                {uploading ? <Loader2 size={12} className="anm-spin" /> : <Upload size={12} />}
                {uploading ? 'Uploading…' : 'Upload File'}
                <input
                  type="file" style={{ display: 'none' }} disabled={disabled || uploading}
                  accept=".csv,.tsv,.txt,.dat,.xlsx,.jsonl,.ndjson"
                  onChange={(e) => { handleUpload(e.target.files?.[0]); e.target.value = ''; }}
                />
              </label>
              <span className="anm-setup-section-hint">CSV, TSV, TXT, Excel (.xlsx) or JSON Lines. Columns are detected automatically.</span>
            </>
          )}
          {uploadError && <span className="anm-upload-error">{uploadError}</span>}
        </div>
      )}
    </div>
  );
}

function typeBadge(dataType) {
  const t = (dataType || '').toUpperCase();
  if (/(INT|NUMERIC|DECIMAL|FLOAT|DOUBLE|REAL|SERIAL)/.test(t)) return 'Numeric';
  if (/(DATE|TIME)/.test(t)) return 'Date/Time';
  if (/(BOOL)/.test(t)) return 'Boolean';
  return 'String';
}

/** Real column table — select-all / select-by-type checkboxes, plus a
 * radio to designate one column as the optional record identifier
 * (detection_config.id_column — used to label anomalous records with
 * something more useful than a raw row position). */
function ColumnTable({ allColumns, selectedColumns, onChangeSelected, idColumn, onChangeIdColumn, disabled, emptyLabel }) {
  const [search, setSearch] = useState('');
  const selectedNames = new Set(selectedColumns.map((c) => c.name));

  const filtered = useMemo(
    () => allColumns.filter((c) => c.name.toLowerCase().includes(search.toLowerCase())),
    [allColumns, search],
  );
  const byType = useMemo(() => {
    const groups = {};
    for (const c of allColumns) {
      const t = typeBadge(c.data_type);
      (groups[t] ||= []).push(c);
    }
    return groups;
  }, [allColumns]);

  const toggle = (col) => {
    if (selectedNames.has(col.name)) {
      onChangeSelected(selectedColumns.filter((c) => c.name !== col.name));
      if (idColumn === col.name) onChangeIdColumn('');
    } else {
      onChangeSelected([...selectedColumns, { name: col.name, data_type: col.data_type || '' }]);
    }
  };
  const selectAll = () => onChangeSelected(allColumns.map((c) => ({ name: c.name, data_type: c.data_type || '' })));
  const clearAll = () => { onChangeSelected([]); onChangeIdColumn(''); };
  const selectByType = (type) => {
    const names = new Set(selectedColumns.map((c) => c.name));
    (byType[type] || []).forEach((c) => names.add(c.name));
    onChangeSelected(allColumns.filter((c) => names.has(c.name)).map((c) => ({ name: c.name, data_type: c.data_type || '' })));
  };

  if (disabled) {
    return <div className="anm-col-table anm-col-table-empty">{emptyLabel}</div>;
  }

  return (
    <div>
      <div className="anm-col-toolbar">
        <button type="button" className="anm-col-toolbar-btn" onClick={selectAll}>Select all</button>
        <button type="button" className="anm-col-toolbar-btn" onClick={clearAll}>Clear all</button>
        {Object.keys(byType).map((t) => (
          <button key={t} type="button" className="anm-col-toolbar-btn" onClick={() => selectByType(t)}>+ All {t}</button>
        ))}
        <div style={{ marginLeft: 'auto', position: 'relative' }}>
          <Search size={12} style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-faint)' }} />
          <input
            className="anm-input" style={{ paddingLeft: 24, fontSize: 12, width: 160 }}
            placeholder="Search columns…" value={search} onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>
      <div className="anm-col-table">
        <div className="anm-col-table-head">
          <span></span><span>Column</span><span>Type</span><span>Identifier</span>
        </div>
        {filtered.map((c) => (
          <label key={c.name} className="anm-col-row">
            <input type="checkbox" checked={selectedNames.has(c.name)} onChange={() => toggle(c)} />
            <span className="anm-col-name">{c.name}</span>
            <span className="anm-col-type-badge">{typeBadge(c.data_type)}</span>
            <input
              type="radio" name="anm-id-column" className="anm-col-id-radio"
              checked={idColumn === c.name} disabled={!selectedNames.has(c.name)}
              onChange={() => onChangeIdColumn(c.name)}
            />
          </label>
        ))}
      </div>
    </div>
  );
}

/** On-demand profiling — calls the real preview-profile endpoint (no
 * AnomalyExecution row created) and renders the returned per-column stats
 * as a compact table. Purely a preview aid for picking sensible detection
 * methods/thresholds before saving. */
function ProfilingPanel({ objectRef, columns }) {
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const canProfile = !!(columns.length && (objectRef.uploaded_file_id != null || (objectRef.connection_id != null && objectRef.object_name)));

  const runProfile = async () => {
    setLoading(true); setError(''); setProfile(null);
    try {
      const result = await anomalyApi.previewProfile({
        connection_id: objectRef.connection_id ?? FILE_BACKED_SENTINEL,
        schema_name: objectRef.schema_name || null,
        object_name: objectRef.object_name,
        uploaded_file_id: objectRef.uploaded_file_id || null,
        columns,
      });
      setProfile(result);
    } catch (err) {
      setError(err?.response?.data?.detail || err?.message || 'Profiling failed.');
    } finally {
      setLoading(false);
    }
  };

  const statKeys = useMemo(() => {
    if (!profile) return [];
    const keys = new Set();
    for (const [col, stats] of Object.entries(profile)) {
      if (col === '__total__') continue;
      Object.keys(stats).forEach((k) => keys.add(k));
    }
    // A stable, useful-first ordering; anything else (data-type specific
    // extras like category_counts) still renders, just after these.
    const priority = ['data_type', 'count', 'null_count', 'null_pct', 'distinct_count', 'min', 'max', 'mean', 'std_dev', 'median', 'p1', 'p25', 'p50', 'p75', 'p99'];
    return [...priority.filter((k) => keys.has(k)), ...[...keys].filter((k) => !priority.includes(k) && k !== 'sampled' && k !== 'sample_size' && typeof profile[Object.keys(profile).find((c) => c !== '__total__')]?.[k] !== 'object')];
  }, [profile]);

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <button type="button" className="anm-col-toolbar-btn" disabled={!canProfile || loading} onClick={runProfile}>
          {loading ? 'Profiling…' : 'Profile Selected Columns'}
        </button>
        {profile && <span style={{ fontSize: 11.5, color: 'var(--text-faint)' }}>{profile.__total__?.toLocaleString()} total rows scanned</span>}
      </div>
      {error && <div className="anm-upload-error">{error}</div>}
      {!profile && !loading && !error && <div className="anm-profile-empty">Pick a source and at least one column above, then click Profile to see real live stats before choosing detection methods.</div>}
      {profile && (
        <div className="anm-profile-table-wrap">
          <table className="anm-profile-table">
            <thead>
              <tr>
                <th>Column</th>
                {statKeys.map((k) => <th key={k}>{k}</th>)}
              </tr>
            </thead>
            <tbody>
              {Object.entries(profile).filter(([col]) => col !== '__total__').map(([col, stats]) => (
                <tr key={col}>
                  <td>{col}</td>
                  {statKeys.map((k) => {
                    const v = stats[k];
                    return <td key={k}>{v === null || v === undefined ? '—' : (typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString() : v.toFixed(3)) : String(v))}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function AnomalyJobSetup({
  objectRef, setObjectRef,
  selectedColumns, setSelectedColumns,
  idColumn, setIdColumn,
  methods, setMethods,
  baselineSource, setBaselineSource,
  baselineNRuns, setBaselineNRuns,
  baselineNDays, setBaselineNDays,
  requireApproval, setRequireApproval,
  createIncidentOnCritical, setCreateIncidentOnCritical,
  retentionDays, setRetentionDays,
  locked,
}) {
  const [allColumns, setAllColumns] = useState([]);

  useEffect(() => {
    if (objectRef.uploaded_file_id) {
      uploadedFileApi.getColumns(objectRef.uploaded_file_id)
        .then((r) => setAllColumns(r.columns.map((name) => ({ name, data_type: '' })))).catch(() => setAllColumns([]));
      return;
    }
    if (!objectRef.connection_id || !objectRef.object_name) { setAllColumns([]); return; }
    connectionApi.getColumns(objectRef.connection_id, objectRef.object_name, { schema: objectRef.schema_name || undefined })
      .then((r) => setAllColumns(r.columns)).catch(() => setAllColumns([]));
  }, [objectRef.connection_id, objectRef.schema_name, objectRef.object_name, objectRef.uploaded_file_id]);

  const toggleMethod = (id) => setMethods(methods.includes(id) ? methods.filter((m) => m !== id) : [...methods, id]);

  return (
    <div className="anm-setup">
      <div>
        <div className="anm-setup-section-title">Source Object</div>
        <ObjectPicker value={objectRef} onChange={setObjectRef} disabled={locked} />
      </div>

      <div>
        <div className="anm-setup-section-title">
          Columns to Check
          {selectedColumns.length > 0 && <span className="anm-col-count-badge">{selectedColumns.length} selected</span>}
        </div>
        <ColumnTable
          allColumns={allColumns} selectedColumns={selectedColumns} onChangeSelected={setSelectedColumns}
          idColumn={idColumn} onChangeIdColumn={setIdColumn}
          disabled={!allColumns.length} emptyLabel="Pick a source connection/file, schema and table above first."
        />
        <span className="anm-setup-section-hint">
          The identifier radio (optional) picks which column labels anomalous records in results — e.g. an order_id or primary key. Without one, records are identified by row position.
        </span>
      </div>

      <div>
        <div className="anm-setup-section-title">Column Profile (live preview)</div>
        <ProfilingPanel objectRef={objectRef} columns={selectedColumns} />
      </div>

      <div>
        <div className="anm-setup-section-title">Detection Methods</div>
        <div className="anm-options">
          {DETECTION_METHODS.map((m) => (
            <div key={m.id} className={`anm-option-card ${methods.includes(m.id) ? 'selected' : ''}`} onClick={() => toggleMethod(m.id)}>
              <div className="anm-option-check">
                {methods.includes(m.id) && <span style={{ color: 'white', fontSize: 10, fontWeight: 700 }}>✓</span>}
              </div>
              <div>
                <div className="anm-option-cat">{m.category}</div>
                <div className="anm-option-title">{m.label}</div>
                <div className="anm-option-desc">{m.desc}</div>
              </div>
            </div>
          ))}
        </div>
        <span className="anm-setup-section-hint">Applied to every selected column above (per-column method overrides can be refined later from the job's detection_config).</span>
      </div>

      <div>
        <div className="anm-setup-section-title">Baseline</div>
        <div className="anm-options">
          {BASELINE_SOURCES.map((b) => (
            <div key={b.id} className={`anm-option-card ${baselineSource === b.id ? 'selected' : ''}`} onClick={() => setBaselineSource(b.id)}>
              <div className="anm-option-check">
                {baselineSource === b.id && <span style={{ color: 'white', fontSize: 10, fontWeight: 700 }}>✓</span>}
              </div>
              <div>
                <div className="anm-option-title">{b.label}</div>
                <div className="anm-option-desc">{b.desc}</div>
              </div>
            </div>
          ))}
        </div>
        <div className="anm-field-row" style={{ marginTop: 12 }}>
          {(baselineSource === 'LAST_N_RUNS' || baselineSource === 'ROLLING_WINDOW') && (
            <div className="anm-field">
              <label className="anm-label">N Runs</label>
              <input type="number" min="1" className="anm-input" value={baselineNRuns} onChange={(e) => setBaselineNRuns(Number(e.target.value) || 1)} />
            </div>
          )}
          {baselineSource === 'LAST_N_DAYS' && (
            <div className="anm-field">
              <label className="anm-label">N Days</label>
              <input type="number" min="1" className="anm-input" value={baselineNDays} onChange={(e) => setBaselineNDays(Number(e.target.value) || 1)} />
            </div>
          )}
          <div className="anm-field">
            <label className="anm-checkbox-row">
              <input type="checkbox" checked={requireApproval} onChange={(e) => setRequireApproval(e.target.checked)} />
              Require manual approval before a computed baseline is used
            </label>
          </div>
        </div>
      </div>

      <div className="anm-field-row">
        <div className="anm-field">
          <label className="anm-checkbox-row">
            <input type="checkbox" checked={createIncidentOnCritical} onChange={(e) => setCreateIncidentOnCritical(e.target.checked)} />
            Create an incident automatically on a CRITICAL result
          </label>
        </div>
        <div className="anm-field">
          <label className="anm-label">Retention (days, optional)</label>
          <input
            type="number" min="1" className="anm-input" placeholder="Keep forever"
            value={retentionDays ?? ''} onChange={(e) => setRetentionDays(e.target.value ? Number(e.target.value) : null)}
          />
        </div>
      </div>
    </div>
  );
}
