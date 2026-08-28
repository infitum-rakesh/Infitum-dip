/**
 * ContactSalesModal — Administration -> Licenses' "Contact Sales" action.
 * This page is otherwise read-only for org admins (Seller controls which
 * modules are purchased, plan limits and expiration); this is the one
 * write it offers — a "please connect me with Sales" inquiry, not a
 * self-service purchase. POSTs to /api/licenses/contact-sales
 * (license_service.contact_sales), which emails the Seller's contact
 * address and logs the inquiry to SellerAuditLog.
 */
import { useState } from 'react';
import { CheckCircle2, XCircle, Mail } from 'lucide-react';
import { Modal, Button } from '@/components/common/index.js';
import { MODULE_META } from '@/utils/permissions.js';
import { licenseApi } from '@/services/api/licenseApi.js';
import './licenses.css';

const EMPTY = { module_interest: '', contact_name: '', contact_email: '', contact_phone: '', message: '' };

export function ContactSalesModal({ open, onClose, user, modules }) {
  const [form, setForm] = useState(() => ({ ...EMPTY, contact_name: user?.full_name ?? '', contact_email: user?.email ?? '' }));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleClose = () => {
    setForm({ ...EMPTY, contact_name: user?.full_name ?? '', contact_email: user?.email ?? '' });
    setError(''); setResult(null);
    onClose();
  };

  const handleSubmit = async () => {
    if (!form.contact_name.trim()) { setError('Your name is required.'); return; }
    if (!form.contact_email.trim()) { setError('A contact email is required.'); return; }
    if (!form.message.trim()) { setError('Please describe what you need — module, seats, or plan change.'); return; }
    setSaving(true);
    setError('');
    try {
      const sent = await licenseApi.contactSales(form);
      setResult(sent);
    } catch (err) {
      setError(err?.message ?? 'Could not send your inquiry.');
    } finally {
      setSaving(false);
    }
  };

  if (result) {
    return (
      <Modal open={open} onClose={handleClose} title="Sales Inquiry" size="sm"
        footer={<Button variant="primary" onClick={handleClose}>Done</Button>}>
        <p className={`lic-sales-result ${result.delivered ? 'lic-sales-result-ok' : 'lic-sales-result-fail'}`}>
          {result.delivered ? <CheckCircle2 size={16} /> : <XCircle size={16} />}
          <Mail size={14} />
          {result.delivered ? 'Your inquiry has been sent — our Sales team will follow up shortly.' : result.detail}
        </p>
      </Modal>
    );
  }

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="Contact Sales"
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={handleClose} disabled={saving}>Cancel</Button>
          <Button variant="primary" onClick={handleSubmit} loading={saving}>Send to Sales</Button>
        </>
      }
    >
      <p style={{ fontSize: 12.5, color: 'var(--text-muted)', marginBottom: 14 }}>
        To purchase a new module, add seats, or change your plan, send the details below to our Sales team.
      </p>

      {error && <div className="lic-form-error">{error}</div>}

      <div className="lic-field-grid">
        <div className="lic-field lic-field-full">
          <label className="lic-field-label">Module of Interest</label>
          <select className="lic-input" value={form.module_interest} onChange={set('module_interest')}>
            <option value="">General inquiry / plan upgrade</option>
            {modules?.map((m) => (
              <option key={m.module} value={m.module}>
                {MODULE_META[m.module]?.label ?? m.module}{m.purchased ? ' (already purchased)' : ''}
              </option>
            ))}
          </select>
        </div>

        <div className="lic-field">
          <label className="lic-field-label">Your Name <span className="lic-required">*</span></label>
          <input className="lic-input" value={form.contact_name} onChange={set('contact_name')} placeholder="Jane Doe" autoFocus />
        </div>
        <div className="lic-field">
          <label className="lic-field-label">Email <span className="lic-required">*</span></label>
          <input type="email" className="lic-input" value={form.contact_email} onChange={set('contact_email')} placeholder="jane@company.com" />
        </div>
        <div className="lic-field lic-field-full">
          <label className="lic-field-label">Phone</label>
          <input className="lic-input" value={form.contact_phone} onChange={set('contact_phone')} placeholder="+1-555-0100" />
        </div>

        <div className="lic-field lic-field-full">
          <label className="lic-field-label">Details <span className="lic-required">*</span></label>
          <textarea
            className="lic-input lic-textarea" rows={4} value={form.message} onChange={set('message')}
            placeholder="Tell us what you need — e.g. which module, how many additional seats, or a plan upgrade."
          />
        </div>
      </div>
    </Modal>
  );
}
