/**
 * PipelineCanvas — the interactive drag-and-drop canvas.
 *
 * Exposes a controlled interface: the parent (TransformationDesigner)
 * owns nodes + edges state and passes it down.
 *
 * Interaction model:
 *   - Drag a palette item onto the canvas  → add node (via onAddNode)
 *   - Click a node                         → select it (via onSelectNode)
 *   - Drag a node                          → move it (via onMoveNode)
 *   - Drag from output port → input port   → add edge (via onAddEdge)
 *   - Click an edge                        → select it
 *   - Right-click an edge                  → context menu → Delete Connection
 *   - Delete / Backspace                   → remove selected node or edge
 *   - Click canvas background              → deselect all
 *   - Drag canvas background               → pan
 *   - Mouse wheel / shift+wheel / trackpad  → scroll canvas (native)
 *   - Zoom in/out (toolbar)                → scales the canvas content
 */
import { useRef, useState, useCallback, useEffect, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { PipelineNode }         from './PipelineNode.jsx';
import { PipelineEdge, GhostEdge } from './PipelineEdge.jsx';
import { NODE_TYPES }           from './nodeTypes.js';
import './pipeline.css';

const NODE_WIDTH  = 200;
const NODE_HEIGHT = 90;

// Logical canvas size (unscaled) — generous so there's always room to pan
// and drop new nodes. Grows to fit the current pipeline's extent.
const MIN_CANVAS_W = 2400;
const MIN_CANVAS_H = 1200;

/** Compute the absolute pixel position of a node's output or input port */
function portPos(node, portType) {
  const x = portType === 'output'
    ? node.position.x + NODE_WIDTH
    : node.position.x;
  return { x, y: node.position.y + NODE_HEIGHT / 2 };
}

/** Would sourceId → targetId create a cycle, given existing edges? */
function createsCycle(edges, sourceId, targetId) {
  // If targetId can already reach sourceId, adding sourceId->targetId closes a cycle.
  const adjacency = new Map();
  edges.forEach((e) => {
    if (!adjacency.has(e.sourceId)) adjacency.set(e.sourceId, []);
    adjacency.get(e.sourceId).push(e.targetId);
  });
  const stack = [targetId];
  const seen = new Set();
  while (stack.length) {
    const cur = stack.pop();
    if (cur === sourceId) return true;
    if (seen.has(cur)) continue;
    seen.add(cur);
    (adjacency.get(cur) ?? []).forEach((n) => stack.push(n));
  }
  return false;
}

/** Full validity check for a proposed sourceId -> targetId connection */
function connectionValidity(nodes, edges, sourceId, targetId) {
  if (!sourceId || !targetId) return { valid: false, reason: '' };
  if (sourceId === targetId) return { valid: false, reason: 'A node cannot connect to itself' };

  const srcNode = nodes.find((n) => n.id === sourceId);
  const tgtNode = nodes.find((n) => n.id === targetId);
  if (!srcNode || !tgtNode) return { valid: false, reason: 'Unknown node' };

  const srcDef = NODE_TYPES[srcNode.type] ?? {};
  const tgtDef = NODE_TYPES[tgtNode.type] ?? {};
  if (srcDef.hasOutput === false) return { valid: false, reason: `${srcDef.label ?? 'This node'} has no output` };
  if (tgtDef.hasInput === false)  return { valid: false, reason: `${tgtDef.label ?? 'This node'} has no input` };

  const alreadyConnected = edges.some((e) => e.sourceId === sourceId && e.targetId === targetId);
  if (alreadyConnected) return { valid: false, reason: 'Already connected' };

  if (createsCycle(edges, sourceId, targetId)) return { valid: false, reason: 'Would create a cycle' };

  return { valid: true, reason: '' };
}

// ── Edge context menu (portal, escapes canvas overflow) ──────────
function EdgeContextMenu({ x, y, onDelete, onClose }) {
  useEffect(() => {
    const close = () => onClose();
    window.addEventListener('mousedown', close);
    window.addEventListener('scroll', close, true);
    return () => { window.removeEventListener('mousedown', close); window.removeEventListener('scroll', close, true); };
  }, [onClose]);

  return createPortal(
    <div
      className="pc-edge-ctx-menu"
      style={{ left: x, top: y }}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="pc-edge-ctx-header">Connection</div>
      <button className="pc-edge-ctx-item danger" onClick={onDelete}>
        Delete Connection
      </button>
    </div>,
    document.body
  );
}

export function PipelineCanvas({
  nodes = [],
  edges = [],
  selectedId = null,
  zoom = 1,
  onSelectNode,
  onSelectEdge,
  onMoveNode,
  onAddNode,
  onAddEdge,
  onDeleteNode,
  onDeleteEdge,
  onDeselectAll,
}) {
  const canvasRef = useRef(null);      // scrollable viewport (.pc-canvas)

  // Drag state for moving a node
  const drag = useRef({ active: false, nodeId: null, startMouse: null, startPos: null });

  // Pan state for dragging the empty background
  const pan = useRef({ active: false, startMouse: null, startScroll: null, moved: false });

  // Ghost edge state (drawing a new edge)
  const [ghost, setGhost] = useState(null); // { sourceId, mousePos, targetId, valid }

  // Right-click context menu for an edge
  const [ctxMenu, setCtxMenu] = useState(null); // { edgeId, x, y }

  // ── Canvas logical size (grows with the pipeline) ───────────
  const canvasSize = useMemo(() => {
    const maxX = nodes.reduce((m, n) => Math.max(m, n.position.x + NODE_WIDTH), 0);
    const maxY = nodes.reduce((m, n) => Math.max(m, n.position.y + NODE_HEIGHT), 0);
    return { w: Math.max(MIN_CANVAS_W, maxX + 500), h: Math.max(MIN_CANVAS_H, maxY + 400) };
  }, [nodes]);

  // Convert a client (viewport) coordinate into canvas-content coordinates,
  // accounting for the current scroll offset and zoom level.
  const clientToCanvas = useCallback((clientX, clientY) => {
    const rect = canvasRef.current.getBoundingClientRect();
    const sl = canvasRef.current.scrollLeft;
    const st = canvasRef.current.scrollTop;
    return {
      x: (clientX - rect.left + sl) / zoom,
      y: (clientY - rect.top + st) / zoom,
    };
  }, [zoom]);

  // ── Drop from palette ──────────────────────────────────────
  const handleDragOver = (e) => { e.preventDefault(); };

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    const type = e.dataTransfer.getData('node-type');
    if (!type || !NODE_TYPES[type]) return;

    const { x: cx, y: cy } = clientToCanvas(e.clientX, e.clientY);
    const x = cx - NODE_WIDTH / 2;
    const y = cy - NODE_HEIGHT / 2;
    onAddNode?.(type, { x: Math.max(0, Math.round(x)), y: Math.max(0, Math.round(y)) });
  }, [clientToCanvas, onAddNode]);

  // ── Node dragging ──────────────────────────────────────────
  const handleNodeDragStart = useCallback((e, nodeId) => {
    const node = nodes.find((n) => n.id === nodeId);
    if (!node) return;
    drag.current = {
      active: true,
      nodeId,
      startMouse: { x: e.clientX, y: e.clientY },
      startPos: { ...node.position },
    };
    window.addEventListener('mousemove', handleNodeMouseMove);
    window.addEventListener('mouseup',   handleNodeMouseUp);
  }, [nodes]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleNodeMouseMove = useCallback((e) => {
    if (!drag.current.active) return;
    const dx = (e.clientX - drag.current.startMouse.x) / zoom;
    const dy = (e.clientY - drag.current.startMouse.y) / zoom;
    onMoveNode?.(drag.current.nodeId, {
      x: Math.max(0, Math.round(drag.current.startPos.x + dx)),
      y: Math.max(0, Math.round(drag.current.startPos.y + dy)),
    });
  }, [zoom, onMoveNode]);

  const handleNodeMouseUp = useCallback(() => {
    drag.current.active = false;
    window.removeEventListener('mousemove', handleNodeMouseMove);
    window.removeEventListener('mouseup',   handleNodeMouseUp);
  }, [handleNodeMouseMove]);

  // ── Canvas pan (drag empty background) ──────────────────────
  const handleCanvasMouseDown = useCallback((e) => {
    // Only start a pan when the mousedown originated on empty canvas —
    // node/port/edge handlers already stopPropagation for their own cases.
    if (e.target.closest('.pc-node') || e.target.closest('[data-edge-id]')) return;
    if (e.button !== 0) return; // left button only
    pan.current = {
      active: true, moved: false,
      startMouse: { x: e.clientX, y: e.clientY },
      startScroll: { x: canvasRef.current.scrollLeft, y: canvasRef.current.scrollTop },
    };
    window.addEventListener('mousemove', handlePanMouseMove);
    window.addEventListener('mouseup',   handlePanMouseUp);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handlePanMouseMove = useCallback((e) => {
    if (!pan.current.active) return;
    const dx = e.clientX - pan.current.startMouse.x;
    const dy = e.clientY - pan.current.startMouse.y;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) pan.current.moved = true;
    if (!pan.current.moved) return;
    canvasRef.current.scrollLeft = pan.current.startScroll.x - dx;
    canvasRef.current.scrollTop  = pan.current.startScroll.y - dy;
    canvasRef.current.classList.add('panning');
  }, []);

  const handlePanMouseUp = useCallback(() => {
    pan.current.active = false;
    canvasRef.current?.classList.remove('panning');
    window.removeEventListener('mousemove', handlePanMouseMove);
    window.removeEventListener('mouseup',   handlePanMouseUp);
  }, [handlePanMouseMove]);

  // Shift + wheel → force horizontal scroll even on mice without native shift-scroll
  const handleWheel = useCallback((e) => {
    if (e.shiftKey && e.deltaY !== 0 && e.deltaX === 0) {
      e.currentTarget.scrollLeft += e.deltaY;
      e.preventDefault();
    }
  }, []);

  // ── Edge drawing (port → port) with live invalid-connection feedback ──
  const handlePortMouseDown = useCallback((e, nodeId, portType) => {
    if (portType !== 'output') return; // start from output only
    e.stopPropagation();
    const node = nodes.find((n) => n.id === nodeId);
    if (!node) return;
    setGhost({ sourceId: nodeId, mousePos: portPos(node, 'output'), targetId: null, valid: true });

    const onMove = (mv) => {
      const canvasPt = clientToCanvas(mv.clientX, mv.clientY);
      const target = document.elementFromPoint(mv.clientX, mv.clientY);
      const targetNodeEl = target?.closest('[data-node-id]');
      const targetId = targetNodeEl?.dataset.nodeId ?? null;
      const { valid } = targetId
        ? connectionValidity(nodes, edges, nodeId, targetId)
        : { valid: true };
      setGhost({ sourceId: nodeId, mousePos: canvasPt, targetId, valid: targetId ? valid : true });
    };
    const onUp = (mu) => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      const target = document.elementFromPoint(mu.clientX, mu.clientY);
      const targetNodeEl = target?.closest('[data-node-id]');
      const targetId = targetNodeEl?.dataset.nodeId;
      if (targetId) {
        const { valid } = connectionValidity(nodes, edges, nodeId, targetId);
        if (valid) onAddEdge?.(nodeId, targetId);
      }
      setGhost(null);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup',   onUp);
  }, [nodes, edges, clientToCanvas, onAddEdge]);

  // ── Edge context menu ────────────────────────────────────────
  const handleEdgeContextMenu = useCallback((edgeId, clientX, clientY) => {
    setCtxMenu({ edgeId, x: clientX, y: clientY });
  }, []);

  // ── Keyboard: Delete / Backspace removes the current selection ──
  useEffect(() => {
    const onKeyDown = (e) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return;
      const tag = e.target?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target?.isContentEditable) return;
      if (!selectedId) return;
      e.preventDefault();
      if (edges.some((ed) => ed.id === selectedId)) onDeleteEdge?.(selectedId);
      else if (nodes.some((n) => n.id === selectedId)) onDeleteNode?.(selectedId);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [selectedId, edges, nodes, onDeleteEdge, onDeleteNode]);

  // Clean up global listeners on unmount
  useEffect(() => () => {
    window.removeEventListener('mousemove', handleNodeMouseMove);
    window.removeEventListener('mouseup',   handleNodeMouseUp);
    window.removeEventListener('mousemove', handlePanMouseMove);
    window.removeEventListener('mouseup',   handlePanMouseUp);
  }, [handleNodeMouseMove, handleNodeMouseUp, handlePanMouseMove, handlePanMouseUp]);

  // ── Render ─────────────────────────────────────────────────
  // Mark which ports are connected for visual state
  const connectedOutputs = new Set(edges.map((e) => e.sourceId));
  const connectedInputs  = new Set(edges.map((e) => e.targetId));
  const augmentedNodes   = nodes.map((n) => ({
    ...n,
    _connectedOutput: connectedOutputs.has(n.id),
    _connectedInput:  connectedInputs.has(n.id),
  }));

  return (
    <div
      ref={canvasRef}
      className={`pc-canvas ${ghost ? (ghost.valid === false ? 'drag-invalid' : 'drag-target') : ''}`}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      onClick={() => onDeselectAll?.()}
      onMouseDown={handleCanvasMouseDown}
      onWheel={handleWheel}
    >
      {/* Scaled sizer — gives the scrollable viewport correct scrollWidth/Height at the current zoom */}
      <div
        className="pc-canvas-sizer"
        style={{ width: canvasSize.w * zoom, height: canvasSize.h * zoom }}
      >
        <div
          className="pc-canvas-inner"
          style={{ width: canvasSize.w, height: canvasSize.h, transform: `scale(${zoom})` }}
        >
          {/* SVG layer for edges */}
          <svg className="pc-svg-layer">
            <defs>
              <marker id="pc-arrow-idle" markerWidth="10" markerHeight="10" refX="8" refY="4" markerUnits="userSpaceOnUse" orient="auto">
                <path d="M0,0 L0,8 L9,4 z" fill="#94A3B8" />
              </marker>
              <marker id="pc-arrow-selected" markerWidth="10" markerHeight="10" refX="8" refY="4" markerUnits="userSpaceOnUse" orient="auto">
                <path d="M0,0 L0,8 L9,4 z" fill="#4F46E5" />
              </marker>
              <marker id="pc-arrow-success" markerWidth="10" markerHeight="10" refX="8" refY="4" markerUnits="userSpaceOnUse" orient="auto">
                <path d="M0,0 L0,8 L9,4 z" fill="#10B981" />
              </marker>
              <marker id="pc-arrow-error" markerWidth="10" markerHeight="10" refX="8" refY="4" markerUnits="userSpaceOnUse" orient="auto">
                <path d="M0,0 L0,8 L9,4 z" fill="#EF4444" />
              </marker>
            </defs>

            {edges.map((edge) => {
              const src = augmentedNodes.find((n) => n.id === edge.sourceId);
              const tgt = augmentedNodes.find((n) => n.id === edge.targetId);
              if (!src || !tgt) return null;
              return (
                <PipelineEdge
                  key={edge.id}
                  edge={edge}
                  sourcePos={portPos(src, 'output')}
                  targetPos={portPos(tgt, 'input')}
                  selected={selectedId === edge.id}
                  animated={src.status === 'running' || tgt.status === 'running'}
                  status={tgt.status !== 'idle' ? tgt.status : 'idle'}
                  onSelect={onSelectEdge}
                  onDelete={onDeleteEdge}
                  onContextMenu={handleEdgeContextMenu}
                />
              );
            })}

            {/* Ghost edge while drawing */}
            {ghost && (() => {
              const srcNode = augmentedNodes.find((n) => n.id === ghost.sourceId);
              return srcNode ? (
                <GhostEdge
                  sourcePos={portPos(srcNode, 'output')}
                  mousePos={ghost.mousePos}
                  invalid={ghost.targetId ? !ghost.valid : false}
                />
              ) : null;
            })()}
          </svg>

          {/* Nodes */}
          {augmentedNodes.map((node) => (
            <PipelineNode
              key={node.id}
              node={node}
              selected={selectedId === node.id}
              onSelect={onSelectNode}
              onDelete={onDeleteNode}
              onDragStart={handleNodeDragStart}
              onPortMouseDown={handlePortMouseDown}
            />
          ))}
        </div>
      </div>

      {/* Empty hint */}
      {nodes.length === 0 && (
        <div className="pc-empty-hint">
          <div className="pc-empty-icon">⚙️</div>
          <div className="pc-empty-text">Drag a node from the palette to get started</div>
          <div className="pc-empty-sub">Connect nodes with arrows to build your pipeline</div>
        </div>
      )}

      {/* Edge right-click context menu */}
      {ctxMenu && (
        <EdgeContextMenu
          x={ctxMenu.x}
          y={ctxMenu.y}
          onDelete={() => { onDeleteEdge?.(ctxMenu.edgeId); setCtxMenu(null); }}
          onClose={() => setCtxMenu(null)}
        />
      )}
    </div>
  );
}
