/**
 * SubscriptionModals — Create / Edit / Change Plan, bundled together (same
 * pattern as Administration -> Users' UserActionModals.jsx). All three
 * hit real endpoints in backend/app/services/seller/subscription_service.py.
 */
import { useEffect, useState } from 'react';
import { Modal, Button } from '@/components/common/index.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import { subscriptionApi } from '@/services/api/seller/subscriptionApi.js';
import './subscriptions.css';

const BILLING_CYCLES = ['MONTHLY', 'ANNUAL', 'CUSTOM'];
const STATUS_OPTIONS = ['TRIAL', 'ACTIVE', 'EXPIRING', 'EXPIRED', 'SUSPENDED', 'CANCELLED'];

// ── Create ───────────────────────────────────────────────────────

const EMPTY_CREATE = {
  organization_id: '', plan_id: '', status: 'TRIAL', billing_cycle: 'MONTHLY',
  amount: '', currency: 'USD', auto_renew: false, start_date: '', end_date: '',
};

export function CreateSubscriptionModal({ open, onClose, plans, onCreated }) {
  const [customers, setCustomers] = useState([]);
  const [form, setForm] = useState(EMPTY_CREATE);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    customerApi.list({ page_size: 200 }).then((r) => setCustomers(r.items)).catch(() => {});
  }, [open]);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const handleClose = () => { setForm(EMPTY_CREATE); setError(''); onClose(); };

  const handleSubmit = async () => {
    if (!form.organization_id) { setError('Select a customer.'); return; }
    setSaving(true);
    setError('');
    try {
      await subscriptionApi.create({
        organization_id: Number(form.organization_id),
        plan_id: form.plan_id ? Number(form.plan_id) : null,
        status: form.status,
        billing_cycle: form.billing_cycle,
        amount: form.amount === '' ? null : Number(form.amount),
        currency: form.currency,
        auto_renew: form.auto_renew,
        start_date: form.start_date ? new Date(form.start_date).toISOString() : null,
        end_date: form.end_date ? new Date(form.end_date).toISOString() : null,
      });
      onCreated?.();
      handleClose();
    } catch (err) {
      setError(err?.message ?? 'Could not create subscription.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={handleClose} title="New Subscription" size="lg"
      footer={<>
        <Button variant="secondary" onClick={handleClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} loading={saving}>Create Subscription</Button>
      </>}>
      {error && <div className="ss-form-error">{error}</div>}
      <div className="ss-field-grid">
        <div className="ss-field ss-field-full">
          <label className="ss-field-label">Customer <span style={{ color: 'var(--color-danger)' }}>*</span></label>
          <select className="ss-input" value={form.organization_id} onChange={set('organization_id')}>
            <option value="">Select customer…</option>
            {customers.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Plan</label>
          <select className="ss-input" value={form.plan_id} onChange={set('plan_id')}>
            <option value="">No plan</option>
            {plans.map((p) => <option key={p.id} value={p.id}>{p.name} (${p.price}/{p.billing_cycle.toLowerCase()})</option>)}
          </select>
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Status</label>
          <select className="ss-input" value={form.status} onChange={set('status')}>
            {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Billing Cycle</label>
          <select className="ss-input" value={form.billing_cycle} onChange={set('billing_cycle')}>
            {BILLING_CYCLES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Auto Renewal</label>
          <select className="ss-input" value={form.auto_renew ? 'yes' : 'no'} onChange={(e) => setForm((f) => ({ ...f, auto_renew: e.target.value === 'yes' }))}>
            <option value="no">Off</option>
            <option value="yes">On</option>
          </select>
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Price (leave blank to use plan price)</label>
          <input type="number" className="ss-input" value={form.amount} onChange={set('amount')} placeholder="e.g. 999" />
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Currency</label>
          <input className="ss-input" value={form.currency} onChange={set('currency')} placeholder="USD" />
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Start Date</label>
          <input type="date" className="ss-input" value={form.start_date} onChange={set('start_date')} />
        </div>
        <div className="ss-field">
          <label className="ss-field-label">End Date</label>
          <input type="date" className="ss-input" value={form.end_date} onChange={set('end_date')} />
        </div>
      </div>
    </Modal>
  );
}

// ── Edit ─────────────────────────────────────────────────────────

export function EditSubscriptionModal({ subscription, onClose, onSaved }) {
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!subscription) { setForm(null); return; }
    setForm({
      end_date: subscription.end_date ? subscription.end_date.slice(0, 10) : '',
      amount: subscription.amount, currency: subscription.currency,
      auto_renew: subscription.auto_renew, billing_cycle: subscription.billing_cycle,
    });
    setError('');
  }, [subscription]);

  if (!subscription || !form) return null;
  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleSubmit = async () => {
    setSaving(true);
    setError('');
    try {
      await subscriptionApi.update(subscription.id, {
        end_date: form.end_date ? new Date(form.end_date).toISOString() : null,
        amount: Number(form.amount), currency: form.currency,
        auto_renew: form.auto_renew, billing_cycle: form.billing_cycle,
      });
      onSaved?.();
    } catch (err) {
      setError(err?.message ?? 'Could not update subscription.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open onClose={onClose} title={`Edit Subscription #${subscription.id}`} size="md"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} loading={saving}>Save Changes</Button>
      </>}>
      {error && <div className="ss-form-error">{error}</div>}
      <div className="ss-field-grid">
        <div className="ss-field">
          <label className="ss-field-label">Billing Cycle</label>
          <select className="ss-input" value={form.billing_cycle} onChange={set('billing_cycle')}>
            {BILLING_CYCLES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Auto Renewal</label>
          <select className="ss-input" value={form.auto_renew ? 'yes' : 'no'} onChange={(e) => setForm((f) => ({ ...f, auto_renew: e.target.value === 'yes' }))}>
            <option value="no">Off</option>
            <option value="yes">On</option>
          </select>
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Amount</label>
          <input type="number" className="ss-input" value={form.amount} onChange={set('amount')} />
        </div>
        <div className="ss-field">
          <label className="ss-field-label">Currency</label>
          <input className="ss-input" value={form.currency} onChange={set('currency')} />
        </div>
        <div className="ss-field ss-field-full">
          <label className="ss-field-label">End Date</label>
          <input type="date" className="ss-input" value={form.end_date} onChange={set('end_date')} />
        </div>
      </div>
    </Modal>
  );
}

// ── Change Plan (Upgrade / Downgrade) ───────────────────────────

export function ChangePlanModal({ subscription, plans, mode, onClose, onSaved }) {
  const [planId, setPlanId] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => { setPlanId(null); setError(''); }, [subscription]);

  if (!subscription) return null;

  const currentPlan = plans.find((p) => p.id === subscription.plan_id);
  const candidates = mode === 'upgrade'
    ? plans.filter((p) => p.id !== subscription.plan_id && p.price >= (currentPlan?.price ?? 0))
    : mode === 'downgrade'
      ? plans.filter((p) => p.id !== subscription.plan_id && p.price <= (currentPlan?.price ?? Infinity))
      : plans.filter((p) => p.id !== subscription.plan_id);

  const handleSubmit = async () => {
    if (!planId) { setError('Select a plan.'); return; }
    setSaving(true);
    setError('');
    try {
      await subscriptionApi.changePlan(subscription.id, planId);
      onSaved?.();
    } catch (err) {
      setError(err?.message ?? 'Could not change plan.');
    } finally {
      setSaving(false);
    }
  };

  const title = mode === 'upgrade' ? 'Upgrade Plan' : mode === 'downgrade' ? 'Downgrade Plan' : 'Change Plan';

  return (
    <Modal open onClose={onClose} title={title} size="md"
      footer={<>
        <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
        <Button variant="primary" onClick={handleSubmit} loading={saving} disabled={!planId}>Confirm</Button>
      </>}>
      {error && <div className="ss-form-error">{error}</div>}
      <p style={{ fontSize: 12.5, color: 'var(--text-muted)', marginBottom: 10 }}>
        Current plan: <strong>{currentPlan?.name ?? 'None'}</strong> (${currentPlan?.price ?? 0}/mo)
      </p>
      {candidates.length === 0 ? (
        <p style={{ fontSize: 12.5, color: 'var(--text-faint)' }}>No {mode === 'upgrade' ? 'higher-tier' : 'lower-tier'} plans available.</p>
      ) : candidates.map((p) => (
        <div key={p.id} className={`ss-plan-option-card ${planId === p.id ? 'selected' : ''}`} onClick={() => setPlanId(p.id)}>
          <div>
            <div className="ss-plan-option-name">{p.name}</div>
            <div className="ss-plan-option-meta">{p.modules.length} modules · {p.limits.users === -1 ? 'Unlimited' : p.limits.users} users</div>
          </div>
          <div className="ss-plan-option-price">${p.price}/{p.billing_cycle.toLowerCase()}</div>
        </div>
      ))}
    </Modal>
  );
}
