/**
 * InviteSellerUserModal — "+ Invite Seller User" on the Seller Users page.
 * POSTs to /api/seller/users (see sellerUserApi.invite). No email service
 * is wired up anywhere in this app, so the response's one-time
 * temp_password is shown here for the admin to hand off manually — same
 * convention as InviteUserModal.jsx (Administration -> Users).
 */
import { useState } from 'react';
import { Copy, Check } from 'lucide-react';
import { Modal, Button } from '@/components/common/index.js';
import { sellerUserApi } from '@/services/api/seller/sellerUserApi.js';
import './seller-users.css';

export const SELLER_ROLE_OPTIONS = [
  { value: '', label: 'Seller Admin (full access — default)' },
  { value: 'SELLER_ADMIN', label: 'Seller Admin' },
  { value: 'SELLER_CUSTOMER_MANAGER', label: 'Customer Manager' },
  { value: 'SELLER_LICENSE_MANAGER', label: 'License Manager' },
  { value: 'SELLER_SUPPORT', label: 'Support' },
  { value: 'SELLER_VIEWER', label: 'Viewer (read-only)' },
];

const EMPTY = { first_name: '', last_name: '', email: '', department: '', seller_role: '', is_active: true };

export function InviteSellerUserModal({ open, onClose, onInvited }) {
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null); // { user, temp_password } once created
  const [copied, setCopied] = useState(false);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleClose = () => { setForm(EMPTY); setError(''); setResult(null); setCopied(false); onClose(); };

  const handleSubmit = async () => {
    if (!form.first_name.trim() || !form.last_name.trim() || !form.email.trim()) {
      setError('First name, last name and email are required.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const created = await sellerUserApi.invite({ ...form, seller_role: form.seller_role || null });
      setResult(created);
      onInvited?.(created.user);
    } catch (err) {
      setError(err?.message ?? 'Could not invite user.');
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
      <Modal open={open} onClose={handleClose} title="Seller User Invited" size="sm"
        footer={<Button variant="primary" onClick={handleClose}>Done</Button>}>
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
          <strong>{result.user.full_name}</strong> ({result.user.email}) has been granted Seller Portal access with status <strong>Pending Invitation</strong>.
        </p>
        <div className="su-credential-box">
          <div className="su-credential-label">Temporary password</div>
          <div className="su-credential-row">
            <span className="su-credential-value">{result.temp_password}</span>
            <Button variant="ghost" size="sm" icon={copied ? <Check size={13} /> : <Copy size={13} />} onClick={copyPassword}>
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>
          <p className="su-credential-note">
            No email service is configured yet — share this password with the user yourself.
            It won't be shown again; use "Reset Password" from the row menu if it's lost.
          </p>
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="Invite Seller User"
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={handleClose} disabled={saving}>Cancel</Button>
          <Button variant="primary" onClick={handleSubmit} loading={saving}>Send Invitation</Button>
        </>
      }
    >
      {error && <div className="su-form-error">{error}</div>}

      <div className="su-field-grid">
        <div className="su-field">
          <label className="su-field-label">First Name <span className="su-required">*</span></label>
          <input className="su-input" style={{ width: '100%' }} value={form.first_name} onChange={set('first_name')} placeholder="Jane" autoFocus />
        </div>
        <div className="su-field">
          <label className="su-field-label">Last Name <span className="su-required">*</span></label>
          <input className="su-input" style={{ width: '100%' }} value={form.last_name} onChange={set('last_name')} placeholder="Smith" />
        </div>
        <div className="su-field">
          <label className="su-field-label">Email <span className="su-required">*</span></label>
          <input type="email" className="su-input" style={{ width: '100%' }} value={form.email} onChange={set('email')} placeholder="jane.smith@platform-admin.com" />
        </div>
        <div className="su-field">
          <label className="su-field-label">Department</label>
          <input className="su-input" style={{ width: '100%' }} value={form.department} onChange={set('department')} placeholder="e.g. Customer Success" />
        </div>
        <div className="su-field">
          <label className="su-field-label">Seller Role</label>
          <select className="su-input" style={{ width: '100%' }} value={form.seller_role} onChange={set('seller_role')}>
            {SELLER_ROLE_OPTIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
        </div>
        <div className="su-field">
          <label className="su-field-label">Status</label>
          <select className="su-input" style={{ width: '100%' }} value={form.is_active ? 'active' : 'inactive'} onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.value === 'active' }))}>
            <option value="active">Active</option>
            <option value="inactive">Inactive</option>
          </select>
        </div>
      </div>
    </Modal>
  );
}
