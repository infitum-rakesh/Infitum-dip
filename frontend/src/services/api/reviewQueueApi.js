/**
 * Review Queue API service — wraps all /api/review-queue/* endpoints.
 * Components must never call apiClient directly; go through this module.
 */
import apiClient from './apiClient.js';

/** Strips undefined/''/'All' values so they don't pollute the query string */
function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const reviewQueueApi = {
  /** Header KPIs: pending, assigned to me, high priority, overdue, approved/rejected today */
  getStats: () => apiClient.get('/review-queue/stats').then((r) => r.data),

  /**
   * Sidebar badge count — the number of PENDING review items. Must always
   * come from here, never hardcoded in the Sidebar component.
   */
  getCount: () => apiClient.get('/review-queue/count').then((r) => r.data),

  /** Assignable org users — backs the 'Specific User' filter and reassign/escalate pickers */
  getFacets: () => apiClient.get('/review-queue/facets').then((r) => r.data),

  /**
   * Paginated, searchable, filterable queue list.
   * params: { q, status, priority, review_type, assigned, assigned_user_id,
   *           document_type, page, page_size, sort_by, sort_dir }
   */
  list: (params = {}) => apiClient.get('/review-queue', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/review-queue/${id}`).then((r) => r.data),

  assign: (id, userId) => apiClient.post(`/review-queue/${id}/assign`, { user_id: userId }).then((r) => r.data),

  updateField: (id, fieldName, correctedValue, reason) =>
    apiClient.patch(`/review-queue/${id}/fields`, { field_name: fieldName, corrected_value: correctedValue, reason }).then((r) => r.data),

  approve: (id) => apiClient.post(`/review-queue/${id}/approve`).then((r) => r.data),

  reject: (id, reason) => apiClient.post(`/review-queue/${id}/reject`, { reason }).then((r) => r.data),

  escalate: (id, reason, assignToUserId) =>
    apiClient.post(`/review-queue/${id}/escalate`, { reason, assign_to_user_id: assignToUserId }).then((r) => r.data),

  bulkAssign: (reviewIds, userId) =>
    apiClient.post('/review-queue/bulk/assign', { review_ids: reviewIds, user_id: userId }).then((r) => r.data),

  bulkApprove: (reviewIds) =>
    apiClient.post('/review-queue/bulk/approve', { review_ids: reviewIds }).then((r) => r.data),

  bulkReject: (reviewIds, reason) =>
    apiClient.post('/review-queue/bulk/reject', { review_ids: reviewIds, reason }).then((r) => r.data),
};
