/**
 * Monitoring API service — wraps /api/monitoring/*. Real data throughout:
 * pipeline runs, job detail, data quality, performance and system health
 * are all read live off the existing execution tables (see backend
 * app/monitoring/service.py) — nothing here is mocked.
 */
import apiClient from './apiClient.js';

export const monitoringApi = {
  getOverview: (days = 7) => apiClient.get('/monitoring/overview', { params: { days } }).then((r) => r.data),

  // INV21 — decision outcomes, ERP call/queue metrics, review queue depth,
  // real stage durations derived from Invoice Intelligence's own lifecycle
  // events (see monitoring/repository.py's invoice_processing_overview()).
  getInvoiceProcessing: (days = 7) => apiClient.get('/monitoring/invoice-processing', { params: { days } }).then((r) => r.data),

  // TALLY-J5 — extraction/reconciliation/write-back rollup + Tally-attributable
  // review queue depth (see monitoring/repository.py's tally_overview()).
  getTallyOverview: (days = 7) => apiClient.get('/monitoring/tally', { params: { days } }).then((r) => r.data),

  // Phase Group 5 (IV26) — requests-by-check-type/provider, success/
  // mismatch/unavailable rates, retry/stale/needs-attention counts, all
  // derived from VerificationUsage (see monitoring/repository.py's
  // india_verification_overview()).
  getIndiaVerificationOverview: (days = 7) => apiClient.get('/monitoring/india-verification', { params: { days } }).then((r) => r.data),

  listPipelineRuns: (params = {}) => apiClient.get('/monitoring/pipeline-runs', { params }).then((r) => r.data),

  listScheduleRuns: (params = {}) => apiClient.get('/monitoring/schedule-runs', { params }).then((r) => r.data),

  getJobRun: (jobId) => apiClient.get(`/monitoring/job-runs/${jobId}`).then((r) => r.data),

  getDataQuality: (params = {}) => apiClient.get('/monitoring/data-quality', { params }).then((r) => r.data),

  getPerformance: (days = 7) => apiClient.get('/monitoring/performance', { params: { days } }).then((r) => r.data),

  getSystemHealth: () => apiClient.get('/monitoring/system-health').then((r) => r.data),

  // ── SLA Definitions ──
  listSla: () => apiClient.get('/monitoring/sla').then((r) => r.data.items),
  createSla: (body) => apiClient.post('/monitoring/sla', body).then((r) => r.data),
  updateSla: (id, body) => apiClient.put(`/monitoring/sla/${id}`, body).then((r) => r.data),
  deleteSla: (id) => apiClient.delete(`/monitoring/sla/${id}`),

  // ── Alert Rules ──
  listAlertRules: () => apiClient.get('/monitoring/alert-rules').then((r) => r.data.items),
  getAvailableChannels: () => apiClient.get('/monitoring/alert-rules/channels').then((r) => r.data.types),
  createAlertRule: (body) => apiClient.post('/monitoring/alert-rules', body).then((r) => r.data),
  updateAlertRule: (id, body) => apiClient.put(`/monitoring/alert-rules/${id}`, body).then((r) => r.data),
  deleteAlertRule: (id) => apiClient.delete(`/monitoring/alert-rules/${id}`),

  // ── Alert Events (fired alerts / incidents feed) ──
  listAlertEvents: (params = {}) => apiClient.get('/monitoring/alert-events', { params }).then((r) => r.data),
  acknowledgeAlertEvent: (id) => apiClient.post(`/monitoring/alert-events/${id}/acknowledge`).then((r) => r.data),
  resolveAlertEvent: (id) => apiClient.post(`/monitoring/alert-events/${id}/resolve`).then((r) => r.data),
};

export const ALERT_CONDITIONS = [
  { value: 'JOB_FAILED', label: 'Pipeline job failed' },
  { value: 'SLA_BREACHED', label: 'SLA breached' },
  { value: 'SLA_WARNING', label: 'SLA warning' },
  { value: 'REJECT_RATE_ABOVE', label: 'Reject rate above threshold (%)' },
  { value: 'DURATION_ABOVE_MINUTES', label: 'Duration above threshold (minutes)' },
  { value: 'VALIDATION_FAILED', label: 'Validation run failed' },
  { value: 'RECONCILIATION_MISMATCH', label: 'Reconciliation mismatch' },
  { value: 'CONSECUTIVE_FAILURES', label: 'Consecutive failures ≥ threshold' },
  { value: 'ANOMALY_DETECTED', label: 'Anomaly detected (warning or critical)' },
];

export const SLA_STATUS_META = {
  MET: { label: 'Met', variant: 'success' },
  WARNING: { label: 'Warning', variant: 'warning' },
  BREACHED: { label: 'Breached', variant: 'danger' },
  NOT_EVALUATED: { label: 'Not evaluated', variant: 'gray' },
};
