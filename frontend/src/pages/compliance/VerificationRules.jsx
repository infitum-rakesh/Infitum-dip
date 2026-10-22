/**
 * VerificationRules — /app/india-verification/rules (Phase Group 4,
 * IV19/IV22). Required-check-sets per business process + freshness
 * policy. Publishing a rule with the same name creates a new version
 * and retires the prior one (never mutated in place) — same discipline
 * as Approval Rules.
 */
import { useEffect, useState } from 'react';
import { ListChecks, Plus, PlayCircle, AlertTriangle, CheckCircle2, XCircle } from 'lucide-react';
import { Button, Modal, StatusBadge, EmptyState, LoadingSpinner } from '@/components/common/index.js';
import indiaVerificationApi from '@/services/api/indiaVerificationApi.js';
import './compliance.css';

function apiErrorMessage(err, fallback) {
  return err?.data?.detail || err?.response?.data?.detail || err?.message || fallback;
}

const CHECK_TYPES = ['PAN', 'GSTIN', 'TAN', 'UDYAM', 'AADHAAR', 'MCA', 'MOA'];
const EMPTY_FORM = { name: '', description: '', businessProcess: '', subjectType: '', requiredCheckTypes: [], freshnessDays: '', priority: 100 };

export default function VerificationRules() {
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  const [simForm, setSimForm] = useState({ businessProcess: '', subjectType: '' });
  const [simResult, setSimResult] = useState(null);
  const [simulating, setSimulating] = useState(false);
  const [simError, setSimError] = useState(null);

  const load = () => {
    setLoading(true);
    indiaVerificationApi.listRules()
      .then((r) => { setRules(r); setLoading(false); })
      .catch((err) => { setError(apiErrorMessage(err, 'Could not load rules.')); setLoading(false); });
  };
  useEffect(() => { load(); }, []);

  const openCreate = () => { setForm(EMPTY_FORM); setSaveError(null); setFormOpen(true); };

  const toggleCheckType = (t) => setForm((f) => ({
    ...f, requiredCheckTypes: f.requiredCheckTypes.includes(t) ? f.requiredCheckTypes.filter((x) => x !== t) : [...f.requiredCheckTypes, t],
  }));

  const canSave = () => form.name.trim() && form.businessProcess.trim() && form.requiredCheckTypes.length > 0;

  const handleSave = async () => {
    if (!canSave()) return;
    setSaving(true);
    setSaveError(null);
    try {
      await indiaVerificationApi.publishRule({
        name: form.name.trim(), description: form.description.trim() || undefined, businessProcess: form.businessProcess.trim(),
        subjectType: form.subjectType.trim() || undefined, requiredCheckTypes: form.requiredCheckTypes,
        freshnessDays: form.freshnessDays ? Number(form.freshnessDays) : undefined, priority: Number(form.priority) || 100,
      });
      setFormOpen(false);
      load();
    } catch (err) {
      setSaveError(apiErrorMessage(err, 'Could not publish this rule.'));
    } finally {
      setSaving(false);
    }
  };

  const toggleStatus = async (rule) => {
    const next = rule.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE';
    await indiaVerificationApi.setRuleStatus(rule.id, next);
    load();
  };

  const handleSimulate = async () => {
    if (!simForm.businessProcess.trim()) return;
    setSimulating(true);
    setSimError(null);
    setSimResult(null);
    try {
      const result = await indiaVerificationApi.simulateRule({ businessProcess: simForm.businessProcess.trim(), subjectType: simForm.subjectType.trim() || undefined });
      setSimResult(result);
    } catch (err) {
      setSimError(apiErrorMessage(err, 'Simulation failed.'));
    } finally {
      setSimulating(false);
    }
  };

  return (
    <div className="iv-page">
      <div className="iv-header">
        <div>
          <h1 className="iv-title"><ListChecks size={20} /> Verification Rules</h1>
          <p className="iv-subtitle">
            Required-check-sets per business process, plus a freshness policy for how long a verified result stays
            valid before it needs re-checking. Publishing a rule with an existing name creates a new version — the
            prior one is retired, never mutated.
          </p>
        </div>
        <Button icon={<Plus size={14} />} onClick={openCreate}>New Rule</Button>
      </div>

      {error && <div className="iv-banner-error" style={{ marginBottom: 16 }}>{error}</div>}

      <div className="iv-list-table-panel" style={{ marginBottom: 24 }}>
        {loading ? (
          <LoadingSpinner />
        ) : rules.length === 0 ? (
          <EmptyState icon={<ListChecks size={28} />} title="No rules yet" description="Create one to define which checks a business process requires." />
        ) : (
          <table className="iv-mini-table">
            <thead>
              <tr>
                <th>Name</th><th>Business Process</th><th>Subject Type</th><th>Required Checks</th>
                <th>Freshness</th><th>Priority</th><th>Version</th><th>Status</th><th></th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td>
                  <td><code>{r.business_process}</code></td>
                  <td>{r.subject_type ?? <em>any</em>}</td>
                  <td>{r.required_check_types.join(', ')}</td>
                  <td>{r.freshness_days ? `${r.freshness_days}d` : <em>never expires</em>}</td>
                  <td>{r.priority}</td>
                  <td>v{r.version}</td>
                  <td><StatusBadge variant={r.status === 'ACTIVE' ? 'success' : 'gray'} dot>{r.status}</StatusBadge></td>
                  <td><Button size="sm" variant="secondary" onClick={() => toggleStatus(r)}>{r.status === 'ACTIVE' ? 'Deactivate' : 'Activate'}</Button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="iv-build-panel">
        <div className="iv-section-title" style={{ marginBottom: 4 }}><PlayCircle size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />Simulate</div>
        <p className="iv-text-muted">See which rule would apply to a given business process / subject type, without needing a real case.</p>
        <div className="iv-build-row">
          <div className="iv-build-field">
            <label>Business Process</label>
            <input className="iv-inline-input" value={simForm.businessProcess} onChange={(e) => setSimForm((f) => ({ ...f, businessProcess: e.target.value }))} placeholder="e.g. VENDOR_ONBOARDING" />
          </div>
          <div className="iv-build-field">
            <label>Subject Type (optional)</label>
            <input className="iv-inline-input" value={simForm.subjectType} onChange={(e) => setSimForm((f) => ({ ...f, subjectType: e.target.value }))} placeholder="e.g. VENDOR" />
          </div>
          <Button icon={<PlayCircle size={14} />} loading={simulating} disabled={!simForm.businessProcess.trim()} onClick={handleSimulate}>Simulate</Button>
        </div>
        {simError && <div className="iv-banner-error">{simError}</div>}
        {simResult && (
          <div>
            <p className="iv-text-muted">{simResult.candidates_considered} candidate rule(s) considered.</p>
            {simResult.conflict && (
              <div className="iv-banner-error" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <AlertTriangle size={14} /> Conflict — rule(s) {simResult.conflicting_rule_ids.join(', ')} are tied; none auto-picked.
              </div>
            )}
            {!simResult.conflict && simResult.winner && (
              <div className="iv-banner-success" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <CheckCircle2 size={14} /> Winner: <strong>{simResult.winner.rule_name}</strong> (priority {simResult.winner.priority}, specificity {simResult.winner.specificity})
              </div>
            )}
            {!simResult.conflict && !simResult.winner && (
              <div className="iv-banner-info" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <XCircle size={14} /> No active rule matches.
              </div>
            )}
            {simResult.evaluations.length > 0 && (
              <table className="iv-mini-table" style={{ marginTop: 8 }}>
                <thead><tr><th>Rule</th><th>Matched</th><th>Priority</th><th>Specificity</th><th>Reason</th></tr></thead>
                <tbody>
                  {simResult.evaluations.map((e) => (
                    <tr key={e.rule_id}>
                      <td>{e.rule_name} (v{e.version})</td>
                      <td>{e.matched ? <StatusBadge variant="success">Matched</StatusBadge> : <StatusBadge variant="gray">No</StatusBadge>}</td>
                      <td>{e.priority}</td>
                      <td>{e.specificity}</td>
                      <td>{e.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>

      <Modal open={formOpen} onClose={() => setFormOpen(false)} title="New Verification Rule" size="md">
        {saveError && <div className="iv-banner-error" style={{ marginBottom: 12 }}>{saveError}</div>}
        <div className="iv-build-field" style={{ marginBottom: 12 }}>
          <label>Name</label>
          <input className="iv-inline-input" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="e.g. Vendor Onboarding — Full KYC" />
        </div>
        <div className="iv-build-field" style={{ marginBottom: 12 }}>
          <label>Description (optional)</label>
          <input className="iv-inline-input" value={form.description} onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} />
        </div>
        <div className="iv-build-row">
          <div className="iv-build-field">
            <label>Business Process</label>
            <input className="iv-inline-input" value={form.businessProcess} onChange={(e) => setForm((f) => ({ ...f, businessProcess: e.target.value }))} placeholder="e.g. VENDOR_ONBOARDING" />
          </div>
          <div className="iv-build-field">
            <label>Subject Type (optional — blank = any)</label>
            <input className="iv-inline-input" value={form.subjectType} onChange={(e) => setForm((f) => ({ ...f, subjectType: e.target.value }))} placeholder="e.g. VENDOR" />
          </div>
        </div>
        <div className="iv-build-field" style={{ marginBottom: 12 }}>
          <label>Required Checks</label>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {CHECK_TYPES.map((t) => (
              <label key={t} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 'var(--fs-sm)' }}>
                <input type="checkbox" checked={form.requiredCheckTypes.includes(t)} onChange={() => toggleCheckType(t)} />{t}
              </label>
            ))}
          </div>
        </div>
        <div className="iv-build-row">
          <div className="iv-build-field">
            <label>Freshness (days, optional — blank = never expires)</label>
            <input className="iv-inline-input" type="number" min="1" value={form.freshnessDays} onChange={(e) => setForm((f) => ({ ...f, freshnessDays: e.target.value }))} placeholder="e.g. 90" />
          </div>
          <div className="iv-build-field">
            <label>Priority (lower wins a tie)</label>
            <input className="iv-inline-input" type="number" value={form.priority} onChange={(e) => setForm((f) => ({ ...f, priority: e.target.value }))} />
          </div>
        </div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 8 }}>
          <Button variant="secondary" onClick={() => setFormOpen(false)}>Cancel</Button>
          <Button loading={saving} disabled={!canSave()} onClick={handleSave}>Publish</Button>
        </div>
      </Modal>
    </div>
  );
}
