/**
 * MappingRowEditor — add/edit a single mapping row. Supports a direct
 * source-column mapping, a multi-column expression (check more than one
 * source column, then write a transformation referencing them — e.g.
 * TRIM(FIRST_NAME || ' ' || LAST_NAME)), a constant value, a default
 * value (COALESCE fallback), or a lookup join against a reference table
 * in the same connection. Nothing here is silently destructive — this
 * only ever changes local draft state; the caller still has to hit
 * Save Mapping to persist it.
 */
import { useEffect, useState } from 'react';
import { Modal, Button } from '@/components/common/index.js';

const emptyLookup = { table: '', key_column: '', return_column: '' };

export function MappingRowEditor({ open, onClose, onSave, row, sourceColumns, targetColumns, mappedTargetNames }) {
  const [targetColumn, setTargetColumn] = useState('');
  const [sourceCols, setSourceCols] = useState([]);
  const [transformation, setTransformation] = useState('');
  const [constantValue, setConstantValue] = useState('');
  const [useConstant, setUseConstant] = useState(false);
  const [defaultValue, setDefaultValue] = useState('');
  const [useDefault, setUseDefault] = useState(false);
  const [lookup, setLookup] = useState(emptyLookup);
  const [useLookup, setUseLookup] = useState(false);

  const isEditing = !!row?.id;

  useEffect(() => {
    if (!open) return;
    if (row) {
      setTargetColumn(row.target_column);
      setSourceCols(row.source_columns || []);
      setTransformation(row.transformation || '');
      setUseConstant(row.constant_value != null);
      setConstantValue(row.constant_value ?? '');
      setUseDefault(row.default_value != null);
      setDefaultValue(row.default_value ?? '');
      setUseLookup(!!row.lookup);
      setLookup(row.lookup || emptyLookup);
    } else {
      setTargetColumn(''); setSourceCols([]); setTransformation('');
      setUseConstant(false); setConstantValue('');
      setUseDefault(false); setDefaultValue('');
      setUseLookup(false); setLookup(emptyLookup);
    }
  }, [open, row]);

  const toggleSourceCol = (name) => {
    setSourceCols((prev) => (prev.includes(name) ? prev.filter((c) => c !== name) : [...prev, name]));
  };

  const availableTargets = targetColumns.filter((c) => c.name === row?.target_column || !mappedTargetNames.has(c.name));

  const canSave = targetColumn && (sourceCols.length > 0 || useConstant || useLookup);

  const handleSave = () => {
    onSave({
      id: isEditing ? row.id : `map_manual_${targetColumn}_${Date.now()}`,
      source_columns: useLookup ? sourceCols.slice(0, 1) : sourceCols,
      target_column: targetColumn,
      transformation: useLookup ? '' : transformation.trim(),
      constant_value: useConstant && constantValue !== '' ? constantValue : null,
      default_value: useDefault && defaultValue !== '' ? defaultValue : null,
      lookup: useLookup && lookup.table && lookup.key_column && lookup.return_column ? lookup : null,
      status: 'MAPPED',
      confidence: null,
      match_method: 'manual',
    });
  };

  return (
    <Modal open={open} onClose={onClose} title={isEditing ? 'Edit Mapping' : 'Add Mapping'} size="lg"
      footer={<>
        <Button variant="secondary" onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={!canSave} onClick={handleSave}>{isEditing ? 'Update' : 'Add'} Mapping</Button>
      </>}
    >
      <div className="ocm-editor-field">
        <label className="ocm-editor-label">Target Column *</label>
        <select className="ocm-select" style={{ width: '100%' }} value={targetColumn} onChange={(e) => setTargetColumn(e.target.value)}>
          <option value="">Select target column…</option>
          {availableTargets.map((c) => <option key={c.name} value={c.name}>{c.name} ({c.data_type})</option>)}
        </select>
      </div>

      {!useLookup && (
        <div className="ocm-editor-field">
          <label className="ocm-editor-label">Source Column(s)</label>
          <div className="ocm-checkbox-grid">
            {sourceColumns.map((c) => (
              <label key={c.name} className="ocm-checkbox-item">
                <input type="checkbox" checked={sourceCols.includes(c.name)} onChange={() => toggleSourceCol(c.name)} />
                {c.name} <span className="ocm-type-hint">{c.data_type}</span>
              </label>
            ))}
          </div>
          <span className="ocm-editor-hint">Check more than one column to build a multi-column expression below (e.g. concatenation).</span>
        </div>
      )}

      {!useLookup && (
        <div className="ocm-editor-field">
          <label className="ocm-editor-label">Transformation</label>
          <textarea className="ocm-textarea" value={transformation} onChange={(e) => setTransformation(e.target.value)}
            placeholder={'Leave empty for a direct passthrough, or e.g.\nTRIM(FIRST_NAME || \' \' || LAST_NAME)\nUPPER(country_code)\nCASE WHEN status = \'A\' THEN \'ACTIVE\' ELSE \'INACTIVE\' END'} />
          <span className="ocm-editor-hint">SQL expression referencing the checked source column(s). SELECT-safe only — no semicolons or DML/DDL keywords.</span>
        </div>
      )}

      <div className="ocm-editor-field">
        <label className="ocm-checkbox-item"><input type="checkbox" checked={useConstant} onChange={(e) => setUseConstant(e.target.checked)} /> Use a constant value instead of a source column</label>
        {useConstant && <input className="ocm-input" style={{ marginTop: 6 }} value={constantValue} onChange={(e) => setConstantValue(e.target.value)} placeholder="e.g. UNKNOWN" />}
      </div>

      <div className="ocm-editor-field">
        <label className="ocm-checkbox-item"><input type="checkbox" checked={useDefault} onChange={(e) => setUseDefault(e.target.checked)} /> Fall back to a default value when the source is NULL</label>
        {useDefault && <input className="ocm-input" style={{ marginTop: 6 }} value={defaultValue} onChange={(e) => setDefaultValue(e.target.value)} placeholder="e.g. UNKNOWN" />}
      </div>

      <div className="ocm-editor-field">
        <label className="ocm-checkbox-item"><input type="checkbox" checked={useLookup} onChange={(e) => setUseLookup(e.target.checked)} /> Look up the value in a reference table (same connection)</label>
        {useLookup && (
          <div className="ocm-lookup-grid">
            <div>
              <label className="ocm-editor-label">Join on source column</label>
              <select className="ocm-select" value={sourceCols[0] || ''} onChange={(e) => setSourceCols([e.target.value])}>
                <option value="">Select…</option>
                {sourceColumns.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
              </select>
            </div>
            <div>
              <label className="ocm-editor-label">Reference table</label>
              <input className="ocm-input" value={lookup.table} onChange={(e) => setLookup((l) => ({ ...l, table: e.target.value }))} placeholder="e.g. country_reference" />
            </div>
            <div>
              <label className="ocm-editor-label">Reference key column</label>
              <input className="ocm-input" value={lookup.key_column} onChange={(e) => setLookup((l) => ({ ...l, key_column: e.target.value }))} placeholder="e.g. country_code" />
            </div>
            <div>
              <label className="ocm-editor-label">Return column</label>
              <input className="ocm-input" value={lookup.return_column} onChange={(e) => setLookup((l) => ({ ...l, return_column: e.target.value }))} placeholder="e.g. country_name" />
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
