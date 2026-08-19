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
import { X } from 'lucide-react';
import { PipelineToolbar } from '@/components/pipeline/PipelineToolbar.jsx';
import { PipelineCanvas }  from '@/components/pipeline/PipelineCanvas.jsx';
import { PALETTE_GROUPS, NODE_TYPES, createNode, createEdge } from '@/components/pipeline/nodeTypes.js';
import { SourceDatasetConfig } from '@/components/pipeline/SourceDatasetConfig.jsx';
import { TargetDatasetConfig } from '@/components/pipeline/TargetDatasetConfig.jsx';
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

// ── Node configuration panel — local draft, committed only on Apply ────

function NodeConfigPanel({ node, onApply, onClose, pipelineId, pipelineDirty }) {
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
            <TargetDatasetConfig cfg={cfg} set={set} />
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
