/**
 * EditCustomerModal — real edit form for an existing customer
 * Organization, opened from CustomerDetails.jsx's header "Edit" button
 * (which previously just switched to the Overview tab and edited
 * nothing — found live while wiring up Company assignment). PUTs to
 * /api/seller/customers/{id} (customer_service.update_customer).
 */
import { useEffect, useState } from 'react';
import { Modal, Button } from '@/components/common/index.js';
import { customerApi } from '@/services/api/seller/customerApi.js';
import { companyApi } from '@/services/api/seller/companyApi.js';
import './customers.css';

export function EditCustomerModal({ open, customer, onClose, onSaved }) {
  const [form, setForm] = useState(null);
  const [companies, setCompanies] = useState([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open || !customer) return;
    setError('');
    setForm({
      name: customer.name, legal_name: customer.legal_name ?? '', industry: customer.industry ?? '',
      country: customer.country ?? '', timezone: customer.timezone ?? '',
      primary_contact_name: customer.primary_contact_name ?? '', primary_contact_email: customer.primary_contact_email ?? '',
      primary_contact_phone: customer.primary_contact_phone ?? '',
      address_line: customer.address_line ?? '', city: customer.city ?? '', branch: customer.branch ?? '',
      postal_code: customer.postal_code ?? '', company_id: customer.company_id ?? '',
      max_users: customer.max_users,
    });
    companyApi.list().then(setCompanies).catch(() => setCompanies([]));
  }, [open, customer]);

  if (!form) return null;

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleSubmit = async () => {
    if (!form.name.trim()) { setError('Organization name is required.'); return; }
    setSaving(true);
    setError('');
    try {
      const body = {
        ...form,
        primary_contact_email: form.primary_contact_email.trim() || null,
        company_id: form.company_id ? Number(form.company_id) : null,
        max_users: form.max_users === '' ? null : Number(form.max_users),
      };
      await customerApi.update(customer.id, body);
      onSaved?.();
    } catch (err) {
      setError(err?.message ?? 'Could not save changes.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={`Edit — ${customer.name}`}
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button variant="primary" onClick={handleSubmit} loading={saving}>Save Changes</Button>
        </>
      }
    >
      {error && <div className="sc-form-error">{error}</div>}

      <div className="sc-field-grid">
        <div className="sc-field">
          <label className="sc-field-label">Organization Name</label>
          <input className="sc-input" value={form.name} onChange={set('name')} autoFocus />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Legal Name</label>
          <input className="sc-input" value={form.legal_name} onChange={set('legal_name')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Industry</label>
          <input className="sc-input" value={form.industry} onChange={set('industry')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Country</label>
          <input className="sc-input" value={form.country} onChange={set('country')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Timezone</label>
          <input className="sc-input" value={form.timezone} onChange={set('timezone')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Max Users</label>
          <input type="number" min={0} className="sc-input" value={form.max_users} onChange={set('max_users')} />
        </div>

        <div className="sc-section-divider">Primary Contact</div>
        <div className="sc-field">
          <label className="sc-field-label">Contact Name</label>
          <input className="sc-input" value={form.primary_contact_name} onChange={set('primary_contact_name')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Email</label>
          <input type="email" className="sc-input" value={form.primary_contact_email} onChange={set('primary_contact_email')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Phone</label>
          <input className="sc-input" value={form.primary_contact_phone} onChange={set('primary_contact_phone')} />
        </div>

        <div className="sc-section-divider">Billing Address &amp; Company</div>
        <div className="sc-field">
          <label className="sc-field-label">Address</label>
          <input className="sc-input" value={form.address_line} onChange={set('address_line')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">City</label>
          <input className="sc-input" value={form.city} onChange={set('city')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Branch / Office</label>
          <input className="sc-input" value={form.branch} onChange={set('branch')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Postal Code</label>
          <input className="sc-input" value={form.postal_code} onChange={set('postal_code')} />
        </div>
        <div className="sc-field">
          <label className="sc-field-label">Invoiced Through Company</label>
          <select className="sc-input" value={form.company_id} onChange={set('company_id')}>
            <option value="">Default company</option>
            {companies.map((c) => <option key={c.id} value={c.id}>{c.name}{c.is_default ? ' (default)' : ''}</option>)}
          </select>
        </div>
      </div>
    </Modal>
  );
}
