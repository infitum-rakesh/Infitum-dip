/**
 * Seller Portal -> License Management API — wraps /api/seller/licenses/*
 * (Phase 5). See backend/app/services/seller/license_service.py. Nothing
 * here is mocked.
 */
import apiClient from '../apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const licenseApi = {
  getStats: () => apiClient.get('/seller/licenses/stats').then((r) => r.data),

  /** params: { customer_id, status_filter, module, page, page_size } */
  list: (params = {}) => apiClient.get('/seller/licenses', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/seller/licenses/${id}`).then((r) => r.data),

  create: (body) => apiClient.post('/seller/licenses', body).then((r) => r.data),

  activate: (id) => apiClient.post(`/seller/licenses/${id}/activate`).then((r) => r.data),

  suspend: (id) => apiClient.post(`/seller/licenses/${id}/suspend`).then((r) => r.data),

  revoke: (id) => apiClient.post(`/seller/licenses/${id}/revoke`).then((r) => r.data),

  extend: (id, days) => apiClient.post(`/seller/licenses/${id}/extend`, { days }).then((r) => r.data),

  renew: (id) => apiClient.post(`/seller/licenses/${id}/renew`).then((r) => r.data),

  changeType: (id, licenseType) => apiClient.post(`/seller/licenses/${id}/change-type`, { license_type: licenseType }).then((r) => r.data),
};
