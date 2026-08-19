/**
 * PlanModal — Create/Edit a real plan on the Plans tab. Wraps
 * planApi.create()/update() (subscriptionApi.js, built Phase 3) — finally
 * surfaced in a UI here. `plan` is the PlanOut row to edit, or null to
 * create a new one.
 */
import { useEffect, useState } from 'react';
import { Modal, Button } from '@/components/common/index.js';
import { MODULE_META } from '@/utils/permissions.js';
import { MODULES } from '@/utils/constants.js';
import { planApi } from '@/services/api/seller/subscriptionApi.js';
import './seller-settings.css';

const BILLING_CYCLES = ['MONTHLY', 'QUARTERLY', 'ANNUAL', 'CUSTOM'];
const ALL_MODULES = Object.values(MODULES);

const EMPTY = {
  code: '', name: '', description: '', modules: [],
  limits: { users: 10, documents: 1000, ai_calls: 5000, storage_gb: 10 },
  price: 0, currency: 'USD', billing_cycle: 'MONTHLY', trial_days: '',
};

export function PlanModal({ open, plan, onClose, onSaved }) {
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    if (plan) {
      setForm({
        code: plan.code, name: plan.name, description: plan.description ?? '',
        modules: plan.modules, limits: { ...plan.limits },
        price: plan.price, currency: plan.currency, billing_cycle: plan.billing_cycle,
        trial_days: plan.trial_days ?? '',
      });
    } else {
      setForm(EMPTY);
    }
    setError('');
  }, [open, plan]);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const setLimit = (key) => (e) => setForm((f) => ({ ...f, limits: { ...f.limits, [key]: Number(e.target.value) } }));
  const toggleModule = (m) => setForm((f) => ({
    ...f, modules: f.modules.includes(m) ? f.modules.filter((x) => x !== m) : [...f.modules, m],
  }));

  const handleSubmit = async () => {
    if (!form.name.trim() || (!plan && !form.code.trim())) {
      setError('Name (and Code, for a new plan) are required.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const body = {
        name: form.name, description: form.description || null, modules: form.modules,
        limits: form.limits, price: Number(form.price), currency: form.currency,
        billing_cycle: form.billing_cycle, trial_days: form.trial_days === '' ? null : Number(form.trial_days),
      };
      if (plan) {
        await planApi.update(plan.id, body);
      } else {
        await planApi.create({ ...body, code: form.code.trim().toLowerCase().replace(/\s+/g, '_') });
      }
      onSaved?.();
    } catch (err) {
      setError(err?.message ?? 'Could not save the plan.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={plan ? `Edit Plan — ${plan.name}` : 'New Plan'}
      size="lg"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} loading={saving}>{plan ? 'Save Changes' : 'Create Plan'}</Button>
      </>}
    >
      {error && <div className="sst-form-error">{error}</div>}

      <div className="sst-grid">
        {!plan && (
          <div className="sst-field">
            <label className="sst-field-label">Code</label>
            <input className="sst-input" value={form.code} onChange={set('code')} placeholder="e.g. starter" />
          </div>
        )}
        <div className="sst-field">
          <label className="sst-field-label">Name</label>
          <input className="sst-input" value={form.name} onChange={set('name')} placeholder="e.g. Starter" />
        </div>
        <div className="sst-field" style={{ gridColumn: plan ? 'span 2' : undefined }}>
          <label className="sst-field-label">Description</label>
          <input className="sst-input" value={form.description} onChange={set('description')} placeholder="Shown to customers" />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Price</label>
          <input type="number" className="sst-input" value={form.price} onChange={set('price')} min={0} step={1} />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Currency</label>
          <input className="sst-input" value={form.currency} onChange={set('currency')} />
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Billing Cycle</label>
          <select className="sst-input" value={form.billing_cycle} onChange={set('billing_cycle')}>
            {BILLING_CYCLES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div className="sst-field">
          <label className="sst-field-label">Trial Days</label>
          <input type="number" className="sst-input" value={form.trial_days} onChange={set('trial_days')} placeholder="none" min={0} />
        </div>
      </div>

      <div className="sst-field" style={{ marginTop: 14 }}>
        <label className="sst-field-label">Modules Included</label>
        <div className="sst-module-picker">
          {ALL_MODULES.map((m) => (
            <label key={m} className="sst-module-picker-row">
              <input type="checkbox" checked={form.modules.includes(m)} onChange={() => toggleModule(m)} />
              {MODULE_META[m]?.icon} {MODULE_META[m]?.label ?? m}
            </label>
          ))}
        </div>
      </div>

      <div className="sst-field" style={{ marginTop: 14 }}>
        <label className="sst-field-label">Limits</label>
        <div className="sst-limits-grid">
          <div className="sst-field">
            <label className="sst-field-label">Users</label>
            <input type="number" className="sst-input" value={form.limits.users} onChange={setLimit('users')} />
          </div>
          <div className="sst-field">
            <label className="sst-field-label">Documents</label>
            <input type="number" className="sst-input" value={form.limits.documents} onChange={setLimit('documents')} />
          </div>
          <div className="sst-field">
            <label className="sst-field-label">AI Calls</label>
            <input type="number" className="sst-input" value={form.limits.ai_calls} onChange={setLimit('ai_calls')} />
          </div>
          <div className="sst-field">
            <label className="sst-field-label">Storage (GB)</label>
            <input type="number" className="sst-input" value={form.limits.storage_gb} onChange={setLimit('storage_gb')} />
          </div>
        </div>
        <p className="sst-limit-note">Use -1 on any limit for unlimited (matches the seeded Enterprise plan's convention).</p>
      </div>
    </Modal>
  );
}
