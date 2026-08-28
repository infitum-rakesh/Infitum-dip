/**
 * License API service — wraps all /api/licenses/* endpoints.
 * Components must never call apiClient directly; go through this module.
 * Mostly read-only: Administration -> Licenses never edits modules/plan/
 * limits/expiration — the Seller controls those, and org admins only
 * control per-user module access (Administration -> Users). The one write
 * this page offers is contactSales(), a "please connect me with Sales"
 * inquiry — not a self-service purchase.
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

  /** Sends a module-purchase inquiry to the Seller's contact email. Returns {delivered, detail}. */
  contactSales: (payload) => apiClient.post('/licenses/contact-sales', payload).then((r) => r.data),
};
