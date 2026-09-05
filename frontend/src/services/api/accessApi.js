/**
 * Real-time, multi-subscription-aware access API (SB30) — wraps
 * backend/app/api/access.py. This is a UX convenience layer, not the
 * security boundary: every protected route enforces access independently
 * server-side (see core/deps.require_action(), SB5). Used by
 * SubscriptionExpiredNotice, Sidebar's locked-item state, the all-expired
 * block screen, and the Dashboard's subscription widgets.
 */
import apiClient from './apiClient.js';

export const accessApi = {
  /** Real-time single-module check: { granted, reason, expired_at, message, ... } */
  checkAccess: (module) => apiClient.get('/access/check', { params: { module } }).then((r) => r.data),

  /** { active_modules, expired_modules, expiring_modules, has_tenant_access } */
  getModulesSummary: () => apiClient.get('/access/modules-summary').then((r) => r.data),

  /** The Seller's real Sales contact (Seller Settings -> Sales Contact) */
  getSalesContact: () => apiClient.get('/access/contact').then((r) => r.data),
};
