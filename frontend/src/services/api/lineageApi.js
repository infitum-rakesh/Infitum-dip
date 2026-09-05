/**
 * Lineage API service — wraps /api/lineage/*. The graph is derived live
 * from the real saved pipeline definition + column mappings on every
 * call (see backend app/lineage/graph.py) — never a separately
 * maintained copy.
 */
import apiClient from './apiClient.js';

export const lineageApi = {
  getOverview: () => apiClient.get('/lineage/overview').then((r) => r.data),

  getPipelineLineage: (pipelineId, version) =>
    apiClient.get(`/lineage/pipeline/${pipelineId}`, { params: version ? { version } : {} }).then((r) => r.data),

  listPipelineVersions: (pipelineId) => apiClient.get(`/lineage/pipeline/${pipelineId}/versions`).then((r) => r.data.items),

  getExecutionLineage: (jobId) => apiClient.get(`/lineage/execution/${jobId}`).then((r) => r.data),

  listColumnMappings: () => apiClient.get('/lineage/column-mappings').then((r) => r.data.items),

  getColumnMappingLineage: (mappingId) => apiClient.get(`/lineage/column-mappings/${mappingId}`).then((r) => r.data),

  // INV22 — real Document -> OCR -> AI/Custom-Engine extraction -> ApInvoice
  // header/lines -> validation -> ERP mapping -> ERP posting ->
  // reconciliation chain for one invoice (see backend graph.py's
  // build_invoice_lineage()).
  getInvoiceLineage: (invoiceId) => apiClient.get(`/lineage/invoices/${invoiceId}`).then((r) => r.data),

  getImpact: (params) => apiClient.get('/lineage/impact', { params }).then((r) => r.data),

  search: (q) => apiClient.get('/lineage/search', { params: { q } }).then((r) => r.data.items),
};

// Node type -> icon/color, mirrors the professional, distinct-icon-per-type
// requirement. Kept here (not duplicated per-component) so every lineage
// view — graph, properties panel, search results — renders a type the
// same way.
export const LINEAGE_NODE_META = {
  PIPELINE: { icon: '⚙️', color: '#4F46E5', bg: '#EEF2FF', label: 'Pipeline' },
  DATABASE: { icon: '🗄️', color: '#0EA5E9', bg: '#F0F9FF', label: 'Database' },
  TABLE: { icon: '📋', color: '#0EA5E9', bg: '#F0F9FF', label: 'Table' },
  VIEW: { icon: '👁️', color: '#0EA5E9', bg: '#F0F9FF', label: 'View' },
  COLUMN: { icon: '🔤', color: '#64748B', bg: '#F8FAFC', label: 'Column' },
  FILE: { icon: '📄', color: '#F59E0B', bg: '#FFFBEB', label: 'File' },
  SFTP: { icon: '📡', color: '#F59E0B', bg: '#FFFBEB', label: 'SFTP' },
  API: { icon: '🔌', color: '#8B5CF6', bg: '#F5F3FF', label: 'API' },
  DOCUMENT: { icon: '📑', color: '#EF4444', bg: '#FEF2F2', label: 'Document' },
  OCR: { icon: '📝', color: '#8B5CF6', bg: '#F5F3FF', label: 'OCR' },
  AI_MODEL: { icon: '🧠', color: '#4F46E5', bg: '#EEF2FF', label: 'AI Model' },
  DATASET: { icon: '🗂️', color: '#10B981', bg: '#ECFDF5', label: 'Dataset' },
  TRANSFORMATION: { icon: '🔀', color: '#F97316', bg: '#FFF7ED', label: 'Transformation' },
  VALIDATION: { icon: '✅', color: '#10B981', bg: '#ECFDF5', label: 'Validation' },
  RECONCILIATION: { icon: '⚖️', color: '#F97316', bg: '#FFF7ED', label: 'Reconciliation' },
  ANOMALY_DETECTION: { icon: '🔎', color: '#8B5CF6', bg: '#F5F3FF', label: 'Anomaly Detection' },
  TARGET: { icon: '🎯', color: '#10B981', bg: '#ECFDF5', label: 'Target' },
  // INV22 — Invoice Intelligence lineage node types.
  INVOICE: { icon: '🧾', color: '#DC2626', bg: '#FEF2F2', label: 'Invoice' },
  ERP_TRANSACTION: { icon: '📤', color: '#F97316', bg: '#FFF7ED', label: 'ERP Transaction' },
};

export const MAPPING_TYPE_LABELS = {
  DIRECT: 'Direct', TRANSFORMED: 'Transformed', DERIVED: 'Derived',
  AGGREGATED: 'Aggregated', LOOKUP: 'Lookup', CONSTANT: 'Constant', SYSTEM_GENERATED: 'System Generated',
};
