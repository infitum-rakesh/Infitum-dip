/**
 * ValidationRules — rule list with add / edit / toggle / delete. Edits a
 * local draft array (committed to the backend only when the parent's
 * "Save Rule Set" runs) — same pattern as ObjectColumnMapping's mapping
 * rows. Props: rules, onRulesChange, columns (live ColumnInfo[] from the
 * picked object), disabled.
 */
import { useState } from 'react';
import { Plus, Pencil, Trash2, AlertCircle } from 'lucide-react';
import { Button, Modal, StatusBadge } from '@/components/common/index.js';
import { RULE_TYPES } from '@/services/api/validationApi.js';
import './validation.css';

const SEVERITY_VARIANT = { error: 'danger', warning: 'warning', info: 'info' };

function blankRule() {
  return { id: `rule_${Date.now()}`, name: '', type: 'NOT_NULL', columns: [], severity: 'error', enabled: true, threshold: 100, config: {} };
}

export function ValidationRules({ rules, onRulesChange, columns, disabled }) {
  const [modalOpen, setModalOpen] = useState(false);
  const [editId, setEditId] = useState(null);
  const [form, setForm] = useState(blankRule());

  const openAdd = () => { setEditId(null); setForm(blankRule()); setModalOpen(true); };
  const openEdit = (rule) => { setEditId(rule.id); setForm({ ...rule, config: { ...rule.config } }); setModalOpen(true); };

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }));
  const setCfg = (k) => (e) => setForm((f) => ({ ...f, config: { ...f.config, [k]: e.target.value } }));

  const handleSave = () => {
    if (!form.name.trim() || form.columns.length === 0) return;
    if (editId) {
      onRulesChange(rules.map((r) => (r.id === editId ? { ...form } : r)));
    } else {
      onRulesChange([...rules, { ...form }]);
    }
    setModalOpen(false);
  };

  const handleDelete = (id) => onRulesChange(rules.filter((r) => r.id !== id));
  const handleToggle = (rule) => onRulesChange(rules.map((r) => (r.id === rule.id ? { ...r, enabled: !r.enabled } : r)));

  const colNames = (columns || []).map((c) => c.name);
  const isMultiColumn = form.type === 'UNIQUE';

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 16px', borderBottom: '1px solid var(--border)', background: '#F8FAFC' }}>
        <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-muted)', fontWeight: 500 }}>
          {rules.filter((r) => r.enabled).length} of {rules.length} rules enabled
        </span>
        <Button variant="primary" size="sm" icon={<Plus size={13} />} disabled={disabled} onClick={openAdd}>Add Rule</Button>
      </div>

      <div className="vld-rule-row" style={{ background: '#F8FAFC', borderBottom: '1px solid var(--border)', cursor: 'default' }}>
        {['Rule Name', 'Type', 'Column(s)', 'Severity', 'Threshold', ''].map((h) => (
          <span key={h} style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.4px' }}>{h}</span>
        ))}
      </div>

      {rules.length === 0 ? (
        <div style={{ padding: 48, textAlign: 'center' }}>
          <div style={{ fontSize: 32, marginBottom: 10 }}>🛡️</div>
          <p style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)' }}>No rules yet</p>
          <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 4, marginBottom: 16 }}>
            {disabled ? 'Pick a connection and object above first.' : 'Add rules to start validating this object.'}
          </p>
          <Button variant="primary" icon={<Plus size={13} />} disabled={disabled} onClick={openAdd}>Add First Rule</Button>
        </div>
      ) : (
        rules.map((rule) => {
          const typeDef = RULE_TYPES[rule.type] ?? {};
          return (
            <div key={rule.id} className="vld-rule-row" style={{ opacity: rule.enabled ? 1 : 0.5 }}>
              <div>
                <div className="vld-rule-name">{rule.name}</div>
                {!rule.enabled && <span style={{ fontSize: 10, color: 'var(--text-faint)' }}>Disabled</span>}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                <span style={{ fontSize: 13 }}>{typeDef.icon}</span>
                <span className="vld-rule-type">{typeDef.label}</span>
              </div>
              <span className="vld-rule-col">{(rule.columns || []).join(', ')}</span>
              <StatusBadge variant={SEVERITY_VARIANT[rule.severity]} size="sm">{rule.severity}</StatusBadge>
              <span style={{ fontSize: 'var(--fs-sm)', color: 'var(--text-muted)' }}>{rule.threshold}%</span>
              <div className="vld-rule-actions">
                <div
                  style={{ width: 32, height: 18, borderRadius: 99, background: rule.enabled ? 'var(--color-primary)' : 'var(--border-dark)', position: 'relative', cursor: 'pointer', transition: 'var(--transition)', flexShrink: 0 }}
                  onClick={() => handleToggle(rule)}
                  title={rule.enabled ? 'Disable rule' : 'Enable rule'}
                >
                  <div style={{ width: 14, height: 14, borderRadius: '50%', background: 'white', position: 'absolute', top: 2, left: rule.enabled ? 16 : 2, transition: 'left 0.2s ease', boxShadow: '0 1px 2px rgba(0,0,0,0.15)' }} />
                </div>
                <Button variant="ghost" size="sm" onClick={() => openEdit(rule)} title="Edit rule"><Pencil size={12} /></Button>
                <Button variant="ghost" size="sm" onClick={() => handleDelete(rule.id)} title="Delete rule" style={{ color: 'var(--color-danger)' }}><Trash2 size={12} /></Button>
              </div>
            </div>
          );
        })
      )}

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title={editId ? 'Edit Validation Rule' : 'Add Validation Rule'}
        size="md"
        footer={
          <>
            <Button variant="secondary" onClick={() => setModalOpen(false)}>Cancel</Button>
            <Button variant="primary" onClick={handleSave}>{editId ? 'Save Changes' : 'Add Rule'}</Button>
          </>
        }
      >
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 'var(--fs-sm)', fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 8 }}>Rule Type</div>
          <div className="vld-rule-type-grid">
            {Object.entries(RULE_TYPES).map(([key, def]) => (
              <div key={key} className={`vld-rule-type-card ${form.type === key ? 'selected' : ''}`}
                onClick={() => setForm((f) => ({ ...f, type: key, columns: key === 'UNIQUE' ? f.columns : f.columns.slice(0, 1) }))}>
                <div className="vld-rule-type-icon">{def.icon}</div>
                <div className="vld-rule-type-label">{def.label}</div>
              </div>
            ))}
          </div>
        </div>

        {RULE_TYPES[form.type] && (
          <div style={{ padding: '8px 12px', background: 'var(--color-primary-light)', borderRadius: 8, fontSize: 12, color: 'var(--color-primary)', marginBottom: 16, display: 'flex', gap: 8 }}>
            <AlertCircle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
            {RULE_TYPES[form.type].desc}
          </div>
        )}

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
          <div>
            <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>Rule Name <span style={{ color: 'var(--color-danger)' }}>*</span></label>
            <input style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, outline: 'none' }} value={form.name} onChange={set('name')} placeholder="COLUMN_NOT_NULL" />
          </div>
          <div>
            <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>
              Column{isMultiColumn ? '(s)' : ''} <span style={{ color: 'var(--color-danger)' }}>*</span>
            </label>
            {isMultiColumn ? (
              <select multiple style={{ width: '100%', minHeight: 70, padding: '4px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13 }}
                value={form.columns} onChange={(e) => setForm((f) => ({ ...f, columns: [...e.target.selectedOptions].map((o) => o.value) }))}>
                {colNames.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            ) : (
              <select style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, background: 'white' }}
                value={form.columns[0] ?? ''} onChange={(e) => setForm((f) => ({ ...f, columns: e.target.value ? [e.target.value] : [] }))}>
                <option value="">Select column…</option>
                {colNames.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            )}
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <div>
            <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>Severity</label>
            <select style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, background: 'white', outline: 'none' }} value={form.severity} onChange={set('severity')}>
              <option value="error">Error</option>
              <option value="warning">Warning</option>
              <option value="info">Info</option>
            </select>
          </div>
          <div>
            <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>Pass Threshold %</label>
            <input type="number" min={0} max={100} style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, outline: 'none' }} value={form.threshold} onChange={set('threshold')} />
          </div>
        </div>

        {form.type === 'FORMAT' && (
          <div style={{ marginTop: 12 }}>
            <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>Regex Pattern</label>
            <input style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, fontFamily: 'var(--font-mono)', outline: 'none' }} value={form.config?.pattern ?? ''} onChange={setCfg('pattern')} placeholder="e.g. ^[A-Z]{2}[0-9]{6}$" />
          </div>
        )}
        {form.type === 'RANGE' && (
          <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>Min value</label>
              <input type="number" style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, outline: 'none' }} value={form.config?.min ?? ''} onChange={setCfg('min')} />
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>Max value</label>
              <input type="number" style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, outline: 'none' }} value={form.config?.max ?? ''} onChange={setCfg('max')} />
            </div>
          </div>
        )}
        {form.type === 'REF_INTEGRITY' && (
          <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>Reference schema</label>
              <input style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, outline: 'none' }} value={form.config?.ref_schema ?? ''} onChange={setCfg('ref_schema')} placeholder="(optional)" />
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>Reference table</label>
              <input style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, outline: 'none' }} value={form.config?.ref_object ?? ''} onChange={setCfg('ref_object')} placeholder="e.g. categories" />
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>Reference column</label>
              <input style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 13, outline: 'none' }} value={form.config?.ref_column ?? ''} onChange={setCfg('ref_column')} placeholder="e.g. id" />
            </div>
            <div style={{ gridColumn: '1 / -1', fontSize: 11, color: 'var(--text-faint)' }}>
              Must be a table on this same connection — a real LEFT JOIN runs against it, not a cross-connection check.
            </div>
          </div>
        )}
        {form.type === 'SQL_CONDITION' && (
          <div style={{ marginTop: 12 }}>
            <label style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', display: 'block', marginBottom: 4 }}>SQL boolean expression every valid row must satisfy</label>
            <textarea style={{ width: '100%', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 7, fontSize: 12, fontFamily: 'var(--font-mono)', outline: 'none', resize: 'vertical', minHeight: 72 }} value={form.config?.sql ?? ''} onChange={setCfg('sql')} placeholder="total_amount > 0 AND status <> 'void'" />
          </div>
        )}
      </Modal>
    </>
  );
}
