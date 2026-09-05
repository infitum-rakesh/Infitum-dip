/**
 * CreateCustomerModal — "+ New Customer" on the Seller Portal Customers
 * page. POSTs to /api/seller/customers (customer_service.create_customer):
 * creates a real Organization + Subscription + (if a contact email is
 * given) an initial ORG_ADMIN user, same "no real mailer, show the temp
 * password once" pattern as Administration -> Users' Invite User modal.
 */
import { useEffect, useMemo, useState } from 'react';
import { Copy, Check, Mail, CheckCircle2, XCircle } from 'lucide-react';
import { Modal, Button } from '@/components/common/index.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import { companyApi } from '@/services/api/seller/companyApi.js';
import { moduleApi } from '@/services/api/seller/moduleApi.js';
import './customers.css';

// Matches the Plan rows seeded by services/seller/plan_seed.py, plus
// "Custom" — pick individual modules instead of a fixed bundle, priced
// from the Module Prices catalog (Seller Settings) instead of Plan.price.
const PLAN_OPTIONS = [
  { value: 'trial', label: 'Trial' },
  { value: 'basic', label: 'Basic' },
  { value: 'professional', label: 'Professional' },
  { value: 'enterprise', label: 'Enterprise' },
  { value: 'custom', label: 'Custom' },
];
const STATUS_OPTIONS = ['trial', 'active', 'expiring', 'expired', 'suspended'];

const EMPTY = {
  organization_name: '', legal_name: '', industry: '', country: '',
  primary_contact_name: '', primary_contact_email: '', primary_contact_phone: '', timezone: '',
  address_line: '', city: '', branch: '', postal_code: '', company_id: '',
  status: 'trial', plan_code: 'trial', start_date: '', expiry_date: '', modules: [],
};

export function CreateCustomerModal({ open, onClose, onCreated }) {
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [copied, setCopied] = useState(false);
  const [companies, setCompanies] = useState([]);
  const [modulePrices, setModulePrices] = useState([]);

  useEffect(() => {
    if (!open) return;
    companyApi.list().then(setCompanies).catch(() => setCompanies([]));
    moduleApi.getModulePrices().then(setModulePrices).catch(() => setModulePrices([]));
  }, [open]);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const toggleModule = (module) => {
    setForm((f) => ({
      ...f,
      modules: f.modules.includes(module) ? f.modules.filter((m) => m !== module) : [...f.modules, module],
    }));
  };

  const isCustomPlan = form.plan_code === 'custom';
  const customTotal = useMemo(() => {
    if (!isCustomPlan) return 0;
    const priceByModule = Object.fromEntries(modulePrices.map((m) => [m.module, m.price]));
    return form.modules.reduce((sum, m) => sum + (priceByModule[m] ?? 0), 0);
  }, [isCustomPlan, form.modules, modulePrices]);

  const handleClose = () => { setForm(EMPTY); setError(''); setResult(null); setCopied(false); onClose(); };

  const handleSubmit = async () => {
    if (!form.organization_name.trim()) { setError('Organization name is required.'); return; }
    if (!form.primary_contact_email.trim()) { setError('A primary contact email is required — without one, login credentials can\'t be generated.'); return; }
    if (isCustomPlan && form.modules.length === 0) { setError('Pick at least one module for the Custom plan.'); return; }
    setSaving(true);
    setError('');
    try {
      const body = {
        ...form,
        primary_contact_email: form.primary_contact_email.trim(),
        company_id: form.company_id ? Number(form.company_id) : null,
        start_date: form.start_date ? new Date(form.start_date).toISOString() : null,
        expiry_date: form.expiry_date ? new Date(form.expiry_date).toISOString() : null,
        modules: isCustomPlan ? form.modules : null,
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
            {result.email_delivered != null && (
              <p className="sc-credential-note" style={{ display: 'flex', alignItems: 'center', gap: 6, color: result.email_delivered ? 'var(--success, #10B981)' : 'var(--danger, #DC2626)' }}>
                {result.email_delivered ? <CheckCircle2 size={13} /> : <XCircle size={13} />}
                <Mail size={13} /> {result.email_delivered ? `Emailed to ${result.admin_email}.` : result.email_detail}
              </p>
            )}
            <p className="sc-credential-note">
              This password won't be shown again — copy it now, or use "Resend Welcome Email" from the customer's
              detail page later (that issues a fresh one).
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
          <label className="sc-field-label">Email <span className="sc-required">*</span></label>
          <input type="email" className="sc-input" value={form.primary_contact_email} onChange={set('primary_contact_email')} placeholder="jane@acme.com" />
          <p className="sc-field-hint">Required — this is where login credentials are sent.</p>
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Phone</label>
          <input className="sc-input" value={form.primary_contact_phone} onChange={set('primary_contact_phone')} placeholder="+1-555-0100" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Timezone</label>
          <input className="sc-input" value={form.timezone} onChange={set('timezone')} placeholder="America/New_York" />
        </div>

        <div className="sc-section-divider">Billing Address &amp; Company</div>
        <div className="sc-field">
          <label className="sc-field-label">Address</label>
          <input className="sc-input" value={form.address_line} onChange={set('address_line')} placeholder="500 Market St" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">City</label>
          <input className="sc-input" value={form.city} onChange={set('city')} placeholder="San Francisco" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Branch / Office</label>
          <input className="sc-input" value={form.branch} onChange={set('branch')} placeholder="e.g. HQ" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Postal Code</label>
          <input className="sc-input" value={form.postal_code} onChange={set('postal_code')} placeholder="94105" />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Invoiced Through Company</label>
          <select className="sc-input" value={form.company_id} onChange={set('company_id')}>
            <option value="">Default company</option>
            {companies.map((c) => <option key={c.id} value={c.id}>{c.name}{c.is_default ? ' (default)' : ''}</option>)}
          </select>
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

        {isCustomPlan && (
          <>
            <div className="sc-field sc-field-full">
              <label className="sc-field-label">Modules <span className="sc-required">*</span></label>
              <div className="sc-module-picker">
                {modulePrices.map((m) => (
                  <label key={m.module} className="sc-module-check">
                    <input type="checkbox" checked={form.modules.includes(m.module)} onChange={() => toggleModule(m.module)} />
                    {m.label} · {m.currency} {m.price.toFixed(0)}/mo
                  </label>
                ))}
              </div>
            </div>
            <div className="sc-custom-total">
              <span>{form.status === 'trial' ? 'Free during trial' : 'Monthly total'}</span>
              <strong>{form.status === 'trial' ? '—' : `$${customTotal.toFixed(2)}/mo`}</strong>
            </div>
            {form.status !== 'trial' && (
              <p className="sc-field-hint sc-field-full">
                A real invoice will be created for this amount. The customer has no module access until it's paid —
                generate a payment link from the customer's row after creating them, or from Billing -&gt; Invoices.
              </p>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}
