/**
 * Pipeline API service — CRUD, execution and scheduling.
 * Real implementation: replace mock bodies with apiClient calls.
 *
 * NOTE: the catalogue/list/run methods below (PIPELINE_DEFS, ALL_RUNS) stay
 * mocked — that surface belongs to the separate Pipeline Builder browsing
 * UI and isn't part of this change. `pipelineVersionApi` at the bottom is
 * the REAL backend integration (/api/pipelines/...) used by the Transform
 * Workspace for save/versioning — see backend app/api/pipelines.py.
 */
import apiClient from './apiClient.js';

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// ── Shared mock node builder ────────────────────────────────

function n(id, type, label, x, y, config = {}, status = 'idle') {
  return { id, type, label, position: { x, y }, config, status, metrics: null };
}
function e(id, src, tgt) { return { id, sourceId: src, targetId: tgt }; }

// ── Mock pipeline catalogue ────────────────────────────────────

const PIPELINE_DEFS = [
  {
    id: 'pl_001',
    name: 'Invoice Intelligence Pipeline',
    description: 'PDF invoices → OCR → AI Extraction → Gold layer in Snowflake',
    tags: ['finance', 'invoices', 'di'],
    version: '2.3',
    status: 'active',
    lastRun: new Date(Date.now() - 3_600_000).toISOString(),
    lastRunStatus: 'success',
    schedule: '0 2 * * *',
    nodes: [
      n('n1',  'doc_input',          'PDF Invoices',        60,  180, { sourceType: 'sftp', path: '/invoices/inbox', fileTypes: ['pdf'] }),
      n('n2',  'image_scanner',      'Image Scanner',       300, 180, { deskew: true, denoise: true, enhance: false }),
      n('n3',  'ocr_engine',         'OCR Engine',          540, 180, { engine: 'dip-ocr-v2', language: 'en', detectTables: true }),
      n('n4',  'ai_extraction',      'AI Extraction',       780, 180, { model: 'gpt-4o', schemaMode: 'auto', confidenceThreshold: 85 }),
      n('n5',  'structured_dataset', 'Invoice Dataset',     1020,180, { datasetName: 'INVOICE_RAW', publishMode: 'append' }),
      n('n6',  'filter',             'Filter Valid',        1260,180, { condition: "status != 'void' AND total_amount > 0" }),
      n('n7',  'cast',               'Cast Types',          1500,180, { castings: [{ column: 'invoice_date', type: 'date' }, { column: 'total_amount', type: 'number' }] }),
      n('n8',  'mapping',            'Map to Schema',       1740,180, { mappings: [{ source: 'invoice_number', target: 'INV_NO' }] }),
      n('n9',  'validation_step',    'Validate Quality',    1980,180, { rulesetName: 'INVOICE_RULES', failAction: 'warn', passThreshold: 95 }),
      n('n10', 'reconciliation_step','Reconcile ERP',       2220,180, { compareWith: 'ERP_INVOICE_MASTER', keyColumn: 'invoice_number', tolerance: '0.01' }),
      n('n11', 'target',             'Snowflake Gold',      2460,180, { datasetName: 'INVOICE_GOLD', mode: 'upsert' }),
      n('n12', 'notification',       'Notify Team',         2460,380, { channel: 'email', recipients: 'finance@acme.com', onSuccess: true, onFailure: true }),
    ],
    edges: [
      e('e1','n1','n2'), e('e2','n2','n3'), e('e3','n3','n4'), e('e4','n4','n5'),
      e('e5','n5','n6'), e('e6','n6','n7'), e('e7','n7','n8'), e('e8','n8','n9'),
      e('e9','n9','n10'),e('e10','n10','n11'), e('e11','n11','n12'),
    ],
  },
  {
    id: 'pl_002',
    name: 'Customer Data Quality',
    description: 'Source → Deduplicate → Validate → Snowflake',
    tags: ['crm', 'quality'],
    version: '1.1',
    status: 'active',
    lastRun: new Date(Date.now() - 86_400_000).toISOString(),
    lastRunStatus: 'warning',
    schedule: '0 6 * * 1',
    nodes: [
      n('n1', 'source',          'Customer Raw',       60,  180, { datasetName: 'CUSTOMER_RAW' }),
      n('n2', 'deduplicate',     'Dedup by Email',     300, 180, { keyColumns: 'email', keepFirst: true }),
      n('n3', 'filter',          'Active Only',        540, 180, { condition: "status = 'active'" }),
      n('n4', 'calculated',      'Full Name',          780, 180, { columnName: 'full_name', expression: "first_name || ' ' || last_name" }),
      n('n5', 'validation_step', 'Validate',           1020,180, { rulesetName: 'CUSTOMER_RULES', failAction: 'error', passThreshold: 99 }),
      n('n6', 'target',          'Customer Gold',      1260,180, { datasetName: 'CUSTOMER_GOLD', mode: 'replace' }),
      n('n7', 'notification',    'Alert on Failure',   1260,380, { channel: 'slack', onSuccess: false, onFailure: true }),
    ],
    edges: [
      e('e1','n1','n2'), e('e2','n2','n3'), e('e3','n3','n4'),
      e('e4','n4','n5'), e('e5','n5','n6'), e('e6','n6','n7'),
    ],
  },
  {
    id: 'pl_003',
    name: 'Sales Reconciliation',
    description: 'Oracle → Snowflake source-to-target reconciliation',
    tags: ['finance', 'recon'],
    version: '1.0',
    status: 'paused',
    lastRun: new Date(Date.now() - 7 * 86_400_000).toISOString(),
    lastRunStatus: 'success',
    schedule: '0 5 * * *',
    nodes: [
      n('n1', 'source',               'Oracle Sales',       60,  180, { datasetName: 'ORACLE_SALES_FACT' }),
      n('n2', 'filter',               'Current Month',      300, 180, { condition: "YEAR(sale_date) = 2024 AND MONTH(sale_date) = MONTH(CURRENT_DATE)" }),
      n('n3', 'aggregate',            'Group by Region',    540, 180, { groupBy: 'region', aggregations: [{ column: 'amount', func: 'sum' }] }),
      n('n4', 'reconciliation_step',  'Recon vs Snowflake', 780, 180, { compareWith: 'SNOWFLAKE_SALES_SUMMARY', keyColumn: 'region', tolerance: '0.01' }),
      n('n5', 'target',               'Recon Report',       1020,180, { datasetName: 'RECON_RESULTS', mode: 'replace' }),
    ],
    edges: [
      e('e1','n1','n2'), e('e2','n2','n3'), e('e3','n3','n4'), e('e4','n4','n5'),
    ],
  },
];

