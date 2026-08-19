/**
 * InviteUserModal — "+ Invite User" on the Users admin page.
 * POSTs to /api/admin/users (see userAdminApi.invite). No email service is
 * wired up anywhere in this app, so the response's one-time temp_password
 * is shown here for the admin to hand off manually — see
 * backend/app/services/user_admin_service.py module docstring.
 */
import { useState } from 'react';
import { Copy, Check } from 'lucide-react';
import { Modal, Button } from '@/components/common/index.js';
import { userAdminApi } from '@/services/api/userAdminApi.js';
import './users.css';

const ROLES = [
  { value: 'viewer', label: 'Viewer' },
  { value: 'analyst', label: 'Analyst' },
  { value: 'data_engineer', label: 'Data Engineer' },
  { value: 'org_admin', label: 'Org Admin' },
];

const EMPTY = {
  first_name: '', last_name: '', email: '', department: '', role: 'viewer', is_active: true, send_invite: true,
};

export function InviteUserModal({ open, onClose, onInvited }) {
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
      const created = await userAdminApi.invite(form);
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
      <Modal open={open} onClose={handleClose} title="User Invited" size="sm"
        footer={<Button variant="primary" onClick={handleClose}>Done</Button>}>
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
          <strong>{result.user.full_name}</strong> ({result.user.email}) has been created with status <strong>Pending Invitation</strong>.
        </p>
        <div className="usr-credential-box">
          <div className="usr-credential-label">Temporary password</div>
          <div className="usr-credential-row">
            <span className="usr-credential-value">{result.temp_password}</span>
            <Button variant="ghost" size="sm" icon={copied ? <Check size={13} /> : <Copy size={13} />} onClick={copyPassword}>
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>
          <p className="usr-credential-note">
            No email service is configured yet — share this password with the user yourself.
            It won't be shown again; use "Reset Password" from their profile if it's lost.
          </p>
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="Invite User"
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={handleClose} disabled={saving}>Cancel</Button>
          <Button variant="primary" onClick={handleSubmit} loading={saving}>Send Invitation</Button>
        </>
      }
    >
      {error && <div className="usr-form-error">{error}</div>}

      <div className="usr-field-grid">
        <div className="usr-field">
          <label className="usr-field-label">First Name <span className="usr-required">*</span></label>
          <input className="usr-input" style={{ width: '100%' }} value={form.first_name} onChange={set('first_name')} placeholder="Jane" autoFocus />
        </div>
        <div className="usr-field">
          <label className="usr-field-label">Last Name <span className="usr-required">*</span></label>
          <input className="usr-input" style={{ width: '100%' }} value={form.last_name} onChange={set('last_name')} placeholder="Smith" />
        </div>
        <div className="usr-field">
          <label className="usr-field-label">Email <span className="usr-required">*</span></label>
          <input type="email" className="usr-input" style={{ width: '100%' }} value={form.email} onChange={set('email')} placeholder="jane.smith@company.com" />
        </div>
        <div className="usr-field">
          <label className="usr-field-label">Department</label>
          <input className="usr-input" style={{ width: '100%' }} value={form.department} onChange={set('department')} placeholder="e.g. Data Engineering" />
        </div>
        <div className="usr-field">
          <label className="usr-field-label">Role</label>
          <select className="usr-input" style={{ width: '100%' }} value={form.role} onChange={set('role')}>
            {ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
        </div>
        <div className="usr-field">
          <label className="usr-field-label">Status</label>
          <select className="usr-input" style={{ width: '100%' }} value={form.is_active ? 'active' : 'inactive'} onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.value === 'active' }))}>
            <option value="active">Active</option>
            <option value="inactive">Inactive</option>
          </select>
        </div>
      </div>

      <label className="usr-checkbox-row">
        <input type="checkbox" checked={form.send_invite} onChange={(e) => setForm((f) => ({ ...f, send_invite: e.target.checked }))} />
        Send invitation email when available
      </label>
    </Modal>
  );
}
