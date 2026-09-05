/**
 * Customer Billing API — org-scoped, read-only view of the current
 * organization's own subscriptions/invoices/payments, plus the
 * self-service "Pay Now" action (spec section 45's Customer Billing
 * screens, SB29). Wraps /api/billing/subscriptions/*, /api/billing/
 * invoices/*, /api/billing/payments/* — the same backend routes the
 * Seller Portal's billing screens use (backend/app/api/billing/*.py),
 * automatically org-scoped server-side for a non-Seller caller. Nothing
 * here is mocked.
 */
import apiClient from './apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const billingApi = {
  /** params: { page, page_size } */
  listMySubscriptions: (params = {}) => apiClient.get('/billing/subscriptions', { params: cleanParams(params) }).then((r) => r.data),

  getSubscriptionModules: (id) => apiClient.get(`/billing/subscriptions/${id}/modules`).then((r) => r.data),

  /** params: { status, page, page_size } */
  listInvoices: (params = {}) => apiClient.get('/billing/invoices', { params: cleanParams(params) }).then((r) => r.data),

  getInvoice: (id) => apiClient.get(`/billing/invoices/${id}`).then((r) => r.data),

  /** Downloads the invoice PDF. Returns a Blob. */
  downloadInvoicePdf: (id) => apiClient.get(`/billing/invoices/${id}/pdf`, { responseType: 'blob' }).then((r) => r.data),

  /** Self-service payment link generation — distinct from the Seller's
   * createPaymentLink (that one is Seller-only, POST .../payment-link). */
  payInvoice: (id) => apiClient.post(`/billing/invoices/${id}/pay`).then((r) => r.data),

  /** params: { page, page_size } */
  listPayments: (params = {}) => apiClient.get('/billing/payments', { params: cleanParams(params) }).then((r) => r.data),

  getPayment: (id) => apiClient.get(`/billing/payments/${id}`).then((r) => r.data),
};