// ── Mock run history ───────────────────────────────────────────

function mockRun(id, pipelineId, pipelineName, status, minsAgo, records, trigger = 'Schedule') {
  const startedAt = new Date(Date.now() - minsAgo * 60_000);
  const duration  = status === 'running' ? null : Math.round(30 + Math.random() * 300);
  return {
    id,
    pipelineId,
    pipelineName,
    status,
    trigger,
    startedAt: startedAt.toISOString(),
    endedAt:   duration ? new Date(startedAt.getTime() + duration * 1000).toISOString() : null,
    durationSec: duration,
    recordsIn:  records,
    recordsOut: status === 'failed' ? Math.round(records * 0.6) : Math.round(records * 0.98),
    recordsFailed: status === 'failed' ? Math.round(records * 0.4) : status === 'warning' ? Math.round(records * 0.02) : 0,
  };
}

const ALL_RUNS = [
  mockRun('run_011', 'pl_001', 'Invoice Intelligence Pipeline',  'running', 2,    9850, 'Schedule'),
  mockRun('run_010', 'pl_001', 'Invoice Intelligence Pipeline',  'success', 62,   9480, 'Manual'),
  mockRun('run_009', 'pl_001', 'Invoice Intelligence Pipeline',  'success', 1502, 8920, 'Schedule'),
  mockRun('run_008', 'pl_001', 'Invoice Intelligence Pipeline',  'warning', 2882, 9110, 'Schedule'),
  mockRun('run_007', 'pl_001', 'Invoice Intelligence Pipeline',  'failed',  5042, 9230, 'API'),
  mockRun('run_006', 'pl_002', 'Customer Data Quality',          'success', 1502, 41230, 'Schedule'),
  mockRun('run_005', 'pl_002', 'Customer Data Quality',          'warning', 4322, 40810, 'Schedule'),
  mockRun('run_004', 'pl_003', 'Sales Reconciliation',           'success', 10262,88420, 'Schedule'),
  mockRun('run_003', 'pl_003', 'Sales Reconciliation',           'success', 24582,91230, 'Schedule'),
  mockRun('run_002', 'pl_002', 'Customer Data Quality',          'success', 34342,39800, 'Manual'),
  mockRun('run_001', 'pl_001', 'Invoice Intelligence Pipeline',  'success', 50102,8640, 'Schedule'),
];

// ── Service ────────────────────────────────────────────────────

