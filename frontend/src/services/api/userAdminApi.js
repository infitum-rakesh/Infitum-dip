/**
 * User admin API service — wraps all /api/admin/users/* endpoints.
 * Components must never call apiClient directly; go through this module.
 * No email service is wired up anywhere in this app — Invite and Reset
 * Password return a one-time temp_password instead of silently emailing
 * one out (see backend user_admin_service.py).
 */
import apiClient from './apiClient.js';

/** Strips undefined/''/'All' values so they don't pollute the query string */
function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const userAdminApi = {
  /** Header KPIs: total, active, inactive, pending, administrators */
  getStats: () => apiClient.get('/admin/users/stats').then((r) => r.data),

  /** Paginated, searchable, filterable user list. */
  list: (params = {}) => apiClient.get('/admin/users', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/admin/users/${id}`).then((r) => r.data),

  /** Returns { user, temp_password } — no email service exists yet. */
  invite: (body) => apiClient.post('/admin/users', body).then((r) => r.data),

  update: (id, body) => apiClient.put(`/admin/users/${id}`, body).then((r) => r.data),

  delete: (id) => apiClient.delete(`/admin/users/${id}`),

  disable: (id) => apiClient.post(`/admin/users/${id}/disable`).then((r) => r.data),

  enable: (id) => apiClient.post(`/admin/users/${id}/enable`).then((r) => r.data),

  /** Returns { user, temp_password }. */
  resetPassword: (id) => apiClient.post(`/admin/users/${id}/reset-password`).then((r) => r.data),

  changeRole: (id, role) => apiClient.post(`/admin/users/${id}/role`, { role }).then((r) => r.data),

  getActivity: (id) => apiClient.get(`/admin/users/${id}/activity`).then((r) => r.data),

  getLoginHistory: (id) => apiClient.get(`/admin/users/${id}/login-history`).then((r) => r.data),
};
