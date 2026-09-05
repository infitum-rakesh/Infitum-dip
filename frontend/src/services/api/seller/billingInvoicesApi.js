/**
 * Seller Portal -> Billing Invoices/Payments API — wraps /api/billing/
 * invoices/* and /api/billing/payments/* (SB14/SB26). Real data from
 * backend/app/api/billing/invoices.py — every row here is a genuine
 * Invoice/Payment/InvoiceLineItem the platform's own billing lifecycle
 * (subscription create/renew/extend/change-plan, or a real payment
 * webhook) wrote, never mocked.
 */
import apiClient from '../apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const billingInvoicesApi = {
  /** params: { organization_id, status, page, page_size } */
  listInvoices: (params = {}) => apiClient.get('/billing/invoices', { params: cleanParams(params) }).then((r) => r.data),

  getInvoice: (id) => apiClient.get(`/billing/invoices/${id}`).then((r) => r.data),

  /** Downloads the invoice PDF. Returns a Blob. */
  downloadInvoicePdf: (id) => apiClient.get(`/billing/invoices/${id}/pdf`, { responseType: 'blob' }).then((r) => r.data),

  createPaymentLink: (id) => apiClient.post(`/billing/invoices/${id}/payment-link`).then((r) => r.data),

  /** params: { organization_id, page, page_size } */
  listPayments: (params = {}) => apiClient.get('/billing/payments', { params: cleanParams(params) }).then((r) => r.data),

  getPayment: (id) => apiClient.get(`/billing/payments/${id}`).then((r) => r.data),
};
