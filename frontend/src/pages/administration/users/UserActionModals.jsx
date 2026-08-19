/**
 * Shared action modals for Administration → Users — used by both Users.jsx
 * (row kebab menu) and UserDetails.jsx (header actions), so the two pages
 * never duplicate this logic.
 */
import { useEffect, useState } from 'react';
import { Copy, Check } from 'lucide-react';
import { Modal, Button } from '@/components/common/index.js';
import { userAdminApi } from '@/services/api/userAdminApi.js';
import './users.css';

const ROLE_OPTIONS = [
  { value: 'org_admin', label: 'Org Admin' },
  { value: 'data_engineer', label: 'Data Engineer' },
  { value: 'analyst', label: 'Analyst' },
  { value: 'viewer', label: 'Viewer' },
];

/** Change Role — confirmation + role select. `user` is the AdminUserOut row, or null to stay closed. */
export function ChangeRoleModal({ user, onClose, onDone }) {
  const [role, setRole] = useState('viewer');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => { if (user) { setRole(user.role); setError(''); } }, [user]);

  const submit = async () => {
    setSaving(true);
    setError('');
    try { await userAdminApi.changeRole(user.id, role); onDone?.(); onClose(); }
    catch (err) { setError(err?.message ?? 'Could not change role.'); }
    finally { setSaving(false); }
  };

  return (
    <Modal open={!!user} onClose={onClose} title="Change Role" size="sm"
      footer={<><Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button><Button variant="primary" loading={saving} onClick={submit}>Save</Button></>}>
      {error && <div className="usr-form-error">{error}</div>}
      {user && (
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
          Changing role for <strong>{user.full_name}</strong>.
        </p>
      )}
      <div className="usr-field">
        <label className="usr-field-label">New role</label>
        <select className="usr-input" style={{ width: '100%' }} value={role} onChange={(e) => setRole(e.target.value)}>
          {ROLE_OPTIONS.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
        </select>
      </div>
    </Modal>
  );
}

/** Reset Password — confirm, then reveal a one-time temp password (no email service is wired up). */
export function ResetPasswordModal({ user, onClose }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => { if (user) { setResult(null); setError(''); setCopied(false); } }, [user]);

  const submit = async () => {
    setSaving(true);
    setError('');
    try { setResult(await userAdminApi.resetPassword(user.id)); }
    catch (err) { setError(err?.message ?? 'Could not reset password.'); }
    finally { setSaving(false); }
  };

  const copyPassword = () => {
    navigator.clipboard?.writeText(result.temp_password).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  };

  if (result) {
    return (
      <Modal open={!!user} onClose={onClose} title="Password Reset" size="sm" footer={<Button variant="primary" onClick={onClose}>Done</Button>}>
        <div className="usr-credential-box">
          <div className="usr-credential-label">New temporary password</div>
          <div className="usr-credential-row">
            <span className="usr-credential-value">{result.temp_password}</span>
            <Button variant="ghost" size="sm" icon={copied ? <Check size={13} /> : <Copy size={13} />} onClick={copyPassword}>{copied ? 'Copied' : 'Copy'}</Button>
          </div>
          <p className="usr-credential-note">Share this with {result.user.full_name} yourself — no email service is configured yet. It won't be shown again.</p>
        </div>
      </Modal>
    );
  }

  return (
    <Modal open={!!user} onClose={onClose} title="Reset Password" size="sm"
      footer={<><Button variant="secondary" onClick={onClose} disabled={saving}>Cancel</Button><Button variant="primary" loading={saving} onClick={submit}>Reset Password</Button></>}>
      {error && <div className="usr-form-error">{error}</div>}
      {user && <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>This generates a new temporary password for <strong>{user.full_name}</strong> and invalidates their current one.</p>}
    </Modal>
  );
}
