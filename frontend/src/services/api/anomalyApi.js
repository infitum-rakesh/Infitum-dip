/**
 * Anomaly Detection API service — wraps /api/anomaly/*. Real statistical/
 * rule-based detection against a live connection or uploaded file — see
 * backend/app/anomaly/engine.py for how a run actually profiles,
 * baselines, detects and scores. Nothing here is mocked.
 */
import apiClient from './apiClient.js';

/** Detection methods the backend's anomaly/detectors/ package implements
 * (see anomaly/detectors/__init__.py's ALL_DETECTORS) — used to drive the
 * method picker in the job editor (AD7). */
export const DETECTION_METHODS = [
  { id: 'Z_SCORE',          label: 'Z-Score',              category: 'Statistical', desc: 'Flags values more than N standard deviations from the mean. Default threshold: 3.0.' },
  { id: 'MODIFIED_Z_SCORE', label: 'Modified Z-Score',     category: 'Statistical', desc: 'Median/MAD-based — more robust to existing outliers than a plain Z-score. Default threshold: 3.5.' },
  { id: 'IQR',               label: 'IQR',                  category: 'Statistical', desc: 'Flags values outside Q1/Q3 ± a multiplier of the interquartile range. Default multiplier: 1.5.' },
  { id: 'PERCENTILE',        label: 'Percentile',           category: 'Statistical', desc: 'Flags values outside a configurable percentile band (default p1–p99).' },
  { id: 'NULL_RATE',         label: 'Null Rate Spike',      category: 'Missing Value', desc: 'Flags a column whose null rate spiked versus its baseline.' },
  { id: 'FORMAT',            label: 'Format / Regex',       category: 'Format', desc: 'Flags values that don’t match a configured pattern (e.g. email, phone).' },
  { id: 'CATEGORICAL',       label: 'Categorical',          category: 'Categorical', desc: 'Flags unseen categories (vs. baseline) and categories that are rare within the current run.' },
  { id: 'DUPLICATE',         label: 'Duplicate Key',        category: 'Duplicate', desc: 'Flags repeated values in a column configured as a key.' },
  { id: 'DISTRIBUTION',      label: 'Distribution Shift',   category: 'Distribution', desc: 'PSI / Jensen-Shannon divergence between the current run’s distribution and baseline.' },
  { id: 'TIMESERIES',        label: 'Time-Series',          category: 'Time-Series', desc: 'Trend/spike/drop/seasonal/moving-average/rolling-stddev/rate-of-change over a time column.' },
];

export const FEEDBACK_STATUSES = ['TRUE_ANOMALY', 'FALSE_POSITIVE', 'EXPECTED', 'IGNORED'];
export const REPORT_FORMATS = ['CSV', 'XLSX', 'JSON', 'PDF'];

/** Baseline sources anomaly/baseline.py's resolve_baseline() understands
 * (see BaselineSource in backend/app/models/anomaly.py) — drives the
 * Baseline section of the job editor (AD7). */
export const BASELINE_SOURCES = [
  { id: 'PREVIOUS_RUN',   label: 'Previous Run',     desc: 'Compare against the immediately preceding execution, verbatim.' },
  { id: 'LAST_N_RUNS',    label: 'Last N Runs',      desc: 'Numeric baseline fields averaged across the last N executions.' },
  { id: 'LAST_N_DAYS',    label: 'Last N Days',      desc: 'Numeric baseline fields averaged across executions in the last N days.' },
  { id: 'ROLLING_WINDOW', label: 'Rolling Window',   desc: 'Same averaging as Last N Runs, recomputed fresh every run.' },
  { id: 'MANUAL',         label: 'Manual',           desc: 'Never auto-computed — only an explicitly approved baseline version is used.' },
];

