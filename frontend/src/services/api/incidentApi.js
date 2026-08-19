/**
 * Incident API service — wraps all /api/incidents/* endpoints.
 * Components must never call apiClient directly; go through this module.
 * Incidents centralizes monitoring across the platform but never scrapes
 * other modules itself (see backend incident_service.py) — this client
 * only talks to the incidents backend.
 */
import apiClient from './apiClient.js';

/** Strips undefined/''/'All' values so they don't pollute the query string */
function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const incidentApi = {
  /** Header KPIs: open, critical, high, investigating, resolved, SLA breached */
  getStats: () => apiClient.get('/incidents/stats').then((r) => r.data),

  /** Paginated, searchable, filterable incident list. */
  list: (params = {}) => apiClient.get('/incidents', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/incidents/${id}`).then((r) => r.data),

  create: (body) => apiClient.post('/incidents', body).then((r) => r.data),

  update: (id, body) => apiClient.put(`/incidents/${id}`, body).then((r) => r.data),

  delete: (id) => apiClient.delete(`/incidents/${id}`),

  assign: (id, body) => apiClient.post(`/incidents/${id}/assign`, body).then((r) => r.data),

  startInvestigation: (id) => apiClient.post(`/incidents/${id}/start-investigation`).then((r) => r.data),

  changeSeverity: (id, severity, reason) => apiClient.post(`/incidents/${id}/severity`, { severity, reason }).then((r) => r.data),

  addComment: (id, message) => apiClient.post(`/incidents/${id}/comments`, { message }).then((r) => r.data),

  resolve: (id, resolutionNote) => apiClient.post(`/incidents/${id}/resolve`, { resolution_note: resolutionNote }).then((r) => r.data),

  reopen: (id, reason) => apiClient.post(`/incidents/${id}/reopen`, { reason }).then((r) => r.data),

  close: (id) => apiClient.post(`/incidents/${id}/close`).then((r) => r.data),

  getTimeline: (id) => apiClient.get(`/incidents/${id}/timeline`).then((r) => r.data),

  getEvidence: (id) => apiClient.get(`/incidents/${id}/evidence`).then((r) => r.data),

  addEvidence: (id, body) => apiClient.post(`/incidents/${id}/evidence`, body).then((r) => r.data),
};
