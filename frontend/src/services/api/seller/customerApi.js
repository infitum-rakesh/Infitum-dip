/**
 * Seller Portal -> Customers API — wraps /api/seller/customers/* (Phase 2).
 * Customer == Organization; see backend/app/services/seller/customer_service.py.
 * Nothing here is mocked.
 */
import apiClient from '../apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const customerApi = {
  getStats: () => apiClient.get('/seller/customers/stats').then((r) => r.data),

  /** params: { q, status_filter, plan, page, page_size } */
  list: (params = {}) => apiClient.get('/seller/customers', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/seller/customers/${id}`).then((r) => r.data),

  create: (body) => apiClient.post('/seller/customers', body).then((r) => r.data),

  update: (id, body) => apiClient.put(`/seller/customers/${id}`, body).then((r) => r.data),

  activate: (id) => apiClient.post(`/seller/customers/${id}/activate`).then((r) => r.data),

  suspend: (id) => apiClient.post(`/seller/customers/${id}/suspend`).then((r) => r.data),

  /** Real, heavily-audited impersonation — returns fresh tokens for the
   * customer's admin account. Caller is responsible for actually swapping
   * the stored session tokens (see Customers.jsx's handleImpersonate). */
  impersonate: (id) => apiClient.post(`/seller/customers/${id}/impersonate`).then((r) => r.data),

  getAudit: (id, params = {}) => apiClient.get(`/seller/customers/${id}/audit`, { params: cleanParams(params) }).then((r) => r.data),
};