export const pipelineApi = {
  async list() {
    await delay(400);
    return PIPELINE_DEFS.map(({ nodes, edges, ...meta }) => ({
      ...meta,
      nodeCount: nodes.length,
      edgeCount: edges.length,
    }));
  },

  async get(id) {
    await delay(300);
    const found = PIPELINE_DEFS.find((p) => p.id === id);
    if (!found) throw new Error(`Pipeline ${id} not found`);
    return { ...found, nodes: found.nodes.map((n) => ({ ...n })), edges: found.edges.map((e) => ({ ...e })) };
  },

  async create(data) {
    await delay(300);
    return { id: `pl_${Date.now()}`, version: '1.0', status: 'active', ...data };
  },

  async update(id, data) {
    await delay(400);
    return { id, ...data };
  },

  async clone(id) {
    await delay(500);
    const src = PIPELINE_DEFS.find((p) => p.id === id);
    return { ...(src ?? {}), id: `pl_${Date.now()}`, name: `${src?.name ?? 'Pipeline'} (Copy)`, version: '1.0', status: 'active', lastRun: null, lastRunStatus: null };
  },

  async delete(id) {
    await delay(300);
    return { ok: true };
  },

  async run(id) {
    await delay(400);
    return { runId: `run_${Date.now()}`, status: 'running', startedAt: new Date().toISOString() };
  },

  async getRuns(pipelineId) {
    await delay(300);
    const runs = pipelineId
      ? ALL_RUNS.filter((r) => r.pipelineId === pipelineId)
      : ALL_RUNS;
    return runs;
  },

  async getRunDetails(runId) {
    await delay(300);
    return ALL_RUNS.find((r) => r.id === runId) ?? null;
  },

  async schedule(id, cron) {
    await delay(300);
    return { id, schedule: cron };
  },
};

// ── Real backend — Transform Workspace persistence + versioning ────
// Every save creates a NEW version (never overwrites); see
// backend/app/services/pipeline_service.py for the retention/restore rules.

export const pipelineVersionApi = {
  /** List every saved pipeline (metadata only — no nodes/edges). */
  list: () => apiClient.get('/pipelines').then((r) => r.data),

  /** Create a brand-new pipeline → version 1. */
  create: (data) => apiClient.post('/pipelines', data).then((r) => r.data),

  /** Fetch a pipeline + its CURRENT version's definition. */
  get: (id) => apiClient.get(`/pipelines/${id}`).then((r) => r.data),

  /** Save = always creates a new version. Returns the pipeline + new definition. */
  save: (id, data) => apiClient.put(`/pipelines/${id}`, data).then((r) => r.data),

  /** Version history, newest first, current flagged. */
  listVersions: (id) => apiClient.get(`/pipelines/${id}/versions`).then((r) => r.data),

  /** One version's full node/edge definition. */
  getVersion: (id, versionNumber) => apiClient.get(`/pipelines/${id}/versions/${versionNumber}`).then((r) => r.data),

  /** Node/edge level diff between two versions — added / removed / modified. */
  compare: (id, versionA, versionB) =>
    apiClient.get(`/pipelines/${id}/compare`, { params: { version_a: versionA, version_b: versionB } }).then((r) => r.data),

  /** Restore does NOT rewind history — it creates a new version copying the old one forward. */
  restore: (id, versionNumber) => apiClient.post(`/pipelines/${id}/versions/${versionNumber}/restore`).then((r) => r.data),

  /** Real delete — removes the pipeline and every version. */
  delete: (id) => apiClient.delete(`/pipelines/${id}`),
};

// ── Real backend — Pipeline Jobs (/api/pipeline-jobs) ───────────────
// Runs a FULL saved pipeline for real — see
// backend/app/execution/pipeline_job_executor.py. This is what Pipeline
// Builder's Run/Abort actions and live log view are backed by.

export const pipelineJobApi = {
  /** Kicks off a real run of a pipeline's current saved version. */
  run: (pipelineId) => apiClient.post('/pipeline-jobs', null, { params: { pipeline_id: pipelineId } }).then((r) => r.data),

  /** Global or per-pipeline job history, newest first. */
  list: (params = {}) => apiClient.get('/pipeline-jobs', { params }).then((r) => r.data),

  get: (jobId) => apiClient.get(`/pipeline-jobs/${jobId}`).then((r) => r.data),

  /** Pass sinceId to tail only new lines while a job is running. */
  getLogs: (jobId, sinceId) => apiClient.get(`/pipeline-jobs/${jobId}/logs`, { params: sinceId ? { since_id: sinceId } : {} }).then((r) => r.data),

  cancel: (jobId) => apiClient.post(`/pipeline-jobs/${jobId}/cancel`).then((r) => r.data),

  /** Real per-row rejects from this job's dataset target node(s) — each
   * one's actual row data and the real target error (see backend's
   * loading/writers/base.py write_batch_with_fallback / KK2-KK4). */
  getRejectedRecords: (jobId, params = {}) =>
    apiClient.get(`/pipeline-jobs/${jobId}/rejected-records`, { params }).then((r) => r.data),

  /** Downloads the real file a File-target node wrote during this job run
   * (backend/app/api/pipeline_jobs.py's /nodes/{node_id}/download — the
   * exact path recorded on the job, not just "whatever's newest on disk"). */
  async downloadOutputFile(jobId, nodeId, filename) {
    const blob = await apiClient.get(`/pipeline-jobs/${jobId}/nodes/${nodeId}/download`, { responseType: 'blob' }).then((r) => r.data);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || `output_${nodeId}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },
};
