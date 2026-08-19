/**
 * Reconciliation API service — wraps /api/reconciliation/* (Phase 7 of
 * the data-integration build). Real source-vs-target comparison — see
 * backend/app/reconciliation/engine.py for how each comparison type
 * actually runs. Nothing here is mocked.
 */
import apiClient from './apiClient.js';

export const COMPARISON_TYPES = [
  { id: 'COUNT',       label: 'Record Count', desc: 'Compare total row counts. Works at any table size.' },
  { id: 'CHECKSUM',    label: 'Checksum',     desc: 'Order-independent hash of every row — detects any content difference. Works at any table size.' },
  { id: 'KEY_MATCH',   label: 'Key Match',    desc: 'Find rows missing from / extra in the target by key column(s). Needs both sides in memory — capped at 200,000 rows.' },
  { id: 'FIELD_LEVEL', label: 'Field-Level',  desc: 'For matched keys, compare column values and report mismatches. Same 200,000-row cap as Key Match.' },
];

export const reconciliationApi = {
  listJobs: () => apiClient.get('/reconciliation/jobs').then((r) => r.data),

  getJob: (id) => apiClient.get(`/reconciliation/jobs/${id}`).then((r) => r.data),

  /** body: { name, source_connection_id, source_schema, source_object, target_connection_id, target_schema, target_object, key_columns, compare_columns, comparison_types } */
  createJob: (body) => apiClient.post('/reconciliation/jobs', body).then((r) => r.data),

  updateJob: (id, body) => apiClient.put(`/reconciliation/jobs/${id}`, body).then((r) => r.data),

  deleteJob: (id) => apiClient.delete(`/reconciliation/jobs/${id}`),

  runJob: (id) => apiClient.post(`/reconciliation/jobs/${id}/run`).then((r) => r.data),

  listRuns: (jobId) => apiClient.get(`/reconciliation/jobs/${jobId}/runs`).then((r) => r.data),

  getRun: (runId) => apiClient.get(`/reconciliation/runs/${runId}`).then((r) => r.data),

  /** params: { exception_type } */
  getExceptions: (runId, params = {}) =>
    apiClient.get(`/reconciliation/runs/${runId}/exceptions`, { params }).then((r) => r.data),
};
