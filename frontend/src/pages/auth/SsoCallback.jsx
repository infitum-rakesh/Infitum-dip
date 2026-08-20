/**
 * SsoCallback — /sso/callback
 * Lands here after a real SAML sign-in (backend app/api/sso.py's POST
 * /acs redirects here with a one-time `code`, never the JWTs themselves).
 * Exchanges that code for a real TokenResponse and completes sign-in the
 * same way password login does — see authApi.ssoExchange / AuthContext's
 * completeSsoLogin.
 */
import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { AlertCircle, Loader2 } from 'lucide-react';
import { authApi } from '@/services/api/authApi.js';
import { useAuth } from '@/hooks/useAuth.js';
import './auth.css';

export default function SsoCallback() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { completeSsoLogin } = useAuth();
  const [error, setError] = useState('');
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return; // StrictMode double-invoke guard — a handoff code is single-use
    ran.current = true;

    const code = params.get('code');
    if (!code) {
      setError('Missing SSO sign-in code.');
      return;
    }
    authApi.ssoExchange(code)
      .then((data) => {
        completeSsoLogin(data);
        navigate('/app/dashboard', { replace: true });
      })
      .catch((err) => setError(err?.message ?? 'This sign-in link has expired or was already used.'));
  }, [params, navigate, completeSsoLogin]);

  return (
    <div className="auth-form-wrap animate-fade-in-up">
      <div className="auth-logo-row">
        <div className="auth-logo-mark">DI</div>
        <span className="auth-logo-name">DataIntelligence</span>
      </div>

      {error ? (
        <>
          <h1 className="auth-heading">Sign-in failed</h1>
          <div className="auth-alert auth-alert-error" role="alert">
            <AlertCircle size={14} /> {error}
          </div>
          <p className="auth-footer-link">
            <a href="/login">Back to sign in</a>
          </p>
        </>
      ) : (
        <>
          <h1 className="auth-heading">Signing you in…</h1>
          <p className="auth-sub-heading" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Loader2 size={14} className="animate-spin" /> Completing your SSO sign-in.
          </p>
        </>
      )}
    </div>
  );
}
