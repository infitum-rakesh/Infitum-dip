/**
 * PipelineNode — a single draggable node on the canvas.
 *
 * Props:
 *   node          : { id, type, label, position, config, status, metrics }
 *   selected      : boolean
 *   onSelect      : (id) => void
 *   onDelete      : (id) => void
 *   onDragStart   : (e, id) => void
 *   onDragEnd     : (e, id) => void
 *   onPortMouseDown: (e, nodeId, portType) => void  — starts edge drawing
 */
import { NODE_TYPES } from './nodeTypes.js';
import './pipeline.css';

const STATUS_ICONS = {
  running: '⏳',
  success: '✓',
  error:   '✕',
  warning: '⚠',
};

export function PipelineNode({
  node,
  selected = false,
  onSelect,
  onDelete,
  onDragStart,
  onDragEnd,
  onPortMouseDown,
}) {
  const def     = NODE_TYPES[node.type] ?? {};
  const hasBody = node.metrics || node.config?.condition || node.config?.expression || node.config?.groupBy || node.config?.sortBy;

  // Selection must be driven by a genuine click, never by mousedown/mouseup —
  // mousedown only arms the drag; the node's own onClick (below) does the
  // selecting and stops the click from bubbling to the canvas' deselect-all.
  // This is what makes properties open on click and STAY open, instead of
  // flickering open-on-press / closed-on-release.
  const handleMouseDown = (e) => {
    if (e.target.closest('.pc-port') || e.target.closest('.pc-node-del')) return;
    e.stopPropagation();
    onDragStart?.(e, node.id);
  };

  const handleClick = (e) => {
    if (e.target.closest('.pc-port') || e.target.closest('.pc-node-del')) return;
    e.stopPropagation();
    onSelect?.(node.id);
  };

  return (
    <div
      className={[
        'pc-node',
        selected ? 'selected' : '',
        node.status !== 'idle' ? `status-${node.status}` : '',
      ].filter(Boolean).join(' ')}
      style={{ left: node.position.x, top: node.position.y }}
      onMouseDown={handleMouseDown}
      onClick={handleClick}
      onDragEnd={(e) => onDragEnd?.(e, node.id)}
      data-node-id={node.id}
    >
      {/* Status bar */}
      {node.status !== 'idle' && (
        <div className={`pc-node-status-bar ${node.status}`} />
      )}

      {/* Input port */}
      {def.hasInput !== false && (
        <div
          className={`pc-port input-port ${node._connectedInput ? 'connected' : ''}`}
          title="Connect input"
          onMouseDown={(e) => { e.stopPropagation(); onPortMouseDown?.(e, node.id, 'input'); }}
        />
      )}

      {/* Header */}
      <div className="pc-node-header">
        <div className="pc-node-icon" style={{ background: def.bg ?? '#F1F5F9', color: def.color ?? '#64748B' }}>
          {def.icon ?? '⚙'}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="pc-node-title">{node.label || def.label}</div>
          <div className="pc-node-type-label">{def.group}</div>
        </div>
        {node.status !== 'idle' && (
          <span style={{ fontSize: 13, flexShrink: 0 }}>{STATUS_ICONS[node.status] ?? ''}</span>
        )}
      </div>

      {/* Body — shows key config or metrics */}
      {hasBody && (
        <div className="pc-node-body">
          {node.metrics && (
            <>
              {node.metrics.rowsIn  != null && <div className="pc-node-metric"><span className="pc-node-metric-key">Rows in</span><span className="pc-node-metric-val">{node.metrics.rowsIn.toLocaleString()}</span></div>}
              {node.metrics.rowsOut != null && <div className="pc-node-metric"><span className="pc-node-metric-key">Rows out</span><span className="pc-node-metric-val">{node.metrics.rowsOut.toLocaleString()}</span></div>}
              {node.metrics.duration && <div className="pc-node-metric"><span className="pc-node-metric-key">Duration</span><span className="pc-node-metric-val">{node.metrics.duration}</span></div>}
            </>
          )}
          {!node.metrics && node.config?.condition && (
            <div className="pc-node-metric"><span className="pc-node-metric-key" style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>{String(node.config.condition).slice(0, 28)}{node.config.condition.length > 28 ? '…' : ''}</span></div>
          )}
          {!node.metrics && node.config?.expression && (
            <div className="pc-node-metric"><span className="pc-node-metric-key" style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>{String(node.config.expression).slice(0, 28)}{node.config.expression.length > 28 ? '…' : ''}</span></div>
          )}
          {!node.metrics && node.config?.sortBy && (
            <div className="pc-node-metric"><span className="pc-node-metric-key">Sort by</span><span className="pc-node-metric-val">{node.config.sortBy} {node.config.direction ?? 'asc'}</span></div>
          )}
          {!node.metrics && node.config?.groupBy && (
            <div className="pc-node-metric"><span className="pc-node-metric-key">Group by</span><span className="pc-node-metric-val">{String(node.config.groupBy).slice(0, 20)}</span></div>
          )}
        </div>
      )}

      {/* Output port */}
      {def.hasOutput !== false && (
        <div
          className={`pc-port output-port ${node._connectedOutput ? 'connected' : ''}`}
          title="Connect output"
          onMouseDown={(e) => { e.stopPropagation(); onPortMouseDown?.(e, node.id, 'output'); }}
        />
      )}

      {/* Delete button — visible on select or hover (CSS) */}
      <div
        className="pc-node-del"
        onClick={(e) => { e.stopPropagation(); onDelete?.(node.id); }}
        title="Delete node"
      >
        ✕
      </div>
    </div>
  );
}
