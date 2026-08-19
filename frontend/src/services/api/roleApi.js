/**
 * Role API service — wraps all /api/roles/* endpoints.
 * Components must never call apiClient directly; go through this module.
 * Permission categories/actions are always driven by what the API returns
 * (RoleListOut.categories / licensed_categories) — never hardcoded here.
 */
import apiClient from './apiClient.js';

export const roleApi = {
  /** items + total + the full category list + which ones are actually licensed. */
  list: () => apiClient.get('/roles').then((r) => r.data),

  get: (id) => apiClient.get(`/roles/${id}`).then((r) => r.data),

  create: (body) => apiClient.post('/roles', body).then((r) => r.data),

  update: (id, body) => apiClient.put(`/roles/${id}`, body).then((r) => r.data),

  delete: (id) => apiClient.delete(`/roles/${id}`),

  assignUser: (id, userId) => apiClient.post(`/roles/${id}/users`, { user_id: userId }).then((r) => r.data),

  unassignUser: (id, userId) => apiClient.delete(`/roles/${id}/users/${userId}`),

  getAudit: (id) => apiClient.get(`/roles/${id}/audit`).then((r) => r.data),
};
