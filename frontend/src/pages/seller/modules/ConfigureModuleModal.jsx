/**
 * ConfigureModuleModal — the "MODULE LICENSE CONFIGURATION" form from the
 * spec (Enabled / License Type / Maximum Users / Monthly Credits /
 * Maximum Documents / Expiry), scoped to one customer + one module. Hits
 * PUT /api/seller/modules/customers/{id}/{module} for real — server-side
 * enforces "cannot enable if suspended / subscription expired / missing
 * dependency", surfaced here as a plain error banner.
 */
import { useEffect, useState } from 'react';
import { Modal, Button } from '@/components/common/index.js';
import { moduleApi } from '@/services/api/seller/moduleApi.js';
import './purchased-modules.css';

const LICENSE_TYPES = ['TRIAL', 'SUBSCRIPTION', 'PER_USER', 'PER_DOCUMENT', 'PER_RECORD', 'USAGE_BASED', 'UNLIMITED', 'CUSTOM'];
const TOKEN_PERIODS = ['DAILY', 'MONTHLY', 'BILLING_CYCLE', 'TOTAL'];
// The only ConnectionType values that are actually AI_PROVIDER-category
// connectors in this codebase (see models/connection.py) — the entitlement
// check (ai_connection_access_service._has_active_ai_entitlement) matches
// bundle entries against Connection.connection_type.value, so offering any
// other type here would be a checkbox that could never match anything.
const AI_PROVIDERS = ['OPENAI', 'ANTHROPIC', 'OLLAMA'];

export function ConfigureModuleModal({ customerId, item, onClose, onSaved }) {
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const isAiConnection = item?.module === 'AI_CONNECTION';

  useEffect(() => {
    if (!item) { setForm(null); return; }
    setForm({
      enabled: item.enabled,
      license_type: item.license_type || 'SUBSCRIPTION',
      max_users: item.max_users ?? '',
      monthly_credits: item.monthly_credits ?? '',
      max_documents: item.max_documents ?? '',
      expiry_date: item.expiry_date ? item.expiry_date.slice(0, 10) : '',
      max_tokens: item.max_tokens ?? '',
      token_period: item.token_period || 'MONTHLY',
      ai_provider_bundle: item.ai_provider_bundle ?? [],
      ai_rate_limit: item.ai_rate_limit ?? '',
      ai_concurrency_limit: item.ai_concurrency_limit ?? '',
    });
    setError('');
  }, [item]);

  if (!item || !form) return null;
  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const toggleProvider = (provider) => setForm((f) => ({
    ...f,
    ai_provider_bundle: f.ai_provider_bundle.includes(provider)
      ? f.ai_provider_bundle.filter((p) => p !== provider)
      : [...f.ai_provider_bundle, provider],
  }));

  const handleSubmit = async () => {
    setSaving(true);
    setError('');
    try {
      await moduleApi.setModule(customerId, item.module, {
        enabled: form.enabled,
        license_type: form.license_type,
        max_users: form.max_users === '' ? null : Number(form.max_users),
        monthly_credits: form.monthly_credits === '' ? null : Number(form.monthly_credits),
        max_documents: form.max_documents === '' ? null : Number(form.max_documents),
        expiry_date: form.expiry_date ? new Date(form.expiry_date).toISOString() : null,
        ...(isAiConnection ? {
          max_tokens: form.max_tokens === '' ? null : Number(form.max_tokens),
          token_period: form.token_period,
          // Empty selection = "all providers" (see CustomerModule.ai_provider_bundle's
          // own docstring: None/empty means unrestricted) rather than "none".
          ai_provider_bundle: form.ai_provider_bundle.length ? form.ai_provider_bundle : null,
          ai_rate_limit: form.ai_rate_limit === '' ? null : Number(form.ai_rate_limit),
          ai_concurrency_limit: form.ai_concurrency_limit === '' ? null : Number(form.ai_concurrency_limit),
        } : {}),
      });
      onSaved?.();
    } catch (err) {
      setError(err?.message ?? 'Could not save this module configuration.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open onClose={onClose} title={`Configure ${item.label}`} size="md"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} loading={saving}>Save</Button>
      </>}>
      {error && <div className="pm-form-error">{error}</div>}
      {item.dependencies.length > 0 && (
        <p className="pm-modal-summary">Depends on: <strong>{item.dependencies.join(', ')}</strong> {item.dependencies_met ? '(met)' : '(not met — enabling will fail)'}</p>
      )}
      <div className="pm-field-grid">
        <div className="pm-field">
          <label className="pm-field-label">Enabled</label>
          <select className="pm-input" value={form.enabled ? 'yes' : 'no'} onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.value === 'yes' }))}>
            <option value="no">No</option>
            <option value="yes">Yes</option>
          </select>
        </div>
        <div className="pm-field">
          <label className="pm-field-label">License Type</label>
          <select className="pm-input" value={form.license_type} onChange={set('license_type')}>
            {LICENSE_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
          </select>
        </div>
        <div className="pm-field">
          <label className="pm-field-label">Maximum Users</label>
          <input type="number" className="pm-input" value={form.max_users} onChange={set('max_users')} placeholder="Unlimited" />
        </div>
        <div className="pm-field">
          <label className="pm-field-label">Monthly Credits</label>
          <input type="number" className="pm-input" value={form.monthly_credits} onChange={set('monthly_credits')} placeholder="Unlimited" />
        </div>
        <div className="pm-field">
          <label className="pm-field-label">Maximum Documents</label>
          <input type="number" className="pm-input" value={form.max_documents} onChange={set('max_documents')} placeholder="Unlimited" />
        </div>
        <div className="pm-field">
          <label className="pm-field-label">Expiry</label>
          <input type="date" className="pm-input" value={form.expiry_date} onChange={set('expiry_date')} />
        </div>
      </div>

      {isAiConnection && (
        <>
          <div className="pm-section-title" style={{ marginTop: 16 }}>AI Connection Limits</div>
          <div className="pm-field-grid">
            <div className="pm-field">
              <label className="pm-field-label">Token Budget</label>
              <input type="number" className="pm-input" value={form.max_tokens} onChange={set('max_tokens')} placeholder="Unlimited" />
            </div>
            <div className="pm-field">
              <label className="pm-field-label">Token Period</label>
              <select className="pm-input" value={form.token_period} onChange={set('token_period')}>
                {TOKEN_PERIODS.map((p) => <option key={p} value={p}>{p.replace(/_/g, ' ')}</option>)}
              </select>
            </div>
            <div className="pm-field">
              <label className="pm-field-label">Rate Limit (req/min)</label>
              <input type="number" className="pm-input" value={form.ai_rate_limit} onChange={set('ai_rate_limit')} placeholder="Unlimited" />
            </div>
            <div className="pm-field">
              <label className="pm-field-label">Concurrency Limit</label>
              <input type="number" className="pm-input" value={form.ai_concurrency_limit} onChange={set('ai_concurrency_limit')} placeholder="Unlimited" />
            </div>
            <div className="pm-field pm-field-full">
              <label className="pm-field-label">Allowed AI Providers</label>
              <div className="pm-checkbox-row">
                {AI_PROVIDERS.map((p) => (
                  <label key={p} className="pm-checkbox-item">
                    <input type="checkbox" checked={form.ai_provider_bundle.includes(p)} onChange={() => toggleProvider(p)} />
                    {p}
                  </label>
                ))}
              </div>
              <p className="pm-field-hint">None checked = all providers allowed.</p>
            </div>
          </div>
        </>
      )}
    </Modal>
  );
}
