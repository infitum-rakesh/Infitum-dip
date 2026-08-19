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
    defaultConfig: { datasetName: 'output_dataset', mode: 'replace' },
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
};

/** Ordered groups for the palette */
export const PALETTE_GROUPS = [
  { id: 'IO',         label: 'Sources & Targets' },
  { id: 'DI',         label: 'Document Intelligence' },
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