export const anomalyApi = {
  // ── Jobs ─────────────────────────────────────────────────────
  // NOTE: GET /anomaly/jobs responds {items, total} (AnomalyJobListOut),
  // unlike reconciliationApi's bare-array convention — unwrapped here so
  // every caller in this file's own module still just gets an array.
  listJobs: () => apiClient.get('/anomaly/jobs').then((r) => r.data.items),

  getJob: (id) => apiClient.get(`/anomaly/jobs/${id}`).then((r) => r.data),

  /** body: { name, description, connection_id, schema_name, object_name, uploaded_file_id, columns, detection_config, baseline_config, threshold_config, on_critical, schedule_frequency, schedule_cron, enabled, retention_days } */
  createJob: (body) => apiClient.post('/anomaly/jobs', body).then((r) => r.data),

  updateJob: (id, body) => apiClient.put(`/anomaly/jobs/${id}`, body).then((r) => r.data),

  deleteJob: (id) => apiClient.delete(`/anomaly/jobs/${id}`),

  /** On-demand column profiling for the job editor (AD7) — the same real
   * two-tier profiler a run uses, called directly against a live source
   * without creating any AnomalyExecution row. body: { connection_id,
   * schema_name, object_name, uploaded_file_id, columns: [{name, data_type}] } */
  previewProfile: (body) => apiClient.post('/anomaly/preview-profile', body).then((r) => r.data),

  // ── Run / executions ─────────────────────────────────────────
  runJob: (id) => apiClient.post(`/anomaly/jobs/${id}/run`).then((r) => r.data),

  listExecutions: (jobId) => apiClient.get(`/anomaly/jobs/${jobId}/executions`).then((r) => r.data),

  getExecution: (executionId) => apiClient.get(`/anomaly/executions/${executionId}`).then((r) => r.data),

  listColumnResults: (executionId) => apiClient.get(`/anomaly/executions/${executionId}/columns`).then((r) => r.data),

  /** params: { severity, anomaly_type, limit, offset } */
  listRecords: (columnResultId, params = {}) =>
    apiClient.get(`/anomaly/column-results/${columnResultId}/records`, { params }).then((r) => r.data),

  // ── Baselines ────────────────────────────────────────────────
  /** params: { column_name } */
  listBaselines: (jobId, params = {}) => apiClient.get(`/anomaly/jobs/${jobId}/baselines`, { params }).then((r) => r.data),

  approveBaseline: (baselineId) => apiClient.post(`/anomaly/baselines/${baselineId}/approve`).then((r) => r.data),

  // ── Feedback ─────────────────────────────────────────────────
  /** body: { status, comment } — status is one of FEEDBACK_STATUSES */
  createFeedback: (recordId, body) => apiClient.post(`/anomaly/records/${recordId}/feedback`, body).then((r) => r.data),

  listFeedback: (recordId) => apiClient.get(`/anomaly/records/${recordId}/feedback`).then((r) => r.data),

  // ── Reports (background jobs — never generated synchronously) ─
  /** body: { format } — one of REPORT_FORMATS */
  createReport: (executionId, body) => apiClient.post(`/anomaly/executions/${executionId}/reports`, body).then((r) => r.data),

  listReports: (executionId) => apiClient.get(`/anomaly/executions/${executionId}/reports`).then((r) => r.data),

  getReport: (reportId) => apiClient.get(`/anomaly/reports/${reportId}`).then((r) => r.data),

  /** Fetch + trigger a browser download of a SUCCEEDED report's real
   * file — same blob-fetch-then-synthetic-<a> pattern
   * sqlWorkspaceApi.js's downloadExport() uses. */
  async downloadReport(reportId, filename) {
    const blob = await apiClient.get(`/anomaly/reports/${reportId}/download`, { responseType: 'blob' }).then((r) => r.data);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || `anomaly_report_${reportId}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  // ── Org-level configuration (AD9) ──────────────────────────────
  // Genuinely wired, not just stored+displayed — see
  // AnomalyJobEditor.jsx's create-new-job path, which reads this to
  // pre-fill a new job's threshold/baseline/retention/on_critical.
  getConfiguration: () => apiClient.get('/anomaly/configuration').then((r) => r.data),

  /** body: partial — { default_score_bands, default_retention_days, default_baseline_source, default_require_baseline_approval, default_create_incident_on_critical } */
  updateConfiguration: (body) => apiClient.put('/anomaly/configuration', body).then((r) => r.data),
};
