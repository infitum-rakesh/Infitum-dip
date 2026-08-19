/**
 * PipelineEdge — renders one SVG curved edge between two nodes.
 *
 * Props:
 *   edge        : { id, sourceId, targetId }
 *   sourcePos   : { x, y }   — centre of source node's output port
 *   targetPos   : { x, y }   — centre of target node's input port
 *   selected    : boolean
 *   animated    : boolean    — dashed flow animation
 *   status      : 'idle'|'success'|'error'|'running'
 *   onSelect    : (edgeId) => void
 *   onDelete    : (edgeId) => void
 *   onContextMenu: (edgeId, clientX, clientY) => void
 */

/** Cubic bezier path — horizontal tangents look clean for LR layouts */
function cubicPath(sx, sy, tx, ty) {
  const cp = Math.max(Math.abs(tx - sx) * 0.5, 60);
  return `M${sx},${sy} C${sx + cp},${sy} ${tx - cp},${ty} ${tx},${ty}`;
}

const MARKER_BY_STATE = {
  selected: 'url(#pc-arrow-selected)',
  success:  'url(#pc-arrow-success)',
  error:    'url(#pc-arrow-error)',
  idle:     'url(#pc-arrow-idle)',
};

export function PipelineEdge({
  edge,
  sourcePos,
  targetPos,
  selected = false,
  animated = false,
  status = 'idle',
  onSelect,
  onDelete,
  onContextMenu,
}) {
  if (!sourcePos || !targetPos) return null;

  const { x: sx, y: sy } = sourcePos;
  const { x: tx, y: ty } = targetPos;
  const d = cubicPath(sx, sy, tx, ty);

  // Midpoint for the delete handle
  const mx = (sx + tx) / 2;
  const my = (sy + ty) / 2;

  const pathClass = [
    'pc-edge-path',
    selected  ? 'selected'  : '',
    animated  ? 'animated'  : '',
    status !== 'idle' ? status : '',
  ].filter(Boolean).join(' ');

  const markerKey = selected ? 'selected' : (status !== 'idle' ? status : 'idle');

  return (
    <g
      data-edge-id={edge.id}
      className={`pc-edge-group ${selected ? 'selected' : ''}`}
      onClick={(e) => { e.stopPropagation(); onSelect?.(edge.id); }}
      onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); onSelect?.(edge.id); onContextMenu?.(edge.id, e.clientX, e.clientY); }}
      style={{ cursor: 'pointer' }}
    >
      {/* Wider transparent hit area — makes thin edges easy to click/right-click */}
      <path d={d} fill="none" stroke="transparent" strokeWidth={14} />

      {/* Soft highlight halo behind the selected edge */}
      {selected && <path d={d} className="pc-edge-halo" />}

      {/* Visible path */}
      <path d={d} className={pathClass} markerEnd={MARKER_BY_STATE[markerKey]} />

      {/* Delete button — only when selected */}
      {selected && (
        <g transform={`translate(${mx},${my})`} className="pc-edge-del-btn">
          <circle r={9} fill="var(--color-danger)" stroke="white" strokeWidth={2} />
          <text
            x="0" y="4"
            textAnchor="middle"
            fontSize={11}
            fill="white"
            fontWeight="700"
            style={{ cursor: 'pointer' }}
            onClick={(e) => { e.stopPropagation(); onDelete?.(edge.id); }}
          >
            ✕
          </text>
        </g>
      )}
    </g>
  );
}

/**
 * GhostEdge — drawn while the user is actively dragging from a port.
 * Turns red + shows a "not allowed" glyph when hovering an invalid target.
 */
export function GhostEdge({ sourcePos, mousePos, invalid = false }) {
  if (!sourcePos || !mousePos) return null;
  const d = cubicPath(sourcePos.x, sourcePos.y, mousePos.x, mousePos.y);
  return (
    <g>
      <path d={d} className={`pc-edge-ghost ${invalid ? 'invalid' : ''}`} />
      {invalid && (
        <g transform={`translate(${mousePos.x},${mousePos.y})`}>
          <circle r={9} fill="var(--color-danger)" opacity={0.95} />
          <line x1={-4} y1={-4} x2={4} y2={4} stroke="white" strokeWidth={1.6} strokeLinecap="round" />
          <line x1={-4} y1={4} x2={4} y2={-4} stroke="white" strokeWidth={1.6} strokeLinecap="round" />
        </g>
      )}
    </g>
  );
}
