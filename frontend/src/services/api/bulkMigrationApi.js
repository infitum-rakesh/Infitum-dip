/**
 * Bulk Object Migration API service — wraps /api/bulk-migration/*
 * (backend/app/api/bulk_migration.py). One reusable Migration Pipeline +
 * a Migration Configuration holding thousands of object rows + an
 * Execution Planner/worker pool — see bulk_migration_service.py and
 * bulk_migration_planner_service.py. Nothing here is mocked; every call
 * hits a real, live-verified backend route.
 */
import apiClient from './apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

/** Real enums — mirror models/bulk_migration.py / bulk_migration_run.py. */
export const OBJECT_TYPES = ['TABLE', 'VIEW', 'FILE'];
export const LOAD_TYPES = ['FULL_REPLACE', 'FULL_APPEND', 'MERGE', 'SCD1', 'SCD2', 'DELTA'];
export const RUN_STATUSES = [
  'PENDING', 'VALIDATING', 'RUNNING', 'PAUSING', 'PAUSED', 'CANCELLING',
  'CANCELLED', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED',
];
export const RUN_STATUS_LABEL = {
  PENDING: 'Pending', VALIDATING: 'Validating', RUNNING: 'Running', PAUSING: 'Pausing…',
  PAUSED: 'Paused', CANCELLING: 'Cancelling…', CANCELLED: 'Cancelled', COMPLETED: 'Completed',
  COMPLETED_WITH_ERRORS: 'Completed with Errors', FAILED: 'Failed',
};
export const RUN_STATUS_VARIANT = {
  PENDING: 'gray', VALIDATING: 'info', RUNNING: 'info', PAUSING: 'warning', PAUSED: 'warning',
  CANCELLING: 'warning', CANCELLED: 'gray', COMPLETED: 'success',
  COMPLETED_WITH_ERRORS: 'warning', FAILED: 'danger',
};
export const TASK_STATUSES = [
  'PENDING', 'QUEUED', 'RUNNING', 'RETRY_PENDING', 'SUCCESS',
  'FAILED', 'SKIPPED', 'CANCELLED', 'BLOCKED', 'STALE',
];
export const TASK_STATUS_VARIANT = {
  PENDING: 'gray', QUEUED: 'gray', RUNNING: 'info', RETRY_PENDING: 'warning', SUCCESS: 'success',
  FAILED: 'danger', SKIPPED: 'gray', CANCELLED: 'gray', BLOCKED: 'warning', STALE: 'danger',
};
export const RUN_ACTIVE_STATUSES = ['PENDING', 'VALIDATING', 'RUNNING', 'PAUSING', 'PAUSED', 'CANCELLING'];

