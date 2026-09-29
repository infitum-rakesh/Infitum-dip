/**
 * Shared node-type catalogue.
 * Single source of truth for every transform node's identity, colour and defaults.
 * Consumed by PipelineNode, PipelineToolbar, PipelineCanvas and TransformationDesigner.
 */

export const NODE_TYPES = {
  // ── Sources / Targets ──────────────────────────────────────
  source: {
    label: 'Source Dataset',
    group: 'IO',
    icon: '🗂️',
    color: '#3B82F6',
    bg: '#EFF6FF',
    hasInput: false,
    hasOutput: true,
    // objectType: TABLE | VIEW | QUERY are real (browse/preview backed by
    // connectors/database — see SourceDatasetConfig.jsx); STORED_PROCEDURE/
    // API/FILE/SFTP_FILE are listed per the spec but not wired to a real
    // connector yet — the config UI marks them "later phase" rather than
    // pretending they work. loadType only applies to Salesforce sources
    // (BULK/AUTO/ROW_BY_ROW — Salesforce Bulk API vs REST); every other
    // connection type ignores it.
    defaultConfig: {
      connectionId: null,
      objectType: 'TABLE',
      schema: '',
      object: '',
      customSql: '',
      parameters: [],       // [{ name, value }] — named binds for customSql
      batchSize: 20000,
      loadType: 'AUTO',     // BULK | AUTO | ROW_BY_ROW
    },
  },
  target: {
    label: 'Output Dataset',
    group: 'IO',
    icon: '🎯',
    color: '#10B981',
    bg: '#ECFDF5',
    hasInput: true,
    hasOutput: false,
    defaultConfig: {
      datasetName: 'output_dataset',
      loadMode: 'FULL',
      loadStrategy: 'CREATE_IF_NOT_EXISTS',
      loadType: 'full',
      fullLoadStrategy: 'create_if_not_exists',
      mode: 'append',
    },
  },

  // ── Transformation nodes ───────────────────────────────────
  filter: {
    label: 'Filter',
    group: 'TRANSFORM',
    icon: '🔽',
    color: '#F59E0B',
    bg: '#FFFBEB',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { condition: '', description: '' },
  },
  select: {
    label: 'Select Columns',
    group: 'TRANSFORM',
    icon: '☑️',
    color: '#8B5CF6',
    bg: '#F5F3FF',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { columns: '', mode: 'include' },
  },
  rename: {
    label: 'Rename',
    group: 'TRANSFORM',
    icon: '✏️',
    color: '#6366F1',
    bg: '#EEF2FF',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { mappings: [] },
  },
  cast: {
    label: 'Cast / Type',
    group: 'TRANSFORM',
    icon: '🔄',
    color: '#EC4899',
    bg: '#FDF2F8',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { castings: [] },
  },
  sort: {
    label: 'Sort',
    group: 'TRANSFORM',
    icon: '↕️',
    color: '#0EA5E9',
    bg: '#F0F9FF',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { sortBy: '', direction: 'asc' },
  },
  deduplicate: {
    label: 'Deduplicate',
    group: 'TRANSFORM',
    icon: '⊟',
    color: '#14B8A6',
    bg: '#F0FDFA',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { keyColumns: '', keepFirst: true },
  },

  // ── Enrichment ─────────────────────────────────────────────
  join: {
    label: 'Join',
    group: 'ENRICH',
    icon: '🔗',
    color: '#F97316',
    bg: '#FFF7ED',
    hasInput: true,
    hasOutput: true,
    inputCount: 2,
    defaultConfig: { joinType: 'inner', leftKey: '', rightKey: '', rightDataset: '' },
  },
  lookup: {
    label: 'Lookup',
    group: 'ENRICH',
    icon: '🔍',
    color: '#EAB308',
    bg: '#FEFCE8',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { lookupDataset: '', lookupKey: '', returnColumns: '' },
  },
  aggregate: {
    label: 'Aggregate',
    group: 'ENRICH',
    icon: '∑',
    color: '#6366F1',
    bg: '#EEF2FF',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { groupBy: '', aggregations: [] },
  },

  // ── Expression ─────────────────────────────────────────────
  calculated: {
    label: 'Calculated Column',
    group: 'EXPRESSION',
    icon: 'ƒ',
    color: '#4F46E5',
    bg: '#EEF2FF',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { columnName: '', expression: '', type: 'string' },
  },
  conditional: {
    label: 'Conditional Logic',
    group: 'EXPRESSION',
    icon: '⚡',
    color: '#DC2626',
    bg: '#FEF2F2',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { conditions: [] },
  },
  mapping: {
    label: 'Column Mapping',
    group: 'EXPRESSION',
    icon: '↔️',
    color: '#059669',
    bg: '#ECFDF5',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { mappings: [] },
  },

  // ── Document Intelligence ──────────────────────────────────
  doc_input: {
    label: 'Document Input',
    group: 'DI',
    icon: '📥',
    color: '#EF4444',
    bg: '#FEF2F2',
    hasInput: false,
    hasOutput: true,
    defaultConfig: { sourceType: 'upload', path: '', fileTypes: ['pdf', 'jpg', 'png', 'tiff'] },
  },
  image_scanner: {
    label: 'Image Scanner',
    group: 'DI',
    icon: '🔬',
    color: '#F59E0B',
    bg: '#FFFBEB',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { deskew: true, denoise: true, enhance: false, detectBlank: true },
  },
  ocr_engine: {
    label: 'OCR Engine',
    group: 'DI',
    icon: '📝',
    color: '#8B5CF6',
    bg: '#F5F3FF',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { engine: 'dip-ocr-v2', language: 'en', detectTables: true, handwriting: false },
  },
  ai_extraction: {
    label: 'AI Extraction',
    group: 'DI',
    icon: '🧠',
    color: '#4F46E5',
    bg: '#EEF2FF',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { model: 'gpt-4o', schemaMode: 'auto', confidenceThreshold: 85, reviewBelow: 70 },
  },
  structured_dataset: {
    label: 'Structured Dataset',
    group: 'DI',
    icon: '🗂️',
    color: '#10B981',
    bg: '#ECFDF5',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { datasetName: '', publishMode: 'overwrite' },
  },

  // ── Quality & Compliance ───────────────────────────────────
  validation_step: {
    label: 'Validation',
    group: 'QUALITY',
    icon: '✅',
    color: '#10B981',
    bg: '#ECFDF5',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { rulesetName: '', failAction: 'warn', passThreshold: 95 },
  },
  reconciliation_step: {
    label: 'Reconciliation',
    group: 'QUALITY',
    icon: '⚖️',
    color: '#F97316',
    bg: '#FFF7ED',
    hasInput: true,
    hasOutput: true,
    defaultConfig: { compareWith: '', keyColumn: '', tolerance: '0.01' },
  },
  anomaly_detection_step: {
    label: 'Anomaly Detection',
    group: 'QUALITY',
    icon: '🔎',
    color: '#8B5CF6',
    bg: '#F5F3FF',
    hasInput: true,
    hasOutput: true,
    // jobId set = real statistical/rule-based check against this node's
    // real in-flight data, using that saved Anomaly Job's own columns/
    // methods/baseline/threshold (persists as a real AnomalyExecution,
    // trigger="Pipeline" — visible in Anomaly Results). jobId unset =
    // same real detection from columnNames/methods below, cold-start
    // baseline (nothing persisted, no AnomalyJob to attach it to).
    defaultConfig: {
      enabled: true,
      jobId: null,
      columnNames: [],
      methods: [],
      idColumn: '',
      onCritical: { create_incident: false, stop_pipeline: false },
    },
  },

  // Distinct from the existing `mapping` node above (EXPRESSION group) —
  // that one is an inline pandas rename with no relationship to the real
  // Column Mapping module. This node instead references a saved, real
  // ColumnMapping by (mappingId, mappingVersion) — only a PUBLISHED
  // version can be attached (enforced server-side too, never just here)
  // — and the pipeline executor resolves and applies that EXACT immutable
  // version's real transform/lookup/constant/default rules when the job
  // runs (execution/mapping_execution.py). See ColumnMappingConfig.jsx.
  column_mapping_step: {
    label: 'Column Mapping',
    group: 'QUALITY',
    icon: '🔗',
    color: '#0EA5E9',
    bg: '#F0F9FF',
    hasInput: true,
    hasOutput: true,
    // Deliberately just a reference — never the mapping's own definition.
    // Unmapped-column policy (IGNORE/ERROR/WARNING) lives on the
    // ColumnMappingVersion itself (set when the mapping was published),
    // not duplicated here — a node-level override would let a pipeline
    // silently disagree with what the mapping was actually validated
    // and published against.
    defaultConfig: { mappingId: null, mappingVersion: null },
  },

  // ── Control Flow ───────────────────────────────────────────
  notification: {
    label: 'Notification',
    group: 'CONTROL',
    icon: '🔔',
    color: '#6366F1',
    bg: '#EEF2FF',
    hasInput: true,
    hasOutput: false,
    defaultConfig: { channel: 'email', recipients: '', onSuccess: true, onFailure: true },
  },

  // ── Invoice Intelligence (INV24/25) ─────────────────────────
  // 6 nodes, deliberately not 7 — there is no standalone "Anomaly Check"
  // node here: invoice_validation's real decide() call already runs the
  // Anomaly Detection engine (INV23) as an inseparable part of its own
  // decision matrix, so a 7th node would either duplicate that check or
  // do nothing new. Every node below does REAL work when a pipeline job
  // runs (execution/pipeline_job_executor.py) — none is a DI-style
  // honest-passthrough placeholder.
  invoice_intake: {
    label: 'Invoice Intake',
    group: 'INVOICE',
    icon: '📨',
    color: '#0D9488',
    bg: '#F0FDFA',
    hasInput: false,
    hasOutput: true,
    // Reuses the exact same discovery/dedup machinery Document Source
    // Schedules use (document_schedule_service.discover_files() etc.) —
    // connectionId must be a real SFTP/FTP/Network File connection
    // (LOCAL_FILE is retired — see connection_service.py). Path/pattern/
    // recursive mirror a Schedule's own document-source config fields.
    defaultConfig: { connectionId: null, path: '/', filePattern: '', recursive: false, batchSize: 50 },
  },
  invoice_processing: {
    label: 'Invoice Processing',
    group: 'INVOICE',
    icon: '🧾',
    color: '#7C3AED',
    bg: '#F5F3FF',
    hasInput: true,
    hasOutput: true,
    // No config — runs the real OCR + AI/Custom extraction chain
    // (document_service.process_document(skip_invoice_pipeline=True)) on
    // every document_id flowing in. Materialize/Validate/Decide happen
    // downstream in Invoice Validation, exactly once, never here.
    defaultConfig: {},
  },
  invoice_validation: {
    label: 'Invoice Validation',
    group: 'INVOICE',
    icon: '📋',
    color: '#059669',
    bg: '#ECFDF5',
    hasInput: true,
    hasOutput: true,
    // No config — real materialize_invoice(source=PIPELINE) +
    // evaluate_invoice() + decide() per document, using this org's
    // current published validation rules (Rules admin UI, not here).
    defaultConfig: {},
  },
  invoice_review_gate: {
    label: 'Review Gate',
    group: 'INVOICE',
    icon: '🚦',
    color: '#D97706',
    bg: '#FFFBEB',
    hasInput: true,
    hasOutput: true,
    // Pure sync filter — keeps only rows whose real decide() outcome is
    // in passOn. Default matches the spec's example pipeline (only
    // STRAIGHT_THROUGH invoices continue on to ERP posting).
    defaultConfig: { passOn: ['STRAIGHT_THROUGH'] },
  },
  invoice_erp_posting: {
    label: 'ERP Posting',
    group: 'INVOICE',
    icon: '📤',
    color: '#DC2626',
    bg: '#FEF2F2',
    hasInput: true,
    hasOutput: true,
    // Real invoice_erp_service.post_invoice() call (fire-and-forget on
    // the backend, polled here up to settleTimeoutSeconds) against the
    // chosen ERP connection (currently ORACLE_ERP_CLOUD only).
    defaultConfig: { erpConnectionId: null, settleTimeoutSeconds: 60 },
  },
  invoice_reconciliation: {
    label: 'Reconciliation',
    group: 'INVOICE',
    icon: '🧮',
    color: '#0EA5E9',
    bg: '#F0F9FF',
    hasInput: true,
    hasOutput: true,
    // No config — real invoice_reconciliation_service.reconcile_invoice()
    // per invoice that reached this node (a genuine RE-verification even
    // though a successful ERP post already auto-triggers one).
    defaultConfig: {},
  },
};

/** Ordered groups for the palette */
export const PALETTE_GROUPS = [
  { id: 'IO',         label: 'Sources & Targets' },
  { id: 'DI',         label: 'Document Intelligence' },
  { id: 'INVOICE',    label: 'Invoice Intelligence' },
  { id: 'TRANSFORM',  label: 'Transform' },
  { id: 'ENRICH',     label: 'Enrich & Join' },
  { id: 'EXPRESSION', label: 'Expressions' },
  { id: 'QUALITY',    label: 'Quality & Compliance' },
  { id: 'CONTROL',    label: 'Control Flow' },
];

/** Create a new node object with defaults */
export function createNode(type, position = { x: 200, y: 200 }) {
  const def = NODE_TYPES[type];
  if (!def) throw new Error(`Unknown node type: ${type}`);
  return {
    id: `node_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
    type,
    position,
    label: def.label,
    config: { ...def.defaultConfig },
    status: 'idle',   // idle | running | success | error | warning
    metrics: null,    // { rowsIn, rowsOut, duration }
  };
}

/** Create an edge connecting two nodes */
export function createEdge(sourceId, targetId) {
  return {
    id: `edge_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
    sourceId,
    targetId,
  };
}
