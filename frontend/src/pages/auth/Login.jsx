import { useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { Eye, EyeOff, AlertCircle, Fingerprint, ArrowLeft } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth.js';
import { Button } from '@/components/common/Button.jsx';
import { validate, email as validateEmail, required } from '@/utils/validators.js';
import './auth.css';

export default function Login() {
  const { login, isLoading } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [form, setForm]       = useState({ email: '', password: '' });
  const [errors, setErrors]   = useState({});
  // Seeded from ?sso_error=... when the backend redirects back here after a
  // failed SAML sign-in (see api/sso.py's _login_error_redirect) — same
  // alert box as any other login error, no special-casing needed to render it.
  const [apiError, setApiErr] = useState(searchParams.get('sso_error') ?? '');
  const [showPwd, setShowPwd] = useState(false);
  const [ssoMode, setSsoMode] = useState(false);
  const [orgSlug, setOrgSlug] = useState('');

  const set = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));

  const handleSubmit = async (e) => {
    e.preventDefault();
    const errs = validate(form, {
      email:    (v) => validateEmail(v),
      password: (v) => required(v, 'Password'),
    });
    setErrors(errs);
    if (Object.keys(errs).length) return;

    setApiErr('');
    const result = await login(form.email, form.password);
    if (result.ok) navigate('/app/dashboard', { replace: true });
    else setApiErr(result.error);
  };

  return (
    <div className="auth-form-wrap animate-fade-in-up">
      <a href="/" className="auth-logo-row auth-logo-link" aria-label="Back to home">
        <div className="auth-logo-mark">DI</div>
        <span className="auth-logo-name">DataIntelligence</span>
      </a>

      <h1 className="auth-heading">Welcome back</h1>
      <p className="auth-sub-heading">{ssoMode ? "Sign in with your company's SSO" : 'Sign in to your workspace'}</p>

      {apiError && (
        <div className="auth-alert auth-alert-error" role="alert">
          <AlertCircle size={14} /> {apiError}
        </div>
      )}

      {ssoMode ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const slug = orgSlug.trim().toLowerCase().replace(/\s+/g, '-');
            if (!slug) return;
            // Real full-page navigation (not fetch) — the backend 302s the
            // browser straight to the IdP's SSO URL with a signed
            // AuthnRequest (see api/sso.py's GET /{org_slug}/login).
            window.location.href = `/api/auth/sso/${encodeURIComponent(slug)}/login`;
          }}
          noValidate
        >
          <div className="form-field">
            <label className="form-label" htmlFor="org-slug">Company ID</label>
            <input
              id="org-slug"
              type="text"
              className="form-input"
              value={orgSlug}
              onChange={(e) => setOrgSlug(e.target.value)}
              placeholder="e.g. acme-corporation"
              autoFocus
            />
            <p style={{ fontSize: 'var(--fs-xs)', color: 'var(--text-muted)', marginTop: 6 }}>
              Your organization's ID, given to you by your administrator. You'll be redirected to your company's identity provider.
            </p>
          </div>
          <Button type="submit" variant="primary" size="lg" full disabled={!orgSlug.trim()}>
            Continue with SSO
          </Button>
        </form>
      ) : (
        <form onSubmit={handleSubmit} noValidate>
          <div className="form-field">
            <label className="form-label" htmlFor="email">Email address</label>
            <input
              id="email"
              type="email"
              className={`form-input ${errors.email ? 'input-error' : ''}`}
              value={form.email}
              onChange={set('email')}
              placeholder="you@company.com"
              autoComplete="email"
              autoFocus
            />
            {errors.email && <p className="field-error"><AlertCircle size={11} />{errors.email}</p>}
          </div>

          <div className="form-field">
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
              <label className="form-label" htmlFor="password">Password</label>
              <Link to="/forgot-password" style={{ fontSize: 'var(--fs-sm)', color: 'var(--color-primary)' }}>
                Forgot password?
              </Link>
            </div>
            <div className="form-input-wrap">
              <input
                id="password"
                type={showPwd ? 'text' : 'password'}
                className={`form-input ${errors.password ? 'input-error' : ''}`}
                value={form.password}
                onChange={set('password')}
                placeholder="Enter your password"
                autoComplete="current-password"
              />
              <button type="button" className="pwd-toggle" onClick={() => setShowPwd((s) => !s)} tabIndex={-1}>
                {showPwd ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>
            {errors.password && <p className="field-error"><AlertCircle size={11} />{errors.password}</p>}
          </div>

          <Button type="submit" variant="primary" size="lg" full loading={isLoading}>
            Sign in
          </Button>
        </form>
      )}

      <button type="button" className="auth-mode-toggle" onClick={() => { setSsoMode((v) => !v); setApiErr(''); }}>
        {ssoMode ? <><ArrowLeft size={14} /> Back to email &amp; password</> : <><Fingerprint size={14} /> Sign in with SSO</>}
      </button>

      <p className="auth-footer-link">
        Don't have an account?{' '}
        <Link to="/register">Start free trial</Link>
      </p>
    </div>
  );
}
