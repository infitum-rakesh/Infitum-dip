/**
 * Execution API service — wraps /api/pipelines/{id}/run and /api/runs/*.
 * Real backend execution (Phase 3 of the data-integration build): a run
 * extracts one Source Dataset node's data from its live connection, in
 * bounded-memory chunks, to a local staging file — see
 * backend/app/execution/pipeline_executor.py for the actual engine.
 */
import apiClient from './apiClient.js';

export const executionApi = {
  /** body: { node_id, key_column?, chunk_size?, worker_count?, partition_count? } */
  startRun: (pipelineId, body) => apiClient.post(`/pipelines/${pipelineId}/run`, body).then((r) => r.data),

  listRuns: (pipelineId) => apiClient.get(`/pipelines/${pipelineId}/runs`).then((r) => r.data),

  getStatus: (runId) => apiClient.get(`/runs/${runId}/status`).then((r) => r.data),

  getMetrics: (runId) => apiClient.get(`/runs/${runId}/metrics`).then((r) => r.data),

  resume: (runId) => apiClient.post(`/runs/${runId}/resume`).then((r) => r.data),

  cancel: (runId) => apiClient.post(`/runs/${runId}/cancel`).then((r) => r.data),
};
