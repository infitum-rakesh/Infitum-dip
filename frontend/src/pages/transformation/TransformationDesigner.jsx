/**
 * TransformationDesigner — the full visual designer page.
 *
 * Owns all pipeline state (nodes, edges, selection, undo stack, dirty
 * tracking, backend versioning). Delegates rendering to:
 *   PipelineToolbar       — top action bar (name, save, history, zoom)
 *   PipelineCanvas        — interactive canvas
 *   NodePalette           — left drag source
 *   NodeConfigPanel       — right configuration panel (Apply/Cancel draft)
 *   VersionHistoryPanel   — right-side drawer: history / compare / restore
 *
 * No Run button here by design — running a saved pipeline always happens
 * from Pipeline Builder (see pages/pipeline/PipelineBuilder.jsx), which is
 * also where per-run status/logs live. This page only builds and saves.
 */
import { useReducer, useCallback, useState, useEffect, useMemo } from 'react';
import { X, AlertTriangle } from 'lucide-react';
import { PipelineToolbar } from '@/components/pipeline/PipelineToolbar.jsx';
import { PipelineCanvas }  from '@/components/pipeline/PipelineCanvas.jsx';
import { PALETTE_GROUPS, NODE_TYPES, createNode, createEdge } from '@/components/pipeline/nodeTypes.js';
import { SourceDatasetConfig } from '@/components/pipeline/SourceDatasetConfig.jsx';
import { TargetDatasetConfig } from '@/components/pipeline/TargetDatasetConfig.jsx';
import { AnomalyDetectionConfig } from '@/components/pipeline/AnomalyDetectionConfig.jsx';
import { ColumnMappingConfig } from '@/components/pipeline/ColumnMappingConfig.jsx';
import { InvoiceIntakeConfig } from '@/components/pipeline/InvoiceIntakeConfig.jsx';
import { InvoiceErpPostingConfig } from '@/components/pipeline/InvoiceErpPostingConfig.jsx';
import { Button } from '@/components/common/Button.jsx';
import { pipelineVersionApi } from '@/services/api/pipelineApi.js';
import { pipelineFileApi } from '@/services/api/pipelineFileApi.js';
import { VersionHistoryPanel } from './VersionHistoryPanel.jsx';
import './transformation.css';

// ── State management ───────────────────────────────────────────

const INIT_PIPELINE = {
  name: 'Untitled Pipeline',
  nodes: [],
  edges: [],
};

function pipelineReducer(state, action) {
  switch (action.type) {

    case 'SET_NAME':
      return { ...state, name: action.name };

    case 'ADD_NODE': {
      const node = createNode(action.nodeType, action.position);
      return { ...state, nodes: [...state.nodes, node] };
    }

    case 'MOVE_NODE':
      return {
        ...state,
        nodes: state.nodes.map((n) =>
          n.id === action.id ? { ...n, position: action.position } : n
        ),
      };

    case 'DELETE_NODE':
      return {
        ...state,
        nodes: state.nodes.filter((n) => n.id !== action.id),
        edges: state.edges.filter((e) => e.sourceId !== action.id && e.targetId !== action.id),
      };

    case 'UPDATE_NODE_CONFIG':
      return {
        ...state,
        nodes: state.nodes.map((n) =>
          n.id === action.id
            ? { ...n, config: { ...n.config, ...action.patch }, label: action.label ?? n.label }
            : n
        ),
      };

    case 'SET_NODE_STATUS':
      return {
        ...state,
        nodes: state.nodes.map((n) =>
          n.id === action.id ? { ...n, status: action.status, metrics: action.metrics ?? n.metrics } : n
        ),
      };

    case 'ADD_EDGE': {
      const edge = createEdge(action.sourceId, action.targetId);
      return { ...state, edges: [...state.edges, edge] };
    }

    case 'DELETE_EDGE':
      return { ...state, edges: state.edges.filter((e) => e.id !== action.id) };

    case 'CLEAR':
      return { ...state, nodes: [], edges: [] };

    case 'LOAD':
      return { ...state, nodes: action.nodes, edges: action.edges, name: action.name ?? state.name };

    default:
      return state;
  }
}

/** Strips run-time-only fields so re-running a pipeline never counts as an
 * unsaved *definition* change, and so saved versions store structure only. */
function structural(nodes) {
  return nodes.map((n) => ({ ...n, status: 'idle', metrics: null }));
}
function snapshotOf(pipeline) {
  return JSON.stringify({ name: pipeline.name, nodes: structural(pipeline.nodes), edges: pipeline.edges });
}

// ── Node palette ───────────────────────────────────────────────

