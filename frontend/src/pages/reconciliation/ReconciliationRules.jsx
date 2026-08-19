/**
 * ReconciliationRules — real source/target connection+object pickers,
 * key/compare column selection (from the source object's live columns —
 * both sides are expected to use the same column names), and comparison
 * type toggles. Props come from the parent Reconciliation.jsx page.
 */
import { useEffect, useState } from 'react';
import { connectionApi } from '@/services/api/connectionApi.js';
import { COMPARISON_TYPES } from '@/services/api/reconciliationApi.js';
import './reconciliation.css';

function ObjectPicker({ label, value, onChange, disabled }) {
  const [connections, setConnections] = useState([]);
  const [schemas, setSchemas] = useState([]);
  const [objects, setObjects] = useState([]);
  const [loadingSchemas, setLoadingSchemas] = useState(false);
  const [loadingObjects, setLoadingObjects] = useState(false);

  useEffect(() => { connectionApi.list({ category: 'DATABASE', page_size: 200 }).then((r) => setConnections(r.items)).catch(() => {}); }, []);

  useEffect(() => {
    if (!value.connection_id) { setSchemas([]); return; }
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
  }, [value.connection_id]);

  useEffect(() => {
    if (!value.connection_id || !value.schema_name) { setObjects([]); return; }
    setLoadingObjects(true);
    connectionApi.getObjects(value.connection_id, { schema: value.schema_name })
      .then((r) => setObjects(r.items)).catch(() => setObjects([])).finally(() => setLoadingObjects(false));
  }, [value.connection_id, value.schema_name]);

  return (
    <div className="rcn-ds-card">
      <div className="rcn-ds-header">
        <span className={`rcn-ds-badge ${label.toLowerCase()}`}>{label}</span>
        <span style={{ fontSize: 'var(--fs-sm)', fontWeight: 600, color: 'var(--text-primary)' }}>{label} Dataset</span>
      </div>
      <div className="rcn-field">
        <label className="rcn-label">Connection</label>
        <select className="rcn-select" disabled={disabled} value={value.connection_id ?? ''}
          onChange={(e) => onChange({ connection_id: e.target.value ? Number(e.target.value) : null, schema_name: '', object_name: '' })}>
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
    </div>
  );
}

export function ReconciliationRules({
  source, setSource, target, setTarget,
  keyColumns, setKeyColumns, compareColumns, setCompareColumns,
  comparisonTypes, setComparisonTypes, locked,
}) {
  const [sourceColumns, setSourceColumns] = useState([]);

  useEffect(() => {
    if (!source.connection_id || !source.object_name) { setSourceColumns([]); return; }
    connectionApi.getColumns(source.connection_id, source.object_name, { schema: source.schema_name || undefined })
      .then((r) => setSourceColumns(r.columns)).catch(() => setSourceColumns([]));
  }, [source.connection_id, source.schema_name, source.object_name]);

  const toggleCompType = (id) => {
    setComparisonTypes(comparisonTypes.includes(id) ? comparisonTypes.filter((c) => c !== id) : [...comparisonTypes, id]);
  };
  const needsKeyMatch = comparisonTypes.includes('KEY_MATCH') || comparisonTypes.includes('FIELD_LEVEL');
  const colNames = sourceColumns.map((c) => c.name);

  return (
    <div className="rcn-setup">
      <ObjectPicker label="Source" value={source} onChange={setSource} disabled={locked} />
      <ObjectPicker label="Target" value={target} onChange={setTarget} disabled={locked} />

      <div>
        <div style={{ fontWeight: 700, fontSize: 'var(--fs-sm)', color: 'var(--text-primary)', marginBottom: 8 }}>
          Key Column(s) {needsKeyMatch && <span style={{ color: 'var(--color-danger)' }}>*</span>}
        </div>
        <select multiple className="rcn-multiselect" value={keyColumns} disabled={!colNames.length}
          onChange={(e) => setKeyColumns([...e.target.selectedOptions].map((o) => o.value))}>
          {colNames.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <span style={{ fontSize: 11, color: 'var(--text-faint)', display: 'block', marginTop: 4 }}>
          Same column name(s) must exist on both source and target. Required for Key Match / Field-Level.
        </span>
      </div>

      <div>
        <div style={{ fontWeight: 700, fontSize: 'var(--fs-sm)', color: 'var(--text-primary)', marginBottom: 8 }}>Columns to Compare</div>
        <select multiple className="rcn-multiselect" value={compareColumns} disabled={!colNames.length}
          onChange={(e) => setCompareColumns([...e.target.selectedOptions].map((o) => o.value))}>
          {colNames.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <span style={{ fontSize: 11, color: 'var(--text-faint)', display: 'block', marginTop: 4 }}>
          Used by Checksum (falls back to key columns if none picked) and Field-Level comparison.
        </span>
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
