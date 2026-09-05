/**
 * Schedule API service — wraps all /api/schedules/* endpoints.
 * Components must never call apiClient directly; go through this module.
 * Schedules execute existing pipelines (see backend schedule_service.py) —
 * this client never talks to a second execution engine.
 */
import apiClient from './apiClient.js';

/** Strips undefined/''/'All' values so they don't pollute the query string */
function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const scheduleApi = {
  /** Header KPIs: active, paused, running today, failed today, next run */
  getStats: () => apiClient.get('/schedules/stats').then((r) => r.data),

  /**
   * Paginated, searchable, filterable schedule list.
   * params: { q, status, environment, frequency, page, page_size, sort_by, sort_dir }
   */
  list: (params = {}) => apiClient.get('/schedules', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/schedules/${id}`).then((r) => r.data),

  create: (body) => apiClient.post('/schedules', body).then((r) => r.data),

  update: (id, body) => apiClient.put(`/schedules/${id}`, body).then((r) => r.data),

  remove: (id) => apiClient.delete(`/schedules/${id}`),

  runNow: (id) => apiClient.post(`/schedules/${id}/run`).then((r) => r.data),

  pause: (id) => apiClient.post(`/schedules/${id}/pause`).then((r) => r.data),

  resume: (id) => apiClient.post(`/schedules/${id}/resume`).then((r) => r.data),

  clone: (id) => apiClient.post(`/schedules/${id}/clone`).then((r) => r.data),

  getRuns: (id, params = {}) => apiClient.get(`/schedules/${id}/runs`, { params: cleanParams(params) }).then((r) => r.data),

  getRun: (id, runId) => apiClient.get(`/schedules/${id}/runs/${runId}`).then((r) => r.data),

  rerun: (id, runId) => apiClient.post(`/schedules/${id}/runs/${runId}/rerun`).then((r) => r.data),

  /** Scheduled Document Intelligence Workflow — cooperative cancellation
   * (checked between files, never mid-file) and the per-file breakdown a
   * document-schedule run detail screen drills into. */
  cancelRun: (id, runId) => apiClient.post(`/schedules/${id}/runs/${runId}/cancel`).then((r) => r.data),

  getRunDocuments: (id, runId, params = {}) =>
    apiClient.get(`/schedules/${id}/runs/${runId}/documents`, { params: cleanParams(params) }).then((r) => r.data),
};
