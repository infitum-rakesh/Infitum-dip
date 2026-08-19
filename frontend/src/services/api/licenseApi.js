/**
 * License API service — wraps all /api/licenses/* endpoints.
 * Components must never call apiClient directly; go through this module.
 * Read-only: Administration -> Licenses never writes anything here — the
 * Seller controls purchased modules/plan/limits/expiration, and org admins
 * only control per-user module access (Administration -> Users).
 */
import apiClient from './apiClient.js';

export const licenseApi = {
  /** License Dashboard: status, plan, dates, users allowed/used, usage summary. */
  getDashboard: () => apiClient.get('/licenses').then((r) => r.data),

  /** Per-module: purchased / enabled / usage / limit / expiry. */
  getModules: () => apiClient.get('/licenses/modules').then((r) => r.data),

  /** Usage-only view (same shape as dashboard.usage, fetchable standalone). */
  getUsage: () => apiClient.get('/licenses/usage').then((r) => r.data),

  /** Trial/status view: is_trial, trial_start/end, days_remaining, usage_limits. */
  getStatus: () => apiClient.get('/licenses/status').then((r) => r.data),
};
