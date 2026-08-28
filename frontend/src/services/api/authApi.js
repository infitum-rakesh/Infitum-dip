/** Auth API service — wraps all /api/auth/* endpoints */
import apiClient from './apiClient.js';

export const authApi = {
  login: (email, password) =>
    apiClient.post('/auth/login', { email, password }).then((r) => r.data),

  register: (payload) =>
    apiClient.post('/auth/register', payload).then((r) => r.data),

  refresh: (refreshToken) =>
    apiClient.post('/auth/refresh', { refresh_token: refreshToken }).then((r) => r.data),

  logout: () =>
    apiClient.post('/auth/logout').then((r) => r.data),

  forgotPassword: (email) =>
    apiClient.post('/auth/forgot-password', { email }).then((r) => r.data),

  resetPassword: (token, newPassword) =>
    apiClient.post('/auth/reset-password', { token, new_password: newPassword }).then((r) => r.data),

  me: () =>
    apiClient.get('/auth/me').then((r) => r.data),

  /** Self-service change — called both for the forced must_change_password
   * flow (currentPassword omitted, backend skips that check) and a normal
   * voluntary change (currentPassword required). Returns the updated
   * UserOut (must_change_password now false). */
  changePassword: (newPassword, currentPassword) =>
    apiClient.post('/auth/change-password', { new_password: newPassword, current_password: currentPassword || undefined }).then((r) => r.data),

  /**
   * Real SAML 2.0 SSO handoff (see backend app/api/sso.py's POST /acs
   * redirect -> this exchange). `code` is a short-lived, single-use token
   * — never the JWTs themselves — so the actual access/refresh tokens
   * never sit in a URL/browser history.
   */
  ssoExchange: (code) =>
    apiClient.post('/auth/sso/exchange', null, { params: { code } }).then((r) => r.data),
};
