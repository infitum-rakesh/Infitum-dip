/**
 * InvoiceSettings — /app/invoice-intel/settings
 * INV17: the four admin surfaces the phased plan bundles together —
 * Rules, Mappings, Learning, Analytics — each a thin, genuinely-wired UI
 * over real backend state, nothing display-only:
 *
 *   Rules     — GET/PUT /invoice-rules + POST .../toggle (new INV17
 *               backend work: invoice_validation_service.publish_rule()/
 *               set_rule_enabled(), versioned same as mapping below).
 *               Every row here is a real ApInvoiceValidationRule that
 *               evaluate_invoice() interprets on the very next invoice.
 *   Mappings  — GET/PUT /invoice-mappings (INV9) — canonical field -> ERP
 *               field mapping, versioned, per ERP connection.
 *   Learning  — GET /invoices/learning/records + POST .../approve (INV13)
 *               — real ApInvoiceLearningRecord rows captured from actual
 *               corrections.
 *   Analytics — GET /invoices/learning/metrics + recurring-corrections
 *               by-field/by-vendor (INV13) — real, computed-on-read
 *               metrics, never a canned number.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  ListChecks, GitBranch, GraduationCap, BarChart3, Plus, Check, X, Loader2, Trash2,
} from 'lucide-react';
import {
  Button, StatusBadge, DataTable, EmptyState, Modal, PermissionGate, LoadingSpinner,
} from '@/components/common/index.js';
import { INVOICE_PERMISSIONS } from '@/utils/constants.js';
import { formatDateTime } from '@/utils/formatters.js';
import { invoiceApi } from '@/services/api/invoiceApi.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import './invoice-intelligence.css';

function apiErrorMessage(err, fallback) { return err?.response?.data?.detail || err?.message || fallback; }

const TABS = [
  { id: 'rules', label: 'Rules', icon: ListChecks },
  { id: 'mappings', label: 'Mappings', icon: GitBranch },
  { id: 'learning', label: 'Learning', icon: GraduationCap },
  { id: 'analytics', label: 'Analytics', icon: BarChart3 },
];

export default function InvoiceSettings() {
  const [tab, setTab] = useState('rules');

  return (
    <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_VIEW} showReason>
      <div className="invs-page">
        <div className="inv-header">
          <div>
            <h1 className="inv-title"><ListChecks size={22} /> Invoice Settings</h1>
            <p className="inv-subtitle">Validation rules, ERP field mapping, and the real learning/evaluation surfaces behind the decision engine.</p>
          </div>
        </div>

        <div className="invs-tab-nav">
          {TABS.map((t) => (
            <button key={t.id} className={`invs-tab-btn ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)}>
              <t.icon size={14} /> {t.label}
            </button>
          ))}
        </div>

        {tab === 'rules' && <RulesTab />}
        {tab === 'mappings' && <MappingsTab />}
        {tab === 'learning' && <LearningTab />}
        {tab === 'analytics' && <AnalyticsTab />}
      </div>
    </PermissionGate>
  );
}

// ── Rules ──────────────────────────────────────────────────────────

const CATEGORIES = ['SCHEMA', 'ARITHMETIC', 'REFERENCE', 'PO', 'DUPLICATE', 'POLICY', 'ERP'];
const SEVERITIES = ['WARNING', 'EXCEPTION'];
const CATEGORY_HINT = {
  SCHEMA: '{"required_fields": ["invoice_number", "vendor_name", "total"]}',
  ARITHMETIC: '{"lhs": "subtotal + tax - discount + freight", "rhs": "total", "tolerance": 0.01}',
  REFERENCE: '{}',
  PO: '{"pattern": "^[A-Za-z0-9-]{3,30}$"}',
  DUPLICATE: '{}',
  POLICY: '{"numerator": "tax", "denominator": "subtotal", "min_pct": 0, "max_pct": 30}',
  ERP: '{}',
};
const EMPTY_RULE_DRAFT = { name: '', category: 'SCHEMA', severity: 'EXCEPTION', message: '', enabled: true, configText: '{}' };

function RulesTab() {
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [draft, setDraft] = useState(EMPTY_RULE_DRAFT);
  const [formError, setFormError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [togglingId, setTogglingId] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    invoiceApi.listRules()
      .then(setRules)
      .catch((err) => setError(apiErrorMessage(err, 'Could not load validation rules.')))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const openNew = () => { setDraft(EMPTY_RULE_DRAFT); setFormError(null); setModalOpen(true); };
  const openEdit = (rule) => {
    setDraft({
      name: rule.name, category: rule.category, severity: rule.severity,
      message: rule.message, enabled: rule.enabled, configText: JSON.stringify(rule.config, null, 2),
    });
    setFormError(null);
    setModalOpen(true);
  };

  const handleSave = async () => {
    let config;
    try {
      config = JSON.parse(draft.configText || '{}');
    } catch {
      setFormError('Config must be valid JSON.');
      return;
    }
    if (!draft.name.trim() || !draft.message.trim()) {
      setFormError('Name and message are both required.');
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      await invoiceApi.publishRule({
        name: draft.name.trim(), category: draft.category, severity: draft.severity,
        config, message: draft.message.trim(), enabled: draft.enabled,
      });
      setModalOpen(false);
      load();
    } catch (err) {
      setFormError(apiErrorMessage(err, 'Could not save this rule.'));
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = async (rule) => {
    setTogglingId(rule.id);
    try {
      await invoiceApi.toggleRule(rule.id, !rule.enabled);
      load();
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not toggle this rule.'));
    } finally {
      setTogglingId(null);
    }
  };

  const columns = [
    { key: 'name', header: 'Name', sortable: true, render: (v, r) => <button className="inv-invoice-link" onClick={() => openEdit(r)}>{v}</button> },
    { key: 'category', header: 'Category', render: (v) => <StatusBadge variant="info" size="sm">{v}</StatusBadge> },
    { key: 'severity', header: 'Severity', render: (v) => <StatusBadge variant={v === 'EXCEPTION' ? 'danger' : 'warning'} dot>{v}</StatusBadge> },
    { key: 'message', header: 'Message' },
    { key: 'version', header: 'Version', render: (v) => `v${v}` },
    {
      key: 'enabled', header: 'Enabled',
      render: (v, r) => (
        <button className={`invs-toggle-btn ${v ? 'on' : 'off'}`} disabled={togglingId === r.id} onClick={(e) => { e.stopPropagation(); handleToggle(r); }}>
          {togglingId === r.id ? <Loader2 size={11} className="spin" /> : v ? <Check size={11} /> : <X size={11} />} {v ? 'Enabled' : 'Disabled'}
        </button>
      ),
    },
    {
      key: 'scope', header: 'Scope',
      render: (_v, r) => (r.organization_id == null ? <span style={{ color: 'var(--text-faint)' }}>Platform default</span> : 'Org override'),
    },
  ];

  return (
    <div className="invs-panel">
      <div className="invs-panel-hdr">
        <div>
          <div className="invs-panel-title">Validation Rules</div>
          <p className="invs-panel-desc">SCHEMA/ARITHMETIC/REFERENCE/PO/DUPLICATE/POLICY/ERP rules the decision engine actually evaluates. Editing publishes a new version — past decisions stay traceable to the version that was current when they ran.</p>
        </div>
        <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_EDIT} fallback={null}>
          <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={openNew}>New Rule</Button>
        </PermissionGate>
      </div>
      <DataTable
        columns={columns}
        data={rules}
        loading={loading}
        error={error}
        emptyState={<EmptyState icon={<ListChecks size={28} />} title="No rules yet" description="Rules seed automatically the first time an invoice is processed." />}
      />

      <Modal
        open={modalOpen} onClose={() => setModalOpen(false)}
        title={rules.some((r) => r.name === draft.name) ? `Edit Rule — ${draft.name}` : 'New Rule'}
        size="lg"
        footer={<><Button variant="secondary" onClick={() => setModalOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} onClick={handleSave}>Publish</Button></>}
      >
        {formError && <div className="invs-error-text" style={{ marginBottom: 10 }}>{formError}</div>}
        <div className="invs-form-grid">
          <div className="invs-form-field">
            <label className="invs-label">Name</label>
            <input className="invs-input" value={draft.name} onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))} placeholder="e.g. Header Total Reconciles" />
          </div>
          <div className="invs-form-field">
            <label className="invs-label">Category</label>
            <select className="invs-select2" value={draft.category} onChange={(e) => setDraft((d) => ({ ...d, category: e.target.value, configText: CATEGORY_HINT[e.target.value] }))}>
              {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div className="invs-form-field">
            <label className="invs-label">Severity</label>
            <select className="invs-select2" value={draft.severity} onChange={(e) => setDraft((d) => ({ ...d, severity: e.target.value }))}>
              {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className="invs-form-field">
            <label className="invs-label">Enabled</label>
            <div className="invs-checkbox-row" style={{ height: 36 }}>
              <input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft((d) => ({ ...d, enabled: e.target.checked }))} /> Rule is active
            </div>
          </div>
          <div className="invs-form-field full">
            <label className="invs-label">Message (shown on a FAIL/WARNING result)</label>
            <input className="invs-input" value={draft.message} onChange={(e) => setDraft((d) => ({ ...d, message: e.target.value }))} placeholder="Business-language explanation of what failed" />
          </div>
          <div className="invs-form-field full">
            <label className="invs-label">Config (JSON — shape depends on category, see hint)</label>
            <textarea className="invs-textarea" value={draft.configText} onChange={(e) => setDraft((d) => ({ ...d, configText: e.target.value }))} spellCheck={false} />
          </div>
        </div>
      </Modal>
    </div>
  );
}

// ── Mappings ───────────────────────────────────────────────────────

function MappingsTab() {
  const [erpConnections, setErpConnections] = useState([]);
  const [connectionId, setConnectionId] = useState('');
  const [rows, setRows] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [saveOk, setSaveOk] = useState(false);

  useEffect(() => {
    connectionApi.list({ category: 'APPLICATION', page_size: 200 })
      .then((r) => {
        const erp = (r.items ?? []).filter((c) => c.connection_type === 'ORACLE_ERP_CLOUD');
        setErpConnections(erp);
        if (erp.length) setConnectionId(String(erp[0].id));
      })
      .catch(() => {});
  }, []);

  const load = useCallback((connId) => {
    if (!connId) return;
    setLoading(true);
    setError(null);
    setSaveOk(false);
    invoiceApi.getMapping(connId)
      .then((data) => setRows(data.map((r) => ({ ...r, transformText: JSON.stringify(r.transform_config || {}) }))))
      .catch((err) => setError(apiErrorMessage(err, 'Could not load the ERP mapping.')))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(connectionId); }, [connectionId, load]);

  const updateRow = (idx, patch) => setRows((rs) => rs.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  const addRow = () => setRows((rs) => [...rs, { canonical_field: '', erp_field: '', is_line_mapping: false, default_value: '', transformText: '{}' }]);
  const removeRow = (idx) => setRows((rs) => rs.filter((_, i) => i !== idx));

  const currentVersion = rows?.length ? Math.max(...rows.map((r) => r.version ?? 0)) : null;

  const handlePublish = async () => {
    setSaveError(null);
    let parsedRows;
    try {
      parsedRows = rows.map((r) => ({
        canonical_field: r.canonical_field.trim(),
        erp_field: r.erp_field.trim(),
        is_line_mapping: !!r.is_line_mapping,
        default_value: r.default_value || null,
        transform_config: JSON.parse(r.transformText || '{}'),
      }));
    } catch {
      setSaveError('One of the transform_config fields is not valid JSON.');
      return;
    }
    if (parsedRows.some((r) => !r.canonical_field || !r.erp_field)) {
      setSaveError('Every row needs both a canonical field and an ERP field.');
      return;
    }
    setSaving(true);
    try {
      await invoiceApi.publishMapping({ erp_connection_id: Number(connectionId), rows: parsedRows });
      setSaveOk(true);
      load(connectionId);
    } catch (err) {
      setSaveError(apiErrorMessage(err, 'Could not publish this mapping.'));
    } finally {
      setSaving(false);
    }
  };

  if (!erpConnections.length) {
    return (
      <div className="invs-panel">
        <EmptyState icon={<GitBranch size={28} />} title="No Oracle ERP connection configured" description="Add an ORACLE_ERP_CLOUD connection under Connections to manage its field mapping here." />
      </div>
    );
  }

  return (
    <div className="invs-panel">
      <div className="invs-panel-hdr">
        <div>
          <div className="invs-panel-title">ERP Field Mapping</div>
          <p className="invs-panel-desc">Canonical invoice field → ERP request field, per connection. Publishing replaces the whole mapping with a new version — invoice_erp_mapping_service builds every real ERP payload from exactly this table.</p>
        </div>
        <select className="inv-select" value={connectionId} onChange={(e) => setConnectionId(e.target.value)}>
          {erpConnections.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </div>

      {loading && <div style={{ padding: 30, textAlign: 'center' }}><LoadingSpinner /></div>}
      {error && <p className="invs-error-text">{error}</p>}

      {!loading && rows && (
        <>
          <p style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)', marginBottom: 10 }}>
            Current version: <strong>v{currentVersion}</strong> · {rows.length} field{rows.length === 1 ? '' : 's'}
          </p>
          <div style={{ overflowX: 'auto' }}>
            <table className="invs-map-table">
              <thead>
                <tr>
                  <th>Canonical Field</th><th>ERP Field</th><th>Line Mapping</th><th>Default Value</th><th>Transform (JSON)</th><th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    <td><input type="text" value={r.canonical_field} onChange={(e) => updateRow(i, { canonical_field: e.target.value })} /></td>
                    <td><input type="text" value={r.erp_field} onChange={(e) => updateRow(i, { erp_field: e.target.value })} /></td>
                    <td style={{ textAlign: 'center' }}><input type="checkbox" checked={!!r.is_line_mapping} onChange={(e) => updateRow(i, { is_line_mapping: e.target.checked })} /></td>
                    <td><input type="text" value={r.default_value ?? ''} onChange={(e) => updateRow(i, { default_value: e.target.value })} /></td>
                    <td><input type="text" value={r.transformText} onChange={(e) => updateRow(i, { transformText: e.target.value })} /></td>
                    <td><button className="doc-icon-btn" title="Remove row" onClick={() => removeRow(i)}><Trash2 size={13} /></button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 14, alignItems: 'center' }}>
            <Button variant="secondary" size="sm" icon={<Plus size={13} />} onClick={addRow}>Add Field</Button>
            <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_EDIT} fallback={null}>
              <Button variant="primary" size="sm" loading={saving} onClick={handlePublish}>Publish New Version</Button>
            </PermissionGate>
            {saveOk && <StatusBadge variant="success" dot>Published</StatusBadge>}
          </div>
          {saveError && <p className="invs-error-text">{saveError}</p>}
        </>
      )}
    </div>
  );
}

// ── Learning ───────────────────────────────────────────────────────

function LearningTab() {
  const [records, setRecords] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [fieldFilter, setFieldFilter] = useState('');
  const [approvedOnly, setApprovedOnly] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [approvingId, setApprovingId] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    invoiceApi.listLearningRecords({ field_name: fieldFilter || undefined, approved_only: approvedOnly, page, page_size: pageSize })
      .then((r) => { setRecords(r.items); setTotal(r.total); })
      .catch((err) => setError(apiErrorMessage(err, 'Could not load learning records.')))
      .finally(() => setLoading(false));
  }, [fieldFilter, approvedOnly, page, pageSize]);

  useEffect(() => { load(); }, [load]);

  const handleApprove = async (id) => {
    setApprovingId(id);
    try {
      await invoiceApi.approveLearningRecord(id);
      load();
    } catch (err) {
      window.alert(apiErrorMessage(err, 'Could not approve this record.'));
    } finally {
      setApprovingId(null);
    }
  };

  const columns = [
    { key: 'invoice_id', header: 'Invoice', render: (v) => <a className="inv-invoice-link" href={`/app/invoice-intel/${v}`}>#{v}</a> },
    { key: 'field_name', header: 'Field', render: (v) => <span style={{ fontFamily: 'monospace' }}>{v}</span> },
    { key: 'original_value', header: 'Original', render: (v) => v || <em className="text-muted">missing</em> },
    { key: 'corrected_value', header: 'Corrected', render: (v) => v || '—' },
    { key: 'document_layout_hint', header: 'Vendor', render: (v) => v || '—' },
    { key: 'created_at', header: 'Corrected On', render: (v) => formatDateTime(v) },
    {
      key: 'approved_for_learning', header: 'Status',
      render: (v, r) => v
        ? <StatusBadge variant="success" dot>Approved</StatusBadge>
        : (
          <PermissionGate {...INVOICE_PERMISSIONS.INVOICE_EDIT} fallback={<StatusBadge variant="gray" dot>Pending</StatusBadge>}>
            <Button variant="secondary" size="sm" loading={approvingId === r.id} onClick={() => handleApprove(r.id)}>Approve</Button>
          </PermissionGate>
        ),
    },
  ];

  return (
    <div className="invs-panel">
      <div className="invs-panel-hdr">
        <div>
          <div className="invs-panel-title">Learning Records</div>
          <p className="invs-panel-desc">Every real field correction captured from Review Queue / Evidence-tab corrections, re-indexed by field and vendor. Approving a record marks it curated for future reference — this build never feeds these back into extraction automatically (no retraining infrastructure exists yet).</p>
        </div>
      </div>
      <div className="inv-toolbar" style={{ marginBottom: 14 }}>
        <input className="invs-input" style={{ maxWidth: 220 }} placeholder="Filter by field name…" value={fieldFilter} onChange={(e) => { setFieldFilter(e.target.value); setPage(1); }} />
        <label className="invs-checkbox-row">
          <input type="checkbox" checked={approvedOnly} onChange={(e) => { setApprovedOnly(e.target.checked); setPage(1); }} /> Approved only
        </label>
      </div>
      <DataTable
        columns={columns}
        data={records}
        loading={loading}
        error={error}
        pagination={{ page, pageSize, total, onChange: setPage }}
        emptyState={<EmptyState icon={<GraduationCap size={28} />} title="No corrections captured yet" description="Corrections made from an invoice's Evidence tab or Review Queue will show up here." />}
      />
    </div>
  );
}

// ── Analytics ──────────────────────────────────────────────────────

function AnalyticsTab() {
  const [metrics, setMetrics] = useState(null);
  const [byField, setByField] = useState([]);
  const [byVendor, setByVendor] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    Promise.all([
      invoiceApi.getLearningMetrics(),
      invoiceApi.getRecurringCorrectionsByField(10),
      invoiceApi.getRecurringCorrectionsByVendor(10),
    ])
      .then(([m, f, v]) => { setMetrics(m); setByField(f); setByVendor(v); })
      .catch((err) => setError(apiErrorMessage(err, 'Could not load evaluation metrics.')))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div style={{ padding: 40, textAlign: 'center' }}><LoadingSpinner /></div>;
  if (error) return <div className="invs-panel"><p className="invs-error-text">{error}</p></div>;

  const kpis = [
    { label: 'Decided Invoices', value: metrics.total_decided },
    { label: 'Straight-Through Rate', value: metrics.straight_through_rate_pct != null ? `${metrics.straight_through_rate_pct}%` : '—' },
    { label: 'Exception Rate', value: metrics.exception_rate_pct != null ? `${metrics.exception_rate_pct}%` : '—' },
    { label: 'Field Accuracy', value: metrics.field_accuracy_pct != null ? `${metrics.field_accuracy_pct}%` : '—' },
    { label: 'Total Corrections', value: metrics.total_corrections },
  ];

  const maxFieldCount = Math.max(1, ...byField.map((f) => f.count));
  const maxVendorCount = Math.max(1, ...byVendor.map((v) => v.count));

  return (
    <div>
      <div className="invs-kpis">
        {kpis.map((k) => (
          <div key={k.label} className="invs-kpi-card">
            <div className="invs-kpi-val">{k.value}</div>
            <div className="invs-kpi-label">{k.label}</div>
          </div>
        ))}
      </div>

      <div className="invs-two-col">
        <div className="invs-panel">
          <div className="invs-panel-title" style={{ marginBottom: 14 }}>Recurring Corrections — by Field</div>
          {byField.length ? (
            <div className="invs-bar-list">
              {byField.map((f) => (
                <div key={f.field_name} className="invs-bar-row">
                  <div className="invs-bar-row-top"><span style={{ fontFamily: 'monospace' }}>{f.field_name}</span><span>{f.count}</span></div>
                  <div className="invs-bar-track"><div className="invs-bar-fill" style={{ width: `${(f.count / maxFieldCount) * 100}%` }} /></div>
                </div>
              ))}
            </div>
          ) : <p className="invd-empty-note">No recurring corrections yet.</p>}
        </div>

        <div className="invs-panel">
          <div className="invs-panel-title" style={{ marginBottom: 14 }}>Recurring Corrections — by Vendor</div>
          {byVendor.length ? (
            <div className="invs-bar-list">
              {byVendor.map((v) => (
                <div key={v.vendor_id} className="invs-bar-row">
                  <div className="invs-bar-row-top"><span>{v.vendor_name}</span><span>{v.count}</span></div>
                  <div className="invs-bar-track"><div className="invs-bar-fill" style={{ width: `${(v.count / maxVendorCount) * 100}%` }} /></div>
                </div>
              ))}
            </div>
          ) : <p className="invd-empty-note">No recurring corrections yet.</p>}
        </div>
      </div>
    </div>
  );
}
