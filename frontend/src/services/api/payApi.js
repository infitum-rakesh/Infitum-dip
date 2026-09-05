/**
 * Public Custom Gateway checkout API — wraps GET/POST /api/pay/{token}
 * (backend/app/api/payments/pay.py). Deliberately public/no-auth: the
 * signed token itself is the credential, same as any real hosted-
 * checkout link. Used by the public /pay/:token page (SB31 — this page
 * was missing entirely until found live: every "Pay Now"/"Generate
 * Payment Link" button in the app produced a URL that 404'd).
 */
import apiClient from './apiClient.js';

export const payApi = {
  getInvoice: (token) => apiClient.get(`/pay/${token}`).then((r) => r.data),
  submit: (token) => apiClient.post(`/pay/${token}/submit`).then((r) => r.data),
};
