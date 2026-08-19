/**
 * Load API service — wraps /api/mappings/{id}/load and /api/loads/*.
 * Real backend target loading (Phase 6 of the data-integration build): a
 * load run streams a saved mapping's real transform SQL from the source
 * and bulk-writes each chunk to the target via a real vendor writer — see
 * backend/app/execution/load_executor.py for the actual engine.
 */
import apiClient from './apiClient.js';

export const loadApi = {
  /** body: { write_mode?: 'APPEND'|'TRUNCATE_INSERT'|'UPSERT', chunk_size? } */
  startLoad: (mappingId, body) => apiClient.post(`/mappings/${mappingId}/load`, body).then((r) => r.data),

  listLoads: (mappingId) => apiClient.get(`/mappings/${mappingId}/loads`).then((r) => r.data),

  getStatus: (runId) => apiClient.get(`/loads/${runId}/status`).then((r) => r.data),

  getMetrics: (runId) => apiClient.get(`/loads/${runId}/metrics`).then((r) => r.data),

  cancel: (runId) => apiClient.post(`/loads/${runId}/cancel`).then((r) => r.data),
};