export const bulkMigrationApi = {
  // ── Pipelines ────────────────────────────────────────────────
  /** params: { search, status, page, page_size } */
  listPipelines: (params = {}) => apiClient.get('/bulk-migration/pipelines', { params: cleanParams(params) }).then((r) => r.data),

  getPipeline: (id) => apiClient.get(`/bulk-migration/pipelines/${id}`).then((r) => r.data),

  /** body: { name, description, source_connection_id, target_connection_id,
   * max_workers, batch_size, max_retries, retry_backoff_seconds, continue_on_error } */
  createPipeline: (body) => apiClient.post('/bulk-migration/pipelines', body).then((r) => r.data),

  updatePipeline: (id, body) => apiClient.patch(`/bulk-migration/pipelines/${id}`, body).then((r) => r.data),

  deletePipeline: (id) => apiClient.delete(`/bulk-migration/pipelines/${id}`).then((r) => r.data),

  // ── Objects ──────────────────────────────────────────────────
  /** params: { search, enabled, object_type, load_type, dependency_group, page, page_size } */
  listObjects: (pipelineId, params = {}) =>
    apiClient.get(`/bulk-migration/pipelines/${pipelineId}/objects`, { params: cleanParams(params) }).then((r) => r.data),

  createObject: (pipelineId, body) => apiClient.post(`/bulk-migration/pipelines/${pipelineId}/objects`, body).then((r) => r.data),

  updateObject: (pipelineId, objectId, body) =>
    apiClient.patch(`/bulk-migration/pipelines/${pipelineId}/objects/${objectId}`, body).then((r) => r.data),

  deleteObject: (pipelineId, objectId) =>
    apiClient.delete(`/bulk-migration/pipelines/${pipelineId}/objects/${objectId}`).then((r) => r.data),

  /** body: { object_ids: [...], patch: {...} } -> { affected } */
  bulkUpdateObjects: (pipelineId, objectIds, patch) =>
    apiClient.post(`/bulk-migration/pipelines/${pipelineId}/objects/bulk`, { object_ids: objectIds, patch }).then((r) => r.data),

  /** body: { object_ids: [...] } -> { affected } */
  bulkDeleteObjects: (pipelineId, objectIds) =>
    apiClient.delete(`/bulk-migration/pipelines/${pipelineId}/objects/bulk`, { data: { object_ids: objectIds } }).then((r) => r.data),

  // ── Config import / export ──────────────────────────────────
  /** Streamed CSV/Excel/JSON/YAML import with a preview-vs-commit split
   * (dryRun=true previews without writing). */
  importConfig: (pipelineId, file, { dryRun = false, onProgress } = {}) => {
    const form = new FormData();
    form.append('file', file);
    return apiClient
      .post(`/bulk-migration/pipelines/${pipelineId}/config/import`, form, {
        params: { dry_run: dryRun },
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: onProgress
          ? (evt) => onProgress(evt.total ? Math.round((evt.loaded / evt.total) * 100) : 0)
          : undefined,
      })
      .then((r) => r.data);
  },

  /** Returns { content, content_type, filename } — trigger a download from the caller. */
  exportConfig: (pipelineId, format = 'csv') =>
    apiClient.get(`/bulk-migration/pipelines/${pipelineId}/config/export`, { params: { format }, responseType: 'blob' })
      .then((r) => ({ blob: r.data, filename: /filename="?([^"]+)"?/.exec(r.headers['content-disposition'] || '')?.[1] || `objects.${format}` })),

  // ── Validate / dry-run / run ─────────────────────────────────
  validatePipeline: (pipelineId) => apiClient.post(`/bulk-migration/pipelines/${pipelineId}/validate`).then((r) => r.data),

  dryRunPipeline: (pipelineId) => apiClient.post(`/bulk-migration/pipelines/${pipelineId}/dry-run`).then((r) => r.data),

  runPipeline: (pipelineId) => apiClient.post(`/bulk-migration/pipelines/${pipelineId}/run`).then((r) => r.data),

  // ── Runs ─────────────────────────────────────────────────────
  /** params: { pipeline_id, status, page, page_size } */
  listRuns: (params = {}) => apiClient.get('/bulk-migration/runs', { params: cleanParams(params) }).then((r) => r.data),

  getRun: (runId) => apiClient.get(`/bulk-migration/runs/${runId}`).then((r) => r.data),

  /** params: { status, dependency_level, page, page_size } */
  listTasks: (runId, params = {}) => apiClient.get(`/bulk-migration/runs/${runId}/tasks`, { params: cleanParams(params) }).then((r) => r.data),

  /** since_id log-drawer paging — mirrors JobLogDrawer.jsx's convention. */
  listEvents: (runId, sinceId = 0, limit = 200) =>
    apiClient.get(`/bulk-migration/runs/${runId}/events`, { params: { since_id: sinceId, limit } }).then((r) => r.data),

  pauseRun: (runId) => apiClient.post(`/bulk-migration/runs/${runId}/pause`).then((r) => r.data),

  resumeRun: (runId) => apiClient.post(`/bulk-migration/runs/${runId}/resume`).then((r) => r.data),

  cancelRun: (runId) => apiClient.post(`/bulk-migration/runs/${runId}/cancel`).then((r) => r.data),

  /** body: { include_successful } -> { requeued } */
  retryFailed: (runId, includeSuccessful = false) =>
    apiClient.post(`/bulk-migration/runs/${runId}/retry-failed`, { include_successful: includeSuccessful }).then((r) => r.data),
};
