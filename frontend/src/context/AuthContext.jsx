import { createContext, useCallback, useEffect, useState } from 'react';
import { authApi } from '@/services/api/authApi.js';
import { STORAGE_KEYS } from '@/utils/constants.js';

export const AuthContext = createContext(null);

const _persist = (key, value) => {
  if (value) localStorage.setItem(key, JSON.stringify(value));
  else localStorage.removeItem(key);
};

const _load = (key) => {
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
};

export function AuthProvider({ children }) {
  const [user, setUser]             = useState(() => _load(STORAGE_KEYS.USER));
  const [organization, setOrg]      = useState(() => _load(STORAGE_KEYS.ORGANIZATION));
  const [isLoading, setLoading]     = useState(false);
  const [isInitialised, setInitd]   = useState(false);
  const [error, setError]           = useState(null);

  /** Hydrate session from localStorage on mount */
  useEffect(() => {
    const token = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN);
    if (!token) { setInitd(true); return; }
    // Optionally validate token with /me; here we trust localStorage
    setInitd(true);
  }, []);

  const _setSession = useCallback((data) => {
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN,  data.access_token);
    localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, data.refresh_token);
    _persist(STORAGE_KEYS.USER,         data.user);
    _persist(STORAGE_KEYS.ORGANIZATION, data.organization);
    setUser(data.user);
    setOrg(data.organization);
  }, []);

  const _clearSession = useCallback(() => {
    Object.values(STORAGE_KEYS).forEach((k) => localStorage.removeItem(k));
    setUser(null);
    setOrg(null);
  }, []);

  const login = useCallback(async (email, password) => {
    setLoading(true);
    setError(null);
    try {
      const data = await authApi.login(email, password);
      _setSession(data);
      return { ok: true };
    } catch (err) {
      const msg = err.message || 'Login failed.';
      setError(msg);
      return { ok: false, error: msg };
    } finally {
      setLoading(false);
    }
  }, [_setSession]);

  const register = useCallback(async (payload) => {
    setLoading(true);
    setError(null);
    try {
      const data = await authApi.register(payload);
      _setSession(data);
      return { ok: true };
    } catch (err) {
      const msg = err.message || 'Registration failed.';
      setError(msg);
      return { ok: false, error: msg };
    } finally {
      setLoading(false);
    }
  }, [_setSession]);

  const logout = useCallback(async () => {
    try { await authApi.logout(); } catch { /* ignore */ }
    localStorage.removeItem(STORAGE_KEYS.IMPERSONATION_ORIGINAL);
    _clearSession();
  }, [_clearSession]);

  // ── Seller Portal impersonation (Phase 2) ──────────────────────
  // Real "Login As": stashes the Seller's own session, then swaps in the
  // customer admin's session returned by POST /seller/customers/{id}/impersonate.
  // No silent impersonation — every call is server-side audited (see
  // core/deps.require_seller_action + services/seller/customer_service.impersonate)
  // before this ever runs.
  const impersonate = useCallback((sessionData) => {
    const accessToken = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN);
    const refreshToken = localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN);
    if (accessToken) {
      localStorage.setItem(STORAGE_KEYS.IMPERSONATION_ORIGINAL, JSON.stringify({
        access_token: accessToken, refresh_token: refreshToken, user, organization,
      }));
    }
    // Impersonation tokens are intentionally short-lived and carry no
    // refresh_token (see customer_service.impersonate docstring) — store
    // whatever refresh token is present (none, today) so a later phase
    // that adds one doesn't need another AuthContext change.
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, sessionData.access_token);
    if (sessionData.refresh_token) localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, sessionData.refresh_token);
    else localStorage.removeItem(STORAGE_KEYS.REFRESH_TOKEN);
    _persist(STORAGE_KEYS.USER, sessionData.user);
    _persist(STORAGE_KEYS.ORGANIZATION, sessionData.organization);
    setUser(sessionData.user);
    setOrg(sessionData.organization);
  }, [user, organization]);

  // Real SAML SSO (SS5) — the /sso/callback page exchanges its one-time
  // handoff code for a normal TokenResponse (see authApi.ssoExchange) and
  // hands it here; this is a genuine new sign-in, not impersonation, so it
  // just calls the same _setSession() login()/register() use rather than
  // impersonate()'s stash-and-swap.
  const completeSsoLogin = useCallback((data) => { _setSession(data); }, [_setSession]);

  // Called after POST /auth/change-password succeeds — patches the flag
  // that forced the redirect so ProtectedRoute lets the user through on
  // the very next render, without a full re-login round trip.
  const clearMustChangePassword = useCallback(() => {
    setUser((prev) => {
      if (!prev) return prev;
      const next = { ...prev, must_change_password: false };
      _persist(STORAGE_KEYS.USER, next);
      return next;
    });
  }, []);

  const isImpersonating = !!_load(STORAGE_KEYS.IMPERSONATION_ORIGINAL);

  const exitImpersonation = useCallback(() => {
    const stashed = _load(STORAGE_KEYS.IMPERSONATION_ORIGINAL);
    if (!stashed) return;
    localStorage.removeItem(STORAGE_KEYS.IMPERSONATION_ORIGINAL);
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, stashed.access_token);
    if (stashed.refresh_token) localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, stashed.refresh_token);
    _persist(STORAGE_KEYS.USER, stashed.user);
    _persist(STORAGE_KEYS.ORGANIZATION, stashed.organization);
    setUser(stashed.user);
    setOrg(stashed.organization);
  }, []);

  const value = {
    user,
    organization,
    isLoading,
    isInitialised,
    error,
    login,
    register,
    logout,
    impersonate,
    completeSsoLogin,
    exitImpersonation,
    isImpersonating,
    clearMustChangePassword,
    isAuthenticated: !!user,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
