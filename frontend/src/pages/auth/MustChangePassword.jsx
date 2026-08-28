import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Eye, EyeOff, AlertCircle, ShieldAlert } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth.js';
import { authApi } from '@/services/api/authApi.js';
import { Button } from '@/components/common/Button.jsx';
import { validate, password as validatePassword, passwordsMatch } from '@/utils/validators.js';
import './auth.css';

// Reached only when AuthContext's user.must_change_password is true — i.e.
// the account is currently on a one-time dummy password an admin/Seller just
// issued (see router.jsx's ProtectedRoute gate). No "current password" field
// on purpose: the backend's change_password() skips that check whenever
// must_change_password is already set, since re-typing a dummy value the
// user was just handed proves nothing. SSO accounts never reach this page —
// must_change_password is never set on one (see auth_service.change_password
// docstring).
export default function MustChangePassword() {
  const { user, logout, clearMustChangePassword } = useAuth();
  const navigate = useNavigate();

  const [form, setForm]       = useState({ password: '', confirm: '' });
  const [errors, setErrors]   = useState({});
  const [loading, setLoading] = useState(false);
  const [apiError, setApiErr] = useState('');
  const [showPwd, setShowPwd] = useState(false);

  const set = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));

  const handleSubmit = async (e) => {
    e.preventDefault();
    const errs = validate(form, {
      password: (v) => validatePassword(v),
      confirm:  (v) => passwordsMatch(v, form.password),
    });
    setErrors(errs);
    if (Object.keys(errs).length) return;

    setApiErr('');
    setLoading(true);
    try {
      await authApi.changePassword(form.password);
      clearMustChangePassword();
      navigate('/app/dashboard', { replace: true });
    } catch (err) {
      setApiErr(err.message || 'Could not update your password.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-form-wrap animate-fade-in-up">
      <div className="auth-logo-row">
        <div className="auth-logo-mark">DI</div>
        <span className="auth-logo-name">DataIntelligence</span>
      </div>

      <div style={{ textAlign: 'center', marginBottom: 24 }}>
        <div style={{ width: 52, height: 52, borderRadius: 16, background: 'var(--color-warning-light, #fef3c7)', display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 16px', color: 'var(--color-warning, #b45309)' }}>
          <ShieldAlert size={24} />
        </div>
        <h1 className="auth-heading">Set a new password</h1>
        <p className="auth-sub-heading">
          {user?.email ? `${user.email} is` : 'Your account is'} using a temporary password. Choose a new one to continue.
        </p>
      </div>

      {apiError && (
        <div className="auth-alert auth-alert-error" role="alert">
          <AlertCircle size={14} /> {apiError}
        </div>
      )}

      <form onSubmit={handleSubmit} noValidate>
        <div className="form-field">
          <label className="form-label" htmlFor="new-password">New password</label>
          <div className="form-input-wrap">
            <input
              id="new-password"
              type={showPwd ? 'text' : 'password'}
              className={`form-input ${errors.password ? 'input-error' : ''}`}
              value={form.password}
              onChange={set('password')}
              placeholder="At least 8 characters"
              autoComplete="new-password"
              autoFocus
            />
            <button type="button" className="pwd-toggle" onClick={() => setShowPwd((s) => !s)} tabIndex={-1}>
              {showPwd ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </div>
          {errors.password && <p className="field-error"><AlertCircle size={11} />{errors.password}</p>}
        </div>

        <div className="form-field">
          <label className="form-label" htmlFor="confirm-password">Confirm new password</label>
          <input
            id="confirm-password"
            type={showPwd ? 'text' : 'password'}
            className={`form-input ${errors.confirm ? 'input-error' : ''}`}
            value={form.confirm}
            onChange={set('confirm')}
            placeholder="Re-enter your new password"
            autoComplete="new-password"
          />
          {errors.confirm && <p className="field-error"><AlertCircle size={11} />{errors.confirm}</p>}
        </div>

        <Button type="submit" variant="primary" size="lg" full loading={loading}>
          Set password &amp; continue
        </Button>
      </form>

      <button
        type="button"
        className="auth-mode-toggle"
        onClick={() => logout()}
        style={{ marginTop: 16 }}
      >
        Sign out instead
      </button>
    </div>
  );
}
