/**
 * ReconciliationRules — real source/target connection+object pickers,
 * key/compare column selection (from the source object's live columns —
 * both sides are expected to use the same column names), and comparison
 * type toggles. Props come from the parent Reconciliation.jsx page.
 */
import { useEffect, useState } from 'react';
import { Loader2, Trash2, Upload } from 'lucide-react';
import { connectionApi } from '@/services/api/connectionApi.js';
import { uploadedFileApi } from '@/services/api/uploadedFileApi.js';
import { COMPARISON_TYPES } from '@/services/api/reconciliationApi.js';
import './reconciliation.css';

// 0 is the real "not a real Connection — see uploaded_file_id instead"
// sentinel this whole feature is built on (backend/app/models/reconciliation.py).
const FILE_BACKED_SENTINEL = 0;

function ObjectPicker({ label, value, onChange, disabled }) {
  const [connections, setConnections] = useState([]);
  const [schemas, setSchemas] = useState([]);
  const [objects, setObjects] = useState([]);
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [loadingObjects, setLoadingObjects] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');

  // A brand-new (never-saved) picker defaults to Connection mode; an
  // already-saved job's mode is read straight off its persisted data —
  // uploaded_file_id set means it was created file-backed and can't switch.
  const mode = value.uploaded_file_id ? 'file' : (value.mode || 'connection');

  useEffect(() => { connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setConnections(r.items)).catch(() => {}); }, []);

  useEffect(() => {
    if (mode !== 'connection' || !value.connection_id) { setSchemas([]); return; }
    setLoadingSchemas(true);
    connectionApi.getSchemas(value.connection_id).then((r) => {
      setSchemas(r.schemas);
      // Single-database dialects (MySQL) report exactly one "schema" —
      // the database the connection already points at. Auto-select it
      // rather than making the user pick from a one-item list before
      // objects can load.
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
    <div className="rcn-ds-card">
      <div className="rcn-ds-header">
        <span className={`rcn-ds-badge ${label.toLowerCase()}`}>{label}</span>
        <span style={{ fontSize: 'var(--fs-sm)', fontWeight: 600, color: 'var(--text-primary)' }}>{label} Dataset</span>
      </div>

      <div className="rcn-mode-toggle" role="tablist">
        <button type="button" role="tab" aria-selected={mode === 'connection'}
          className={`rcn-mode-btn ${mode === 'connection' ? 'active' : ''}`}
          disabled={disabled} onClick={() => switchMode('connection')}>
          Connection
        </button>
        <button type="button" role="tab" aria-selected={mode === 'file'}
          className={`rcn-mode-btn ${mode === 'file' ? 'active' : ''}`}
          disabled={disabled} onClick={() => switchMode('file')}>
          Upload File
        </button>
      </div>

      {mode === 'connection' ? (
        <>
          <div className="rcn-field">
            <label className="rcn-label">Connection</label>
            <select className="rcn-select" disabled={disabled} value={value.connection_id ?? ''}
              onChange={(e) => onChange({ connection_id: e.target.value ? Number(e.target.value) : null, schema_name: '', object_name: '', uploaded_file_id: null, mode: 'connection' })}>
              <option value="">Select connection…</option>
              {connections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
            </select>
          </div>
          <div className="rcn-field">
            <label className="rcn-label">Schema</label>
            <select className="rcn-select" disabled={disabled || !value.connection_id} value={value.schema_name}
              onChange={(e) => onChange({ ...value, schema_name: e.target.value, object_name: '' })}>
              <option value="">{loadingSchemas ? 'Loading…' : 'Select schema…'}</option>
              {schemas.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className="rcn-field">
            <label className="rcn-label">Table / View</label>
            <select className="rcn-select" disabled={disabled || !value.schema_name} value={value.object_name}
              onChange={(e) => onChange({ ...value, object_name: e.target.value })}>
              <option value="">{loadingObjects ? 'Loading…' : 'Select object…'}</option>
              {objects.map((o) => <option key={o.name} value={o.name}>{o.name}</option>)}
            </select>
          </div>
        </>
      ) : (
        <div className="rcn-field">
          <label className="rcn-label">File</label>
          {value.uploaded_file_id ? (
            <div className="rcn-uploaded-file-card">
              <span className="rcn-uploaded-file-name">{value.object_name}</span>
              {!disabled && (
                <button type="button" className="rcn-icon-btn" title="Remove file" onClick={clearUpload}>
                  <Trash2 size={12} />
                </button>
              )}
            </div>
          ) : (
            <>
              <label className="rcn-upload-btn" style={{ cursor: disabled || uploading ? 'default' : 'pointer' }}>
                {uploading ? <Loader2 size={12} className="rcn-spin" /> : <Upload size={12} />}
                {uploading ? 'Uploading…' : 'Upload File'}
                <input
                  type="file" style={{ display: 'none' }} disabled={disabled || uploading}
                  accept=".csv,.tsv,.txt,.dat,.xlsx,.jsonl,.ndjson"
                  onChange={(e) => { handleUpload(e.target.files?.[0]); e.target.value = ''; }}
                />
              </label>
              <span style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 5, display: 'block' }}>
                CSV, TSV, TXT, Excel (.xlsx) or JSON Lines. Columns and row count are detected automatically.
              </span>
            </>
          )}
          {uploadError && <span className="rcn-upload-error">{uploadError}</span>}
        </div>
      )}
    </div>
  );
}

/**
 * ColumnCheckboxList — real checkbox picker for a set of column names.
 * Replaces a native <select multiple>, whose "click = replace selection,
 * ctrl/cmd-click = add" behavior isn't discoverable — clicking a row in a
 * plain list box doesn't visibly register as "picking multiple things",
 * so it read as "there's no option to provide a key column" even though
 * the control was there. Every row is an explicit checkbox with a visible
 * checked state, no modifier keys needed.
 */
function ColumnCheckboxList({ columns, selected, onChange, disabled, emptyLabel }) {
  const toggle = (name) => {
    onChange(selected.includes(name) ? selected.filter((c) => c !== name) : [...selected, name]);
  };
  if (disabled) {
    return <div className="rcn-col-checklist rcn-col-checklist-empty">{emptyLabel}</div>;
  }
  return (
    <div className="rcn-col-checklist">
      {columns.map((c) => (
        <label key={c} className="rcn-col-check-row">
          <input type="checkbox" checked={selected.includes(c)} onChange={() => toggle(c)} />
          <span>{c}</span>
        </label>
      ))}
    </div>
  );
}

/**
 * ColumnMappingTable — real source-column → target-column picker. Reconciles
 * two objects that hold the same data under different column names (a
 * common real case — legacy table vs its replacement, or two systems that
 * never agreed on naming). Any source column left unmapped here falls back
 * to the same name on the target — this table only overrides that default
 * for the columns that actually need it, it doesn't require mapping every
 * column.
 */
function ColumnMappingTable({ sourceColumns, targetColumns, mapping, onChange, disabled, emptyLabel }) {
  const targetSet = new Set(targetColumns);
  const setMap = (src, tgt) => {
    const next = { ...mapping };
    if (!tgt) delete next[src];
    else next[src] = tgt;
    onChange(next);
  };
  if (disabled) {
    return <div className="rcn-col-checklist rcn-col-checklist-empty">{emptyLabel}</div>;
  }
  return (
    <div className="rcn-colmap-table">
      {sourceColumns.map((src) => {
        const mapped = mapping[src] || '';
        const effective = mapped || src;
        const autoMatched = !mapped && targetSet.has(src);
        const unresolved = !targetSet.has(effective);
        return (
          <div key={src} className="rcn-colmap-row">
            <span className="rcn-colmap-src">{src}</span>
            <span className="rcn-colmap-arrow">→</span>
            <select className="rcn-colmap-select" value={mapped} onChange={(e) => setMap(src, e.target.value)}>
              <option value="">{targetSet.has(src) ? `${src} (same name)` : '— pick target column —'}</option>
              {targetColumns.filter((t) => t !== src).map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            {autoMatched && <span className="rcn-colmap-tag rcn-colmap-tag-auto">auto-matched</span>}
            {!autoMatched && mapped && <span className="rcn-colmap-tag rcn-colmap-tag-mapped">mapped</span>}
            {unresolved && <span className="rcn-colmap-tag rcn-colmap-tag-warn">no match on target</span>}
          </div>
        );
      })}
    </div>
  );
}

export function ReconciliationRules({
  source, setSource, target, setTarget,
  keyColumns, setKeyColumns, compareColumns, setCompareColumns,
  comparisonTypes, setComparisonTypes,
  columnMapping, setColumnMapping,
  fallbackMatchColumns, setFallbackMatchColumns,
  nameNormalization, setNameNormalization,
  locked,
}) {
  const [sourceColumns, setSourceColumns] = useState([]);
  const [targetColumns, setTargetColumns] = useState([]);

  useEffect(() => {
    if (source.uploaded_file_id) {
      uploadedFileApi.getColumns(source.uploaded_file_id)
        .then((r) => setSourceColumns(r.columns.map((name) => ({ name })))).catch(() => setSourceColumns([]));
      return;
    }
    if (!source.connection_id || !source.object_name) { setSourceColumns([]); return; }
    connectionApi.getColumns(source.connection_id, source.object_name, { schema: source.schema_name || undefined })
      .then((r) => setSourceColumns(r.columns)).catch(() => setSourceColumns([]));
  }, [source.connection_id, source.schema_name, source.object_name, source.uploaded_file_id]);

  useEffect(() => {
    if (target.uploaded_file_id) {
      uploadedFileApi.getColumns(target.uploaded_file_id)
        .then((r) => setTargetColumns(r.columns.map((name) => ({ name })))).catch(() => setTargetColumns([]));
      return;
    }
    if (!target.connection_id || !target.object_name) { setTargetColumns([]); return; }
    connectionApi.getColumns(target.connection_id, target.object_name, { schema: target.schema_name || undefined })
      .then((r) => setTargetColumns(r.columns)).catch(() => setTargetColumns([]));
  }, [target.connection_id, target.schema_name, target.object_name, target.uploaded_file_id]);

  const toggleCompType = (id) => {
    setComparisonTypes(comparisonTypes.includes(id) ? comparisonTypes.filter((c) => c !== id) : [...comparisonTypes, id]);
  };
  const needsKeyMatch = comparisonTypes.includes('KEY_MATCH') || comparisonTypes.includes('FIELD_LEVEL');
  const colNames = sourceColumns.map((c) => c.name);
  const targetColNames = targetColumns.map((c) => c.name);

  return (
    <div className="rcn-setup">
      <ObjectPicker label="Source" value={source} onChange={setSource} disabled={locked} />
      <ObjectPicker label="Target" value={target} onChange={setTarget} disabled={locked} />

      <div>
        <div style={{ fontWeight: 700, fontSize: 'var(--fs-sm)', color: 'var(--text-primary)', marginBottom: 8 }}>
          Key Column(s) {needsKeyMatch && <span style={{ color: 'var(--color-danger)' }}>*</span>}
          {keyColumns.length > 0 && <span className="rcn-col-count-badge">{keyColumns.length} selected</span>}
        </div>
        <ColumnCheckboxList
          columns={colNames} selected={keyColumns} onChange={setKeyColumns} disabled={!colNames.length}
          emptyLabel="Pick a source connection, schema and table above first."
        />
        <span style={{ fontSize: 11, color: 'var(--text-faint)', display: 'block', marginTop: 4 }}>
          Same column name(s) must exist on both source and target. Required for Key Match / Field-Level.
        </span>
      </div>

      <div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
          <div style={{ fontWeight: 700, fontSize: 'var(--fs-sm)', color: 'var(--text-primary)' }}>
            Columns to Compare
            {compareColumns.length > 0 && <span className="rcn-col-count-badge">{compareColumns.length} selected</span>}
          </div>
          {colNames.length > 0 && (
            <button
              type="button" className="rcn-col-select-all"
              onClick={() => setCompareColumns(compareColumns.length === colNames.length ? [] : colNames)}
            >
              {compareColumns.length === colNames.length ? 'Clear all' : 'Select all'}
            </button>
          )}
        </div>
        <ColumnCheckboxList
          columns={colNames} selected={compareColumns} onChange={setCompareColumns} disabled={!colNames.length}
          emptyLabel="Pick a source connection, schema and table above first."
        />
        <span style={{ fontSize: 11, color: 'var(--text-faint)', display: 'block', marginTop: 4 }}>
          Used by Checksum (falls back to key columns, then to every real column on the object if neither is picked) and Field-Level comparison.
        </span>
      </div>

      <div style={{ gridColumn: '1 / -1' }}>
        <div style={{ fontWeight: 700, fontSize: 'var(--fs-sm)', color: 'var(--text-primary)', marginBottom: 4 }}>
          Column Mapping <span style={{ fontWeight: 500, color: 'var(--text-faint)' }}>(optional)</span>
        </div>
        <span style={{ fontSize: 11, color: 'var(--text-faint)', display: 'block', marginBottom: 8 }}>
          Map a source column to a differently-named target column when the two sides hold the same data under
          different names. Anything left unmapped compares by matching column name on both sides, as before.
        </span>
        <ColumnMappingTable
          sourceColumns={colNames} targetColumns={targetColNames} mapping={columnMapping} onChange={setColumnMapping}
          disabled={!colNames.length || !targetColNames.length}
          emptyLabel="Pick a source AND target connection, schema and table above first."
        />
      </div>

      <div style={{ gridColumn: '1 / -1' }}>
        <div style={{ fontWeight: 700, fontSize: 'var(--fs-sm)', color: 'var(--text-primary)', marginBottom: 4 }}>
          Fallback Match Columns <span style={{ fontWeight: 500, color: 'var(--text-faint)' }}>(optional)</span>
          {fallbackMatchColumns.length > 0 && <span className="rcn-col-count-badge">{fallbackMatchColumns.length} selected</span>}
        </div>
        <span style={{ fontSize: 11, color: 'var(--text-faint)', display: 'block', marginBottom: 8 }}>
          When a source record's key doesn't exist on the target (e.g. IDs changed after a migration), try matching
          by these column(s) instead — normalized per the rules below — before reporting it Missing. Two or more
          target records sharing the same normalized name are never auto-picked; they're reported Ambiguous instead.
        </span>
        <ColumnCheckboxList
          columns={colNames} selected={fallbackMatchColumns} onChange={setFallbackMatchColumns} disabled={!colNames.length}
          emptyLabel="Pick a source connection, schema and table above first."
        />
        {fallbackMatchColumns.length > 0 && (
          <div style={{ display: 'flex', gap: 16, marginTop: 10, flexWrap: 'wrap' }}>
            {[
              ['trim', 'Trim whitespace'],
              ['case_insensitive', 'Case-insensitive'],
              ['collapse_spaces', 'Collapse repeated spaces'],
              ['unicode_normalize', 'Unicode normalize (NFKC)'],
            ].map(([key, label]) => (
              <label key={key} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-secondary)', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={nameNormalization[key] ?? (key === 'trim' || key === 'case_insensitive')}
                  onChange={(e) => setNameNormalization({ ...nameNormalization, [key]: e.target.checked })}
                />
                {label}
              </label>
            ))}
          </div>
        )}
      </div>

      <div style={{ gridColumn: '1 / -1' }}>
        <div style={{ fontWeight: 700, fontSize: 'var(--fs-sm)', color: 'var(--text-primary)', marginBottom: 10 }}>Comparison Types</div>
        <div className="rcn-options">
          {COMPARISON_TYPES.map((ct) => (
            <div key={ct.id} className={`rcn-option-card ${comparisonTypes.includes(ct.id) ? 'selected' : ''}`} onClick={() => toggleCompType(ct.id)}>
              <div className="rcn-option-check">
                {comparisonTypes.includes(ct.id) && <span style={{ color: 'white', fontSize: 10, fontWeight: 700 }}>✓</span>}
              </div>
              <div>
                <div className="rcn-option-title">{ct.label}</div>
                <div className="rcn-option-desc">{ct.desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