function NodePalette() {
  const handleDragStart = (e, type) => {
    e.dataTransfer.setData('node-type', type);
    e.dataTransfer.effectAllowed = 'copy';
  };

  return (
    <div className="pc-palette">
      {PALETTE_GROUPS.map((group) => {
        const items = Object.entries(NODE_TYPES).filter(([, def]) => def.group === group.id);
        if (!items.length) return null;
        return (
          <div key={group.id}>
            <div className="pc-palette-section">{group.label}</div>
            {items.map(([type, def]) => (
              <div
                key={type}
                className="pc-palette-item"
                draggable
                onDragStart={(e) => handleDragStart(e, type)}
                title={`Drag to add ${def.label}`}
              >
                <div className="pc-palette-icon" style={{ background: def.bg, color: def.color }}>
                  {def.icon}
                </div>
                <span>{def.label}</span>
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

/** Disclosure banner shown at the top of a node's Configuration section
 * whenever the config UI's fields don't map 1:1 onto what actually
 * happens when a pipeline job runs (see execution/pipeline_job_executor.py)
 * — never let a config panel imply a node does more than it really does. */
function ConfigNotice({ children }) {
  return (
    <div className="pc-config-notice">
      <AlertTriangle size={13} />
      <span>{children}</span>
    </div>
  );
}

// ── Node configuration panel — local draft, committed only on Apply ────

function NodeConfigPanel({ node, onApply, onClose, pipelineId, pipelineDirty, nodes, edges }) {
  const [draft, setDraft] = useState(null); // { label, config }

  // Reset the draft whenever a DIFFERENT node is selected (not on every
  // keystroke, and not when the same node's config changes externally
  // for unrelated reasons like run status).
  useEffect(() => {
    setDraft(node ? { label: node.label, config: { ...node.config } } : null);
  }, [node?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!node || !draft) {
    return (
      <div className="pc-config-panel">
        <div className="pc-config-header">
          <span className="pc-config-title">Properties</span>
        </div>
        <div className="pc-config-body" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: 24 }}>
          Select a node to configure it
        </div>
      </div>
    );
  }

  const def = NODE_TYPES[node.type] ?? {};
  const cfg = draft.config;

  const set = (key, value) => setDraft((d) => ({ ...d, config: { ...d.config, [key]: value } }));
  const setLabel = (label) => setDraft((d) => ({ ...d, label }));

  const isDirty = draft.label !== node.label || JSON.stringify(draft.config) !== JSON.stringify(node.config);

  const handleApply = () => onApply(node.id, draft.label, draft.config);
  const handleCancel = () => setDraft({ label: node.label, config: { ...node.config } });

  return (
    <div className={`pc-config-panel ${node.type === 'source' ? 'pc-config-panel-wide' : ''}`}>
      <div className="pc-config-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 18 }}>{def.icon}</span>
          <span className="pc-config-title">{def.label}</span>
        </div>
        <button className="pc-config-close" onClick={onClose} title="Close properties">
          <X size={15} />
        </button>
      </div>
      <div className="pc-config-body">

        {/* Node label + read-only type */}
        <div className="pc-config-section">
          <div className="pc-config-section-title">General</div>
          <div className="pc-config-row">
            <label className="pc-config-label">Node name</label>
            <input
              className="pc-config-input"
              value={draft.label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>
          <div className="pc-config-row">
            <label className="pc-config-label">Node type</label>
            <div className="pc-config-readonly">{def.label}</div>
          </div>
        </div>

        {/* Type-specific config */}
        <div className="pc-config-section">
          <div className="pc-config-section-title">Configuration</div>

          {/* Source Dataset — real, connection-backed schema/object browsing + preview */}
          {node.type === 'source' && (
            <SourceDatasetConfig cfg={cfg} set={set} pipelineId={pipelineId} nodeId={node.id} pipelineDirty={pipelineDirty || isDirty} />
          )}

          {/* Target — Dataset (DB via BulkWriter) / File (local disk) /
              SFTP (real upload) / API (real batched POST/PUT/PATCH) */}
          {node.type === 'target' && (
            <TargetDatasetConfig cfg={cfg} set={set} nodeId={node.id} nodes={nodes} edges={edges} />
          )}

          {/* Filter */}
          {node.type === 'filter' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Condition <span style={{ color: 'var(--color-danger)' }}>*</span></label>
              <input className="pc-config-input" style={{ fontFamily: 'var(--font-mono)' }} value={cfg.condition ?? ''} onChange={(e) => set('condition', e.target.value)} placeholder="e.g. amount > 0 AND status != 'void'" />
              <span style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>Use SQL-style expressions</span>
            </div>
          )}

          {/* Select */}
          {node.type === 'select' && (
            <>
              <div className="pc-config-row">
                <label className="pc-config-label">Mode</label>
                <select className="pc-config-select" value={cfg.mode ?? 'include'} onChange={(e) => set('mode', e.target.value)}>
                  <option value="include">Include columns</option>
                  <option value="exclude">Exclude columns</option>
                </select>
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Columns (comma-separated)</label>
                <textarea className="pc-config-textarea" value={cfg.columns ?? ''} onChange={(e) => set('columns', e.target.value)} placeholder="col1, col2, col3" />
              </div>
            </>
          )}

          {/* Rename */}
          {node.type === 'rename' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Mappings (old → new, one per line)</label>
              <textarea className="pc-config-textarea" style={{ fontFamily: 'var(--font-mono)' }} value={Array.isArray(cfg.mappings) ? cfg.mappings.map((m) => `${m.from} → ${m.to}`).join('\n') : ''} onChange={(e) => {
                const mappings = e.target.value.split('\n').filter(Boolean).map((line) => {
                  const [from, , to] = line.split(' ');
                  return { from: (from ?? '').trim(), to: (to ?? '').trim() };
                });
                set('mappings', mappings);
              }} placeholder={"old_col → new_col\nfirst_name → firstName"} />
            </div>
          )}

          {/* Cast */}
          {node.type === 'cast' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Castings (column:type, one per line)</label>
              <textarea className="pc-config-textarea" style={{ fontFamily: 'var(--font-mono)' }} value={Array.isArray(cfg.castings) ? cfg.castings.map((c) => `${c.column}:${c.type}`).join('\n') : ''} onChange={(e) => {
                const castings = e.target.value.split('\n').filter(Boolean).map((line) => {
                  const [column, type] = line.split(':');
                  return { column: (column ?? '').trim(), type: (type ?? 'string').trim() };
                });
                set('castings', castings);
              }} placeholder={"amount:number\norder_date:date"} />
            </div>
          )}

          {/* Sort */}
          {node.type === 'sort' && (
            <>
              <div className="pc-config-row">
                <label className="pc-config-label">Sort by column</label>
                <input className="pc-config-input" value={cfg.sortBy ?? ''} onChange={(e) => set('sortBy', e.target.value)} placeholder="e.g. order_date" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Direction</label>
                <select className="pc-config-select" value={cfg.direction ?? 'asc'} onChange={(e) => set('direction', e.target.value)}>
                  <option value="asc">Ascending</option>
                  <option value="desc">Descending</option>
                </select>
              </div>
            </>
          )}

          {/* Deduplicate */}
          {node.type === 'deduplicate' && (
            <>
              <div className="pc-config-row">
                <label className="pc-config-label">Key columns (comma-separated)</label>
                <input className="pc-config-input" value={cfg.keyColumns ?? ''} onChange={(e) => set('keyColumns', e.target.value)} placeholder="customer_id, order_id" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Keep</label>
                <select className="pc-config-select" value={cfg.keepFirst ? 'first' : 'last'} onChange={(e) => set('keepFirst', e.target.value === 'first')}>
                  <option value="first">First occurrence</option>
                  <option value="last">Last occurrence</option>
                </select>
              </div>
            </>
          )}

          {/* Join */}
          {node.type === 'join' && (
            <>
              <div className="pc-config-row">
                <label className="pc-config-label">Join type</label>
                <select className="pc-config-select" value={cfg.joinType ?? 'inner'} onChange={(e) => set('joinType', e.target.value)}>
                  {['inner', 'left', 'right', 'full', 'cross'].map((t) => <option key={t}>{t}</option>)}
                </select>
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Right dataset</label>
                <input className="pc-config-input" value={cfg.rightDataset ?? ''} onChange={(e) => set('rightDataset', e.target.value)} placeholder="e.g. CUSTOMER_DIM" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Left key</label>
                <input className="pc-config-input" value={cfg.leftKey ?? ''} onChange={(e) => set('leftKey', e.target.value)} placeholder="e.g. customer_id" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Right key</label>
                <input className="pc-config-input" value={cfg.rightKey ?? ''} onChange={(e) => set('rightKey', e.target.value)} placeholder="e.g. id" />
              </div>
            </>
          )}

          {/* Lookup */}
          {node.type === 'lookup' && (
            <>
              <div className="pc-config-row">
                <label className="pc-config-label">Lookup dataset</label>
                <input className="pc-config-input" value={cfg.lookupDataset ?? ''} onChange={(e) => set('lookupDataset', e.target.value)} />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Lookup key</label>
                <input className="pc-config-input" value={cfg.lookupKey ?? ''} onChange={(e) => set('lookupKey', e.target.value)} />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Return columns (comma-separated)</label>
                <input className="pc-config-input" value={cfg.returnColumns ?? ''} onChange={(e) => set('returnColumns', e.target.value)} />
              </div>
            </>
          )}

          {/* Aggregate */}
          {node.type === 'aggregate' && (
            <>
              <div className="pc-config-row">
                <label className="pc-config-label">Group by (comma-separated)</label>
                <input className="pc-config-input" value={cfg.groupBy ?? ''} onChange={(e) => set('groupBy', e.target.value)} placeholder="region, year" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Aggregations (col:func, one per line)</label>
                <textarea className="pc-config-textarea" style={{ fontFamily: 'var(--font-mono)' }} value={Array.isArray(cfg.aggregations) ? cfg.aggregations.map((a) => `${a.column}:${a.func}`).join('\n') : ''} onChange={(e) => {
                  const aggs = e.target.value.split('\n').filter(Boolean).map((line) => {
                    const [column, func] = line.split(':');
                    return { column: (column ?? '').trim(), func: (func ?? 'sum').trim() };
                  });
                  set('aggregations', aggs);
                }} placeholder={"amount:sum\norder_id:count"} />
              </div>
            </>
          )}

          {/* Calculated Column */}
          {node.type === 'calculated' && (
            <>
              <div className="pc-config-row">
                <label className="pc-config-label">New column name</label>
                <input className="pc-config-input" value={cfg.columnName ?? ''} onChange={(e) => set('columnName', e.target.value)} placeholder="e.g. total_with_tax" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Expression</label>
                <textarea className="pc-config-textarea" style={{ fontFamily: 'var(--font-mono)' }} value={cfg.expression ?? ''} onChange={(e) => set('expression', e.target.value)} placeholder="e.g. price * quantity * (1 + tax_rate)" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Output type</label>
                <select className="pc-config-select" value={cfg.type ?? 'number'} onChange={(e) => set('type', e.target.value)}>
                  {['string', 'number', 'date', 'boolean'].map((t) => <option key={t}>{t}</option>)}
                </select>
              </div>
            </>
          )}

          {/* Conditional Logic */}
          {node.type === 'conditional' && (
            <div className="pc-config-row">
              <label className="pc-config-label">CASE-WHEN rules (one per line: condition → value)</label>
              <textarea className="pc-config-textarea" style={{ fontFamily: 'var(--font-mono)', minHeight: 100 }} value={Array.isArray(cfg.conditions) ? cfg.conditions.map((c) => `${c.when} → ${c.then}`).join('\n') : ''} onChange={(e) => {
                const conditions = e.target.value.split('\n').filter(Boolean).map((line) => {
                  const [when, , then] = line.split(' ');
                  return { when: (when ?? '').trim(), then: (then ?? '').trim() };
                });
                set('conditions', conditions);
              }} placeholder={"status = 'active' → 1\nstatus = 'inactive' → 0"} />
            </div>
          )}

          {/* Column Mapping */}
          {node.type === 'mapping' && (
            <div className="pc-config-row">
              <label className="pc-config-label">Mappings (source → target, one per line)</label>
              <textarea className="pc-config-textarea" style={{ fontFamily: 'var(--font-mono)' }} value={Array.isArray(cfg.mappings) ? cfg.mappings.map((m) => `${m.source} → ${m.target}`).join('\n') : ''} onChange={(e) => {
                const mappings = e.target.value.split('\n').filter(Boolean).map((line) => {
                  const [source, , target] = line.split(' ');
                  return { source: (source ?? '').trim(), target: (target ?? '').trim() };
                });
                set('mappings', mappings);
              }} placeholder={"invoice_number → INV_NO\nvendor_name → VENDOR"} />
            </div>
          )}

          {/* Validation — real, generic row-completeness check */}
          {node.type === 'validation_step' && (
            <>
              <ConfigNotice>
                Runs a real, generic row-completeness check (% of rows with zero null values) against the threshold below when the pipeline job runs — this node has no inline rule editor, so it does not execute a saved Validation rule set's actual rules. For real per-column rules, use the Validation module instead.
              </ConfigNotice>
              <div className="pc-config-row">
                <label className="pc-config-label">Ruleset name (label only)</label>
                <input className="pc-config-input" value={cfg.rulesetName ?? ''} onChange={(e) => set('rulesetName', e.target.value)} placeholder="e.g. Orders completeness check" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Pass threshold (% complete rows)</label>
                <input className="pc-config-input" type="number" min={0} max={100} value={cfg.passThreshold ?? 95} onChange={(e) => set('passThreshold', Number(e.target.value))} />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">On failure</label>
                <select className="pc-config-select" value={cfg.failAction ?? 'warn'} onChange={(e) => set('failAction', e.target.value)}>
                  <option value="warn">Warn — continue pipeline</option>
                  <option value="error">Error — stop pipeline</option>
                </select>
              </div>
            </>
          )}

          {/* Reconciliation — real row-count comparison */}
          {node.type === 'reconciliation_step' && (
            <>
              <ConfigNotice>
                Compares this pipeline's row count against the table/object named below (read from the pipeline's primary Source Dataset connection) — a real, generic count check when the pipeline job runs. Key Column is accepted here but not yet used for per-key diffs.
              </ConfigNotice>
              <div className="pc-config-row">
                <label className="pc-config-label">Compare with (table/object name)</label>
                <input className="pc-config-input" value={cfg.compareWith ?? ''} onChange={(e) => set('compareWith', e.target.value)} placeholder="e.g. orders_dim" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Key column <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>(not yet used)</span></label>
                <input className="pc-config-input" value={cfg.keyColumn ?? ''} onChange={(e) => set('keyColumn', e.target.value)} placeholder="e.g. id" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Tolerance</label>
                <input className="pc-config-input" value={cfg.tolerance ?? '0.01'} onChange={(e) => set('tolerance', e.target.value)} placeholder="0.01 = 1% of target row count, or an integer row count" />
              </div>
            </>
          )}

          {/* Anomaly Detection — real, job-referencing or inline (AD10) */}
          {node.type === 'anomaly_detection_step' && (
            <AnomalyDetectionConfig cfg={cfg} set={set} />
          )}

          {/* Column Mapping — real, references a saved PUBLISHED
              ColumnMapping (id, version); resolved and applied for real
              at execution time (execution/mapping_execution.py). Not the
              unrelated inline `mapping` rename node above. */}
          {node.type === 'column_mapping_step' && (
            <ColumnMappingConfig cfg={cfg} set={set} pipelineId={pipelineId} nodeId={node.id} nodes={nodes} edges={edges} />
          )}

          {/* Notification — only Webhook actually sends */}
          {node.type === 'notification' && (
            <>
              <ConfigNotice>
                Only the Webhook channel actually sends (a real HTTP POST) when the pipeline job runs — Email/Slack/SMS are accepted here but logged as not wired to a real transport. The On Success/On Failure checkboxes aren't enforced by the run engine yet: a Notification node fires whenever the pipeline reaches it in the graph, not conditionally on the job's final outcome.
              </ConfigNotice>
              <div className="pc-config-row">
                <label className="pc-config-label">Channel</label>
                <select className="pc-config-select" value={cfg.channel ?? 'email'} onChange={(e) => set('channel', e.target.value)}>
                  <option value="webhook">Webhook (real)</option>
                  <option value="email">Email (not wired)</option>
                  <option value="slack">Slack (not wired)</option>
                  <option value="sms">SMS (not wired)</option>
                </select>
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">{cfg.channel === 'webhook' ? 'Webhook URL' : 'Recipients'}</label>
                <input className="pc-config-input" value={cfg.recipients ?? ''} onChange={(e) => set('recipients', e.target.value)} placeholder={cfg.channel === 'webhook' ? 'https://example.com/hooks/pipeline' : 'user@example.com, #channel'} />
              </div>
              <label className="pc-config-checkbox-row">
                <input type="checkbox" checked={cfg.onSuccess ?? true} onChange={(e) => set('onSuccess', e.target.checked)} /> Notify on success
              </label>
              <label className="pc-config-checkbox-row">
                <input type="checkbox" checked={cfg.onFailure ?? true} onChange={(e) => set('onFailure', e.target.checked)} /> Notify on failure
              </label>
            </>
          )}

          {/* Document Input — DI group; see module-level notice below */}
          {node.type === 'doc_input' && (
            <>
              <ConfigNotice>
                Document Intelligence nodes are documentation/planning placeholders in Transform Workspace — this node passes its input through unchanged when a pipeline job runs (see the job log). Real OCR + AI extraction happens in the separate Document Intelligence wizard; use its "Continue to Transformation" action to feed extracted data into a real Source Dataset node instead.
              </ConfigNotice>
              <div className="pc-config-row">
                <label className="pc-config-label">Source type</label>
                <select className="pc-config-select" value={cfg.sourceType ?? 'upload'} onChange={(e) => set('sourceType', e.target.value)}>
                  <option value="upload">Manual upload</option>
                  <option value="path">Watched folder path</option>
                </select>
              </div>
              {cfg.sourceType === 'path' && (
                <div className="pc-config-row">
                  <label className="pc-config-label">Path</label>
                  <input className="pc-config-input" value={cfg.path ?? ''} onChange={(e) => set('path', e.target.value)} placeholder="/incoming/invoices" />
                </div>
              )}
              <div className="pc-config-row">
                <label className="pc-config-label">Accepted file types (comma-separated)</label>
                <input className="pc-config-input" value={(cfg.fileTypes ?? []).join(', ')} onChange={(e) => set('fileTypes', e.target.value.split(',').map((s) => s.trim()).filter(Boolean))} placeholder="pdf, jpg, png, tiff" />
              </div>
            </>
          )}

          {/* Image Scanner */}
          {node.type === 'image_scanner' && (
            <>
              <ConfigNotice>
                Documentation placeholder — passes data through unchanged when a pipeline job runs. Real image pre-processing happens inside the Document Intelligence wizard's OCR stage.
              </ConfigNotice>
              <label className="pc-config-checkbox-row"><input type="checkbox" checked={cfg.deskew ?? true} onChange={(e) => set('deskew', e.target.checked)} /> Deskew</label>
              <label className="pc-config-checkbox-row"><input type="checkbox" checked={cfg.denoise ?? true} onChange={(e) => set('denoise', e.target.checked)} /> Denoise</label>
              <label className="pc-config-checkbox-row"><input type="checkbox" checked={cfg.enhance ?? false} onChange={(e) => set('enhance', e.target.checked)} /> Enhance contrast</label>
              <label className="pc-config-checkbox-row"><input type="checkbox" checked={cfg.detectBlank ?? true} onChange={(e) => set('detectBlank', e.target.checked)} /> Detect blank pages</label>
            </>
          )}

          {/* OCR Engine */}
          {node.type === 'ocr_engine' && (
            <>
              <ConfigNotice>
                Documentation placeholder — passes data through unchanged here. The real OCR engine (Tesseract, or a cloud OCR provider set in Settings) runs inside the Document Intelligence wizard.
              </ConfigNotice>
              <div className="pc-config-row">
                <label className="pc-config-label">Engine</label>
                <input className="pc-config-input" value={cfg.engine ?? 'dip-ocr-v2'} onChange={(e) => set('engine', e.target.value)} />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Language</label>
                <input className="pc-config-input" value={cfg.language ?? 'en'} onChange={(e) => set('language', e.target.value)} placeholder="en" />
              </div>
              <label className="pc-config-checkbox-row"><input type="checkbox" checked={cfg.detectTables ?? true} onChange={(e) => set('detectTables', e.target.checked)} /> Detect tables</label>
              <label className="pc-config-checkbox-row"><input type="checkbox" checked={cfg.handwriting ?? false} onChange={(e) => set('handwriting', e.target.checked)} /> Handwriting recognition</label>
            </>
          )}

          {/* AI Extraction */}
          {node.type === 'ai_extraction' && (
            <>
              <ConfigNotice>
                Documentation placeholder — passes data through unchanged here. Real AI field extraction (against a saved AI Agent connection) runs inside the Document Intelligence wizard's AI Extraction stage.
              </ConfigNotice>
              <div className="pc-config-row">
                <label className="pc-config-label">Model</label>
                <input className="pc-config-input" value={cfg.model ?? 'gpt-4o'} onChange={(e) => set('model', e.target.value)} />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Schema mode</label>
                <select className="pc-config-select" value={cfg.schemaMode ?? 'auto'} onChange={(e) => set('schemaMode', e.target.value)}>
                  <option value="auto">Auto-detect</option>
                  <option value="fixed">Fixed schema</option>
                </select>
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Confidence threshold (%)</label>
                <input className="pc-config-input" type="number" min={0} max={100} value={cfg.confidenceThreshold ?? 85} onChange={(e) => set('confidenceThreshold', Number(e.target.value))} />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Route to review below (%)</label>
                <input className="pc-config-input" type="number" min={0} max={100} value={cfg.reviewBelow ?? 70} onChange={(e) => set('reviewBelow', Number(e.target.value))} />
              </div>
            </>
          )}

          {/* Structured Dataset */}
          {node.type === 'structured_dataset' && (
            <>
              <ConfigNotice>
                Documentation placeholder — passes data through unchanged here. To land AI-extracted document data into a database table for real, use the Document Intelligence wizard's "Continue to Transformation" action (creates a real Source Dataset node) and add an Output Dataset node as the target.
              </ConfigNotice>
              <div className="pc-config-row">
                <label className="pc-config-label">Dataset name</label>
                <input className="pc-config-input" value={cfg.datasetName ?? ''} onChange={(e) => set('datasetName', e.target.value)} placeholder="e.g. extracted_invoices" />
              </div>
              <div className="pc-config-row">
                <label className="pc-config-label">Publish mode</label>
                <select className="pc-config-select" value={cfg.publishMode ?? 'overwrite'} onChange={(e) => set('publishMode', e.target.value)}>
                  <option value="overwrite">Overwrite</option>
                  <option value="append">Append</option>
                </select>
              </div>
            </>
          )}

          {/* Invoice Intake — real SFTP/FTP/Network File discovery */}
          {node.type === 'invoice_intake' && (
            <InvoiceIntakeConfig cfg={cfg} set={set} />
          )}

          {/* Invoice Processing — no config, real OCR+extraction per document */}
          {node.type === 'invoice_processing' && (
            <ConfigNotice>
              No configuration — runs the real OCR + AI/Custom extraction chain (document_service.process_document()) on every document flowing in from Invoice Intake. Materialize/Validate/Decide happen downstream in Invoice Validation, not here.
            </ConfigNotice>
          )}

          {/* Invoice Validation — no config, real materialize+validate+decide */}
          {node.type === 'invoice_validation' && (
            <ConfigNotice>
              No configuration — runs the real materialize_invoice() → evaluate_invoice() → decide() chain per document, using this organization's current published validation rules (manage them in Invoice Intelligence → Settings → Rules) and the real Anomaly Detection check as part of decide()'s own decision matrix.
            </ConfigNotice>
          )}

          {/* Review Gate — pure sync filter on the real decision outcome */}
          {node.type === 'invoice_review_gate' && (
            <>
              <ConfigNotice>
                A real filter — only rows whose decide() outcome (set by the upstream Invoice Validation node) is checked below continue past this node.
              </ConfigNotice>
              {['STRAIGHT_THROUGH', 'REVIEW_REQUIRED', 'REJECT', 'RETRY'].map((outcome) => {
                const passOn = Array.isArray(cfg.passOn) ? cfg.passOn : ['STRAIGHT_THROUGH'];
                return (
                  <label className="pc-config-checkbox-row" key={outcome}>
                    <input
                      type="checkbox"
                      checked={passOn.includes(outcome)}
                      onChange={(e) => set('passOn', e.target.checked ? [...passOn, outcome] : passOn.filter((o) => o !== outcome))}
                    /> {outcome.replaceAll('_', ' ')}
                  </label>
                );
              })}
            </>
          )}

          {/* ERP Posting — real idempotent post + settle-poll */}
          {node.type === 'invoice_erp_posting' && (
            <InvoiceErpPostingConfig cfg={cfg} set={set} />
          )}

          {/* Reconciliation — no config, real re-verification */}
          {node.type === 'invoice_reconciliation' && (
            <ConfigNotice>
              No configuration — runs the real invoice_reconciliation_service.reconcile_invoice() field-by-field platform-vs-ERP comparison for every invoice that reached this node.
            </ConfigNotice>
          )}
        </div>
      </div>

      {/* Apply / Cancel — nothing reaches the canvas node until Apply */}
      <div className="pc-config-footer">
        <Button variant="secondary" size="sm" onClick={handleCancel} disabled={!isDirty}>Cancel</Button>
        <Button variant="primary" size="sm" onClick={handleApply} disabled={!isDirty}>Apply</Button>
      </div>
    </div>
  );
}

// ── Main designer ──────────────────────────────────────────────

export function TransformationDesigner({ initialPipeline = null, onSaved = null }) {
  const [pipeline, dispatch] = useReducer(
    pipelineReducer,
    initialPipeline ?? INIT_PIPELINE
  );

  const [selectedId, setSelectedId] = useState(null);
  const [zoom, setZoom]             = useState(1);

  // ── Backend persistence / versioning ───────────────────────
  const [pipelineId, setPipelineId]         = useState(initialPipeline?.id ?? null);
  const [currentVersion, setCurrentVersion] = useState(initialPipeline?.current_version ?? null);
  const [savedSnapshot, setSavedSnapshot]   = useState(() => snapshotOf(initialPipeline ?? INIT_PIPELINE));
  const [saving, setSaving]                 = useState(false);
  const [saveError, setSaveError]           = useState('');
  const [showHistory, setShowHistory]       = useState(false);

  const dirty = useMemo(() => snapshotOf(pipeline) !== savedSnapshot, [pipeline, savedSnapshot]);

  // Undo stack (simple one-level for now)
  const [undoStack, setUndoStack] = useState([]);

  const pushUndo = useCallback(() =>
    setUndoStack((s) => [...s.slice(-20), { nodes: pipeline.nodes, edges: pipeline.edges }]),
  [pipeline]);

  // ── Handlers ────────────────────────────────────────────────
  const handleAddNode = useCallback((type, position) => {
    pushUndo();
    dispatch({ type: 'ADD_NODE', nodeType: type, position });
  }, [pushUndo]);

  const handleMoveNode = useCallback((id, position) => {
    dispatch({ type: 'MOVE_NODE', id, position });
  }, []);

  const handleDeleteNode = useCallback((id) => {
    pushUndo();
    dispatch({ type: 'DELETE_NODE', id });
    if (selectedId === id) setSelectedId(null);
  }, [pushUndo, selectedId]);

  const handleAddEdge = useCallback((sourceId, targetId) => {
    pushUndo();
    dispatch({ type: 'ADD_EDGE', sourceId, targetId });
  }, [pushUndo]);

  const handleDeleteEdge = useCallback((id) => {
    pushUndo();
    dispatch({ type: 'DELETE_EDGE', id });
    if (selectedId === id) setSelectedId(null);
  }, [pushUndo, selectedId]);

  /** Fired only on Apply — commits the properties draft to the canvas node. */
  const handleApplyConfig = useCallback((nodeId, label, config) => {
    pushUndo();
    dispatch({ type: 'UPDATE_NODE_CONFIG', id: nodeId, patch: config, label });
  }, [pushUndo]);

  const handleUndo = useCallback(() => {
    if (!undoStack.length) return;
    const prev = undoStack[undoStack.length - 1];
    setUndoStack((s) => s.slice(0, -1));
    dispatch({ type: 'LOAD', nodes: prev.nodes, edges: prev.edges });
  }, [undoStack]);

  /** Save ALWAYS creates a new version server-side — never overwrites in place. */
  const handleSave = useCallback(async () => {
    setSaving(true);
    setSaveError('');
    try {
      const payload = { name: pipeline.name, nodes: structural(pipeline.nodes), edges: pipeline.edges, settings: {} };
      const wasUnsaved = !pipelineId;
      const result = pipelineId
        ? await pipelineVersionApi.save(pipelineId, payload)
        : await pipelineVersionApi.create(payload);
      setPipelineId(result.id);
      setCurrentVersion(result.current_version);
      setSavedSnapshot(snapshotOf(pipeline));

      // A brand-new pipeline only gets a real id right here — any file
      // uploaded into a Source Dataset node's "Upload File" option before
      // now was necessarily unlinked (PipelineSourceFile.pipeline_id was
      // null). Link it now so the upload is uniquely tied to this pipeline,
      // never left "floating" or shared across pipelines.
      if (wasUnsaved) {
        const uploadedFileIds = pipeline.nodes
          .filter((n) => n.type === 'source' && n.config?.uploadedFileId)
          .map((n) => n.config.uploadedFileId);
        await Promise.all(uploadedFileIds.map((fileId) => pipelineFileApi.attach(fileId, result.id).catch(() => {})));
      }

      // Let the parent page (Transformation.jsx) know a save happened so
      // its "Open Pipeline" picker list stays in sync — this is what makes
      // a saved pipeline actually show up somewhere after Save.
      onSaved?.({ id: result.id, name: pipeline.name, current_version: result.current_version });
    } catch (err) {
      setSaveError(err?.response?.data?.detail ?? 'Could not save pipeline.');
    } finally {
      setSaving(false);
    }
  }, [pipeline, pipelineId, onSaved]);

  const handleRestored = useCallback((updated) => {
    dispatch({ type: 'LOAD', nodes: updated.nodes, edges: updated.edges, name: updated.name });
    setCurrentVersion(updated.current_version);
    setSavedSnapshot(snapshotOf({ name: updated.name, nodes: updated.nodes, edges: updated.edges }));
    setSelectedId(null);
    setShowHistory(false);
  }, []);

  const selectedNode = pipeline.nodes.find((n) => n.id === selectedId) ?? null;

  return (
    <div className="pc-designer-layout" style={{ position: 'relative' }}>
      {/* ── Left: node palette ── */}
      <NodePalette />

      {/* ── Centre: toolbar + canvas ── */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>
        <PipelineToolbar
          canUndo={undoStack.length > 0}
          canRedo={false}
          onUndo={handleUndo}
          onSave={handleSave}
          onFitView={() => setZoom(1)}
          onZoomIn={() => setZoom((z) => Math.min(2, parseFloat((z + 0.15).toFixed(2))))}
          onZoomOut={() => setZoom((z) => Math.max(0.25, parseFloat((z - 0.15).toFixed(2))))}
          zoom={zoom}
          pipelineName={pipeline.name}
          versionLabel={currentVersion ? `v${currentVersion}` : null}
          onNameChange={(name) => dispatch({ type: 'SET_NAME', name })}
          onClearCanvas={() => { pushUndo(); dispatch({ type: 'CLEAR' }); setSelectedId(null); }}
          dirty={dirty}
          onHistory={() => setShowHistory(true)}
        />
        {saving && <div className="pc-save-status">Saving…</div>}
        {saveError && <div className="pc-save-status error">{saveError}</div>}
        <PipelineCanvas
          nodes={pipeline.nodes}
          edges={pipeline.edges}
          selectedId={selectedId}
          zoom={zoom}
          onSelectNode={setSelectedId}
          onSelectEdge={setSelectedId}
          onMoveNode={handleMoveNode}
          onAddNode={handleAddNode}
          onAddEdge={handleAddEdge}
          onDeleteNode={handleDeleteNode}
          onDeleteEdge={handleDeleteEdge}
          onDeselectAll={() => setSelectedId(null)}
        />
      </div>

      {/* ── Right: config panel ── */}
      <NodeConfigPanel
        node={selectedNode}
        onApply={handleApplyConfig}
        onClose={() => setSelectedId(null)}
        pipelineId={pipelineId}
        pipelineDirty={dirty}
        nodes={pipeline.nodes}
        edges={pipeline.edges}
      />

      {/* ── Version History drawer ── */}
      {showHistory && (
        <VersionHistoryPanel
          pipelineId={pipelineId}
          currentVersion={currentVersion}
          onClose={() => setShowHistory(false)}
          onRestored={handleRestored}
        />
      )}
    </div>
  );
}
