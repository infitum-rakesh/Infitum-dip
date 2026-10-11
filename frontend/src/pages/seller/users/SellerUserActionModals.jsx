/**
 * Shared action modals for the Seller Users page (row kebab menu):
 * Edit, Change Seller Role, Reset Password. Mirrors
 * administration/users/UserActionModals.jsx exactly, scoped to
 * sellerUserApi instead of userAdminApi.
 */
import { useEffect, useState } from 'react';
import { Copy, Check, CheckCircle2, XCircle, Mail } from 'lucide-react';
import { Modal, Button } from '@/components/common/index.js';
import { sellerUserApi } from '@/services/api/seller/sellerUserApi.js';
import { SELLER_ROLE_OPTIONS } from './InviteSellerUserModal.jsx';
import './seller-users.css';

/** Edit — name + department. `user` is the SellerUserOut row, or null to stay closed. */
export function EditSellerUserModal({ user, onClose, onDone }) {
  const [form, setForm] = useState({ first_name: '', last_name: '', department: '' });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (user) {
      const [first, ...rest] = (user.full_name || '').split(' ');
      setForm({ first_name: first || '', last_name: rest.join(' '), department: user.department || '' });
      setError('');
    }
  }, [user]);

  const submit = async () => {
    setSaving(true);
    setError('');
    try { await sellerUserApi.update(user.id, form); onDone?.(); onClose(); }
    catch (err) { setError(err?.message ?? 'Could not update user.'); }
    finally { setSaving(false); }
  };

  return (
    <Modal open={!!user} onClose={onClose} title="Edit Seller User" size="md"
      footer={<><Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button><Button variant="primary" loading={saving} onClick={submit}>Save</Button></>}>
      {error && <div className="su-form-error">{error}</div>}
      <div className="su-field-grid">
        <div className="su-field">
          <label className="su-field-label">First Name</label>
          <input className="su-input" style={{ width: '100%' }} value={form.first_name} onChange={(e) => setForm((f) => ({ ...f, first_name: e.target.value }))} />
        </div>
        <div className="su-field">
          <label className="su-field-label">Last Name</label>
          <input className="su-input" style={{ width: '100%' }} value={form.last_name} onChange={(e) => setForm((f) => ({ ...f, last_name: e.target.value }))} />
        </div>
      </div>
      <div className="su-field">
        <label className="su-field-label">Department</label>
        <input className="su-input" style={{ width: '100%' }} value={form.department} onChange={(e) => setForm((f) => ({ ...f, department: e.target.value }))} />
      </div>
    </Modal>
  );
}

/** Change Seller Role — confirmation + role select. */
export function ChangeSellerRoleModal({ user, onClose, onDone }) {
  const [role, setRole] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => { if (user) { setRole(user.seller_role || ''); setError(''); } }, [user]);

  const submit = async () => {
    setSaving(true);
    setError('');
    try { await sellerUserApi.changeRole(user.id, role || null); onDone?.(); onClose(); }
    catch (err) { setError(err?.message ?? 'Could not change role.'); }
    finally { setSaving(false); }
  };

  return (
    <Modal open={!!user} onClose={onClose} title="Change Seller Role" size="sm"
      footer={<><Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button><Button variant="primary" loading={saving} onClick={submit}>Save</Button></>}>
      {error && <div className="su-form-error">{error}</div>}
      {user && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
          Changing Seller Portal role for <strong>{user.full_name}</strong>.
        </p>
      )}
      <div className="su-field">
        <label className="su-field-label">New role</label>
        <select className="su-input" style={{ width: '100%' }} value={role} onChange={(e) => setRole(e.target.value)}>
          {SELLER_ROLE_OPTIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
        </select>
      </div>
    </Modal>
  );
}

/** Reset Password — confirm, then reveal a one-time temp password. */
export function ResetSellerPasswordModal({ user, onClose }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => { if (user) { setResult(null); setError(''); setCopied(false); } }, [user]);

  const submit = async () => {
    setSaving(true);
    setError('');
    try { setResult(await sellerUserApi.resetPassword(user.id)); }
    catch (err) { setError(err?.message ?? 'Could not reset password.'); }
    finally { setSaving(false); }
  };

  const copyPassword = () => {
    navigator.clipboard?.writeText(result.temp_password).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  };

  if (result) {
    return (
      <Modal open={!!user} onClose={onClose} title="Password Reset" size="sm" footer={<Button variant="primary" onClick={onClose}>Done</Button>}>
        <div className="su-credential-box">
          <div className="su-credential-label">New temporary password</div>
          <div className="su-credential-row">
            <span className="su-credential-value">{result.temp_password}</span>
            <Button variant="ghost" size="sm" icon={copied ? <Check size={13} /> : <Copy size={13} />} onClick={copyPassword}>{copied ? 'Copied' : 'Copy'}</Button>
          </div>
          {result.email_delivered != null && (
            <p className="su-credential-note" style={{ display: 'flex', alignItems: 'center', gap: 6, color: result.email_delivered ? 'var(--success, #10B981)' : 'var(--danger, #DC2626)' }}>
              {result.email_delivered ? <CheckCircle2 size={13} /> : <XCircle size={13} />}
              <Mail size={13} /> {result.email_delivered ? `Emailed to ${result.user.email}.` : result.email_detail}
            </p>
          )}
          <p className="su-credential-note">Share this with {result.user.full_name} yourself if the email above didn't arrive. It won't be shown again.</p>
        </div>
      </Modal>
    );
  }

  return (
    <Modal open={!!user} onClose={onClose} title="Reset Password" size="sm"
      footer={<><Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button><Button variant="primary" loading={saving} onClick={submit}>Reset Password</Button></>}>
      {error && <div className="su-form-error">{error}</div>}
      {user && <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>This generates a new temporary password for <strong>{user.full_name}</strong> and invalidates their current one.</p>}
    </Modal>
  );
}
