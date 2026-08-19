import { useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, CheckCircle2, Mail } from 'lucide-react';
import { authApi } from '@/services/api/authApi.js';
import { Button } from '@/components/common/Button.jsx';
import { validate, email as validateEmail } from '@/utils/validators.js';
import './auth.css';

export default function ForgotPassword() {
  const [emailVal, setEmail] = useState('');
  const [errors, setErrors]  = useState({});
  const [loading, setLoading]= useState(false);
  const [sent, setSent]      = useState(false);
  const [apiError, setApiErr]= useState('');

  const handleSubmit = async (e) => {
    e.preventDefault();
    const errs = validate({ email: emailVal }, { email: (v) => validateEmail(v) });
    setErrors(errs);
    if (Object.keys(errs).length) return;

    setLoading(true);
    setApiErr('');
    try {
      await authApi.forgotPassword(emailVal);
      setSent(true);
    } catch (err) {
      setApiErr(err.message || 'Request failed.');
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

      {sent ? (
        <>
          <div className="auth-alert auth-alert-success" style={{ marginBottom: 20 }}>
            <CheckCircle2 size={16} />
            Check your inbox — a reset link has been sent to <strong>{emailVal}</strong>
          </div>
          <p className="auth-footer-link"><Link to="/login">← Back to sign in</Link></p>
        </>
      ) : (
        <>
          <div style={{ textAlign: 'center', marginBottom: 24 }}>
            <div style={{ width: 52, height: 52, borderRadius: 16, background: 'var(--color-primary-light)', display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 16px', color: 'var(--color-primary)' }}>
              <Mail size={24} />
            </div>
            <h1 className="auth-heading">Reset password</h1>
            <p className="auth-sub-heading">Enter your email and we'll send a reset link.</p>
          </div>

          {apiError && (
            <div className="auth-alert auth-alert-error"><AlertCircle size={14} />{apiError}</div>
          )}

          <form onSubmit={handleSubmit} noValidate>
            <div className="form-field">
              <label className="form-label" htmlFor="email">Email address</label>
              <input
                id="email" type="email"
                className={`form-input ${errors.email ? 'input-error' : ''}`}
                value={emailVal} onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com" autoFocus
              />
              {errors.email && <p className="field-error"><AlertCircle size={11}/>{errors.email}</p>}
            </div>
            <Button type="submit" variant="primary" size="lg" full loading={loading}>
              Send reset link
            </Button>
          </form>

          <p className="auth-footer-link"><Link to="/login">← Back to sign in</Link></p>
        </>
      )}
    </div>
  );
}
