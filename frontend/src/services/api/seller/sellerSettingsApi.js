/**
 * Seller Portal -> Seller Settings API — wraps /api/seller/settings
 * (Phase 9). Company/Branding/Default License Settings on the singleton
 * Seller row — see backend/app/services/seller/seller_settings_service.py.
 * Plan CRUD reuses planApi from subscriptionApi.js (built Phase 3, never
 * surfaced in any UI until this page's Plans tab) rather than duplicating
 * a second client for the same endpoints.
 */
import apiClient from '../apiClient.js';

export const sellerSettingsApi = {
  get: () => apiClient.get('/seller/settings').then((r) => r.data),

  /** body: { name?, legal_name?, support_email?, billing_email?, website?, branding?, default_trial_days? } */
  update: (body) => apiClient.put('/seller/settings', body).then((r) => r.data),

  /** body: { provider?, host?, port?, username?, password?, api_key?, from_address?, use_tls? } —
   *  provider is "SMTP" or "RESEND"; omit/blank password or api_key to leave it unchanged */
  updateSmtp: (body) => apiClient.put('/seller/settings/smtp', body).then((r) => r.data),

  sendTestEmail: (to) => apiClient.post('/seller/settings/smtp/test', { to }).then((r) => r.data),
};
