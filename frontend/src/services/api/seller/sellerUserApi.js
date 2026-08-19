/**
 * Seller Portal -> Seller Users API — wraps /api/seller/users/* (Phase 7).
 * Manages the Seller's OWN staff accounts — see
 * backend/app/services/seller/seller_user_service.py. Nothing here is
 * mocked; Invite/Reset Password return a one-time temp_password since no
 * email service exists in this codebase (same convention as userAdminApi.js).
 */
import apiClient from '../apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const sellerUserApi = {
  getStats: () => apiClient.get('/seller/users/stats').then((r) => r.data),

  /** params: { q, seller_role, status, page, page_size, sort_by, sort_dir } */
  list: (params = {}) => apiClient.get('/seller/users', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/seller/users/${id}`).then((r) => r.data),

  invite: (body) => apiClient.post('/seller/users', body).then((r) => r.data),

  update: (id, body) => apiClient.put(`/seller/users/${id}`, body).then((r) => r.data),

  delete: (id) => apiClient.delete(`/seller/users/${id}`),

  disable: (id) => apiClient.post(`/seller/users/${id}/disable`).then((r) => r.data),

  enable: (id) => apiClient.post(`/seller/users/${id}/enable`).then((r) => r.data),

  resetPassword: (id) => apiClient.post(`/seller/users/${id}/reset-password`).then((r) => r.data),

  changeRole: (id, sellerRole) => apiClient.post(`/seller/users/${id}/role`, { seller_role: sellerRole }).then((r) => r.data),

  getActivity: (id) => apiClient.get(`/seller/users/${id}/activity`).then((r) => r.data),

  getLoginHistory: (id) => apiClient.get(`/seller/users/${id}/login-history`).then((r) => r.data),
};
