/**
 * CreateCustomerModal — "+ New Customer" on the Seller Portal Customers
 * page. POSTs to /api/seller/customers (customer_service.create_customer):
 * creates a real Organization + Subscription + (if a contact email is
 * given) an initial ORG_ADMIN user, same "no real mailer, show the temp
 * password once" pattern as Administration -> Users' Invite User modal.
 */
import { useState } from 'react';
import { Copy, Check } from 'lucide-react';
import { Modal, Button } from '@/components/common/index.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import './customers.css';

// Matches the Plan rows seeded by services/seller/plan_seed.py — not a
// free-form "Custom" option yet, since there's no plan-authoring UI until
// Phase 3/4. Picking a plan here sets the licensing ceiling for real.
const PLAN_OPTIONS = [
  { value: 'trial', label: 'Trial' },
  { value: 'basic', label: 'Basic' },
  { value: 'professional', label: 'Professional' },
  { value: 'enterprise', label: 'Enterprise' },
];
const STATUS_OPTIONS = ['trial', 'active', 'expiring', 'expired', 'suspended'];

const EMPTY = {
  organization_name: '', legal_name: '', industry: '', country: '',
  primary_contact_name: '', primary_contact_email: '', primary_contact_phone: '', timezone: '',
  status: 'trial', plan_code: 'trial', start_date: '', expiry_date: '',
};

export function CreateCustomerModal({ open, onClose, onCreated }) {
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [copied, setCopied] = useState(false);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleClose = () => { setForm(EMPTY); setError(''); setResult(null); setCopied(false); onClose(); };

  const handleSubmit = async () => {
    if (!form.organization_name.trim()) { setError('Organization name is required.'); return; }
    setSaving(true);
    setError('');
    try {
      const body = {
        ...form,
        primary_contact_email: form.primary_contact_email.trim() || null,
        start_date: form.start_date ? new Date(form.start_date).toISOString() : null,
        expiry_date: form.expiry_date ? new Date(form.expiry_date).toISOString() : null,
      };
      const created = await customerApi.create(body);
      setResult(created);
      onCreated?.();
    } catch (err) {
      setError(err?.message ?? 'Could not create customer.');
    } finally {
      setSaving(false);
    }
  };

  const copyPassword = () => {
    navigator.clipboard?.writeText(result.temp_password).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  if (result) {
    return (
      <Modal open={open} onClose={handleClose} title="Customer Created" size="sm"
        footer={<Button variant="primary" onClick={handleClose}>Done</Button>}>
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
          <strong>{result.name}</strong> has been onboarded.
        </p>
        {result.temp_password ? (
          <div className="sc-credential-box">
            <div className="sc-credential-label">Primary contact temporary password</div>
            <div className="sc-credential-row">
              <span className="sc-credential-value">{result.temp_password}</span>
              <Button variant="ghost" size="sm" icon={copied ? <Check size={13} /> : <Copy size={13} />} onClick={copyPassword}>
                {copied ? 'Copied' : 'Copy'}
              </Button>
            </div>
            <p className="sc-credential-note">
              No email service is configured yet — share this password with the customer's contact yourself.
              It won't be shown again.
            </p>
          </div>
        ) : (
          <p style={{ fontSize: 12.5, color: 'var(--text-muted)' }}>No primary contact email was given, so no login account was created yet.</p>
        )}
      </Modal>
    );
  }

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="New Customer"
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={handleClose} disabled={saving}>Cancel</Button>
          <Button variant="primary" onClick={handleSubmit} loading={saving}>Create Customer</Button>
        </>
      }
    >
      {error && <div className="sc-form-error">{error}</div>}

      <div className="sc-field-grid">
        <div className="sc-field">
          <label className="sc-field-label">Organization Name <span className="sc-required">*</span></label>
          <input className="sc-input" value={form.organization_name} onChange={set('organization_name')} placeholder="Acme Corporation" autoFocus />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Legal Name</label>
          <input className="sc-input" value={form.legal_name} onChange={set('legal_name')} placeholder="Acme Corporation LLC" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Industry</label>
          <input className="sc-input" value={form.industry} onChange={set('industry')} placeholder="Manufacturing" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Country</label>
          <input className="sc-input" value={form.country} onChange={set('country')} placeholder="United States" />
        </div>

        <div className="sc-section-divider">Primary Contact</div>
        <div className="sc-field">
          <label className="sc-field-label">Contact Name</label>
          <input className="sc-input" value={form.primary_contact_name} onChange={set('primary_contact_name')} placeholder="Jane Doe" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Email</label>
          <input type="email" className="sc-input" value={form.primary_contact_email} onChange={set('primary_contact_email')} placeholder="jane@acme.com" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Phone</label>
          <input className="sc-input" value={form.primary_contact_phone} onChange={set('primary_contact_phone')} placeholder="+1-555-0100" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Timezone</label>
          <input className="sc-input" value={form.timezone} onChange={set('timezone')} placeholder="America/New_York" />
        </div>

        <div className="sc-section-divider">Plan &amp; Status</div>
        <div className="sc-field">
          <label className="sc-field-label">Initial Plan</label>
          <select className="sc-input" value={form.plan_code} onChange={set('plan_code')}>
            {PLAN_OPTIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Status</label>
          <select className="sc-input" value={form.status} onChange={set('status')}>
            {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s.charAt(0).toUpperCase() + s.slice(1)}</option>)}
          </select>
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Start Date</label>
          <input type="date" className="sc-input" value={form.start_date} onChange={set('start_date')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Expiry Date</label>
          <input type="date" className="sc-input" value={form.expiry_date} onChange={set('expiry_date')} />
        </div>
      </div>
    </Modal>
  );
}
