/**
 * Audit Log API service — wraps all /api/audit-logs/* endpoints.
 * Components must never call apiClient directly; go through this module.
 * This is a merged, read-only view over every audit trail the backend
 * already keeps (Users/Roles/Incidents' own tables) — see backend
 * app/services/audit_log_service.py for the full source map.
 */
import apiClient from './apiClient.js';

/** Strips undefined/''/'All' values so they don't pollute the query string */
function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const auditLogApi = {
  /** Paginated, filterable, searchable merged audit trail. */
  list: (params = {}) => apiClient.get('/audit-logs', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/audit-logs/${id}`).then((r) => r.data),

  /**
   * Downloads a CSV or Excel export of the (optionally filtered) audit
   * trail. Returns a Blob — caller is responsible for triggering the save.
   */
  export: (format, params = {}) =>
    apiClient
      .get('/audit-logs/export', { params: cleanParams({ ...params, format }), responseType: 'blob' })
      .then((r) => r.data),
};
