/**
 * LineageGraph — interactive node/edge renderer for one pipeline's
 * lineage graph (Pipeline Lineage and Execution Lineage tabs share this).
 * No external graph library — a small layered auto-layout (BFS depth
 * over FLOW/CONTAINS/TRANSFORMATION edges; PIPELINE's own DEFINES edges
 * are drawn but excluded from layering so the canvas node grid stays
 * clean) plus pan (drag) and zoom (wheel), rendered as absolutely
 * positioned divs over an SVG edge layer.
 *
 * Clicking a node opens a PERSISTENT properties panel (stays open until
 * explicitly closed or another node is clicked) — no click-and-hold,
 * per the explicit requirement.
 */
import { useMemo, useRef, useState } from 'react';
import { Maximize2, X, ZoomIn, ZoomOut, RotateCcw } from 'lucide-react';
import { LINEAGE_NODE_META } from '@/services/api/lineageApi.js';
import './lineage.css';

const COL_W = 210;
const ROW_H = 90;
const NODE_W = 168;

function layoutGraph(nodes, edges) {
  const flowEdges = edges.filter((e) => e.edge_type !== 'DEFINES');
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const outAdj = new Map(nodes.map((n) => [n.id, []]));
  const inDegree = new Map(nodes.map((n) => [n.id, 0]));
  for (const e of flowEdges) {
    if (!byId.has(e.source) || !byId.has(e.target)) continue;
    outAdj.get(e.source)?.push(e.target);
    inDegree.set(e.target, (inDegree.get(e.target) || 0) + 1);
  }
  const layer = new Map();
  const queue = nodes.filter((n) => (inDegree.get(n.id) || 0) === 0 && n.type !== 'PIPELINE').map((n) => n.id);
  queue.forEach((id) => layer.set(id, 0));
  // Real bug fix: this is a Bellman-Ford-style longest-path relaxation,
  // which only terminates on a DAG. A pipeline whose source and target
  // read/write the SAME connection — an ordinary, common shape, not a
  // contrived edge case — puts a real cycle in the FLOW/CONTAINS/
  // TRANSFORMATION edges (e.g. connection -> source table -> source
  // dataset -> transform -> target dataset -> target table -> back to
  // the same connection). Without a bound, that cycle relaxes the same
  // nodes' layer forever, pushing onto `queue` until it exceeds JS's
  // max array length and crashes the whole app (RangeError: Invalid
  // array length). Cap each node's re-queue count at nodes.length + 1 —
  // the same bound Bellman-Ford uses to detect it has hit a cycle — so
  // a cyclic graph just stops refining that node's layer (keeps
  // whatever it last settled on) instead of looping forever. A real
  // DAG never gets close to this bound, so normal graphs are unaffected.
  const relaxCount = new Map();
  const maxRelax = nodes.length + 1;
  let qi = 0;
  while (qi < queue.length) {
    const id = queue[qi++];
    const d = layer.get(id);
    for (const next of outAdj.get(id) || []) {
      if (!layer.has(next) || layer.get(next) < d + 1) {
        const seen = (relaxCount.get(next) || 0) + 1;
        if (seen > maxRelax) continue;
        relaxCount.set(next, seen);
        layer.set(next, d + 1);
        queue.push(next);
      }
    }
  }
  // Anything unreached (isolated, or only connected via DEFINES) goes layer 0.
  for (const n of nodes) {
    if (n.type === 'PIPELINE') continue;
    if (!layer.has(n.id)) layer.set(n.id, 0);
  }

  const byLayer = new Map();
  for (const n of nodes) {
    if (n.type === 'PIPELINE') continue;
    const l = layer.get(n.id) ?? 0;
    if (!byLayer.has(l)) byLayer.set(l, []);
    byLayer.get(l).push(n);
  }

  const positions = new Map();
  for (const [l, ns] of byLayer) {
    ns.forEach((n, i) => positions.set(n.id, { x: l * COL_W + 20, y: i * ROW_H + 50 }));
  }
  const maxLayer = Math.max(0, ...[...byLayer.keys()]);
  const maxRows = Math.max(1, ...[...byLayer.values()].map((v) => v.length));
  return { positions, width: (maxLayer + 1) * COL_W + NODE_W, height: maxRows * ROW_H + 80 };
}

export function LineageGraph({ nodes, edges, height = 520 }) {
  const [selected, setSelected] = useState(null);
  const [search, setSearch] = useState('');
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const dragRef = useRef(null);
  const containerRef = useRef(null);

  const { positions, width, height: gHeight } = useMemo(() => layoutGraph(nodes, edges), [nodes, edges]);

  const matchesSearch = (n) => !search || (n.label + ' ' + (n.subtitle || '')).toLowerCase().includes(search.toLowerCase());

  const onMouseDown = (e) => {
    if (e.target.closest('.lin-node')) return;
    dragRef.current = { startX: e.clientX, startY: e.clientY, panX: pan.x, panY: pan.y };
  };
  const onMouseMove = (e) => {
    if (!dragRef.current) return;
    const dx = e.clientX - dragRef.current.startX;
    const dy = e.clientY - dragRef.current.startY;
    setPan({ x: dragRef.current.panX + dx, y: dragRef.current.panY + dy });
  };
  const onMouseUp = () => { dragRef.current = null; };
  const onWheel = (e) => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? -0.08 : 0.08;
    setZoom((z) => Math.min(2, Math.max(0.3, z + delta)));
  };
  const resetView = () => { setZoom(1); setPan({ x: 0, y: 0 }); };
  const toggleFullscreen = () => {
    if (!document.fullscreenElement) containerRef.current?.requestFullscreen?.();
    else document.exitFullscreen?.();
  };

  const canvasNodes = nodes.filter((n) => n.type !== 'PIPELINE');
  const edgeLines = edges
    .filter((e) => e.edge_type !== 'DEFINES')
    .map((e) => {
      const a = positions.get(e.source);
      const b = positions.get(e.target);
      if (!a || !b) return null;
      const x1 = a.x + NODE_W, y1 = a.y + 24, x2 = b.x, y2 = b.y + 24;
      const midX = (x1 + x2) / 2;
      return { ...e, path: `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}` };
    })
    .filter(Boolean);

  return (
    <div className="lin-graph-shell" ref={containerRef} style={{ height }}>
      <div
        className="lin-graph-main"
        onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={onMouseUp} onMouseLeave={onMouseUp} onWheel={onWheel}
      >
        <div className="lin-graph-toolbar">
          <button title="Zoom in" onClick={() => setZoom((z) => Math.min(2, z + 0.15))}><ZoomIn size={14} /></button>
          <button title="Zoom out" onClick={() => setZoom((z) => Math.max(0.3, z - 0.15))}><ZoomOut size={14} /></button>
          <button title="Reset view" onClick={resetView}><RotateCcw size={14} /></button>
          <button title="Fullscreen" onClick={toggleFullscreen}><Maximize2 size={14} /></button>
        </div>
        <div className="lin-graph-search">
          <input className="lin-input" placeholder="Search nodes…" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <div className="lin-graph-canvas" style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`, width, height: gHeight }}>
          <svg width={width} height={gHeight} style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none' }}>
            <defs>
              <marker id="lin-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
                <path d="M0,0 L7,3 L0,6 Z" fill="#94A3B8" />
              </marker>
            </defs>
            {edgeLines.map((e) => (
              <g key={e.id}>
                <path d={e.path} fill="none" stroke={e.edge_type === 'TRANSFORMATION' ? '#F97316' : '#94A3B8'} strokeWidth={1.4} markerEnd="url(#lin-arrow)" opacity={0.8} />
                {e.label && (
                  <text x={(positions.get(e.source).x + NODE_W + positions.get(e.target).x) / 2} y={(positions.get(e.source).y + positions.get(e.target).y) / 2 + 20} fontSize="9" fill="#64748B" textAnchor="middle">
                    {e.label.length > 28 ? e.label.slice(0, 28) + '…' : e.label}
                  </text>
                )}
              </g>
            ))}
          </svg>
          {canvasNodes.map((n) => {
            const pos = positions.get(n.id);
            if (!pos) return null;
            const meta = LINEAGE_NODE_META[n.type] || { icon: '◆', color: '#64748B', bg: '#F8FAFC' };
            const dim = search && !matchesSearch(n);
            return (
              <div
                key={n.id}
                className={`lin-node ${selected?.id === n.id ? 'selected' : ''} ${dim ? 'dim' : ''}`}
                style={{ left: pos.x, top: pos.y, '--nc': meta.color, '--nb': meta.bg }}
                onClick={() => setSelected(n)}
              >
                <div className="lin-node-head"><span>{meta.icon}</span><span>{n.label}</span></div>
                <div className="lin-node-type">{n.type}</div>
                {n.subtitle && <div className="lin-node-sub" title={n.subtitle}>{n.subtitle}</div>}
                {n.execution && (
                  <span className={`lin-node-exec ${n.execution.status === 'error' ? 'error' : 'success'}`}>
                    {n.execution.rows_in ?? 0}→{n.execution.rows_out ?? 0} rows
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>
      <div className="lin-props-panel">
        {!selected ? (
          <div className="lin-props-empty">Click a node to see its details.</div>
        ) : (
          <>
            <div className="lin-props-head">
              <strong>{selected.label}</strong>
              <button className="lin-props-close" onClick={() => setSelected(null)}><X size={14} /></button>
            </div>
            <div className="lin-field-row"><span className="lin-field-label">Type</span><span className="lin-field-value">{selected.type}</span></div>
            {selected.subtitle && <div className="lin-field-row"><span className="lin-field-label">Detail</span><span className="lin-field-value">{selected.subtitle}</span></div>}
            {Object.entries(selected.meta || {}).map(([k, v]) => v != null && (
              <div className="lin-field-row" key={k}><span className="lin-field-label">{k}</span><span className="lin-field-value">{String(v)}</span></div>
            ))}
            {selected.execution && (
              <>
                <div className="lin-field-row"><span className="lin-field-label">Execution Status</span><span className="lin-field-value">{selected.execution.status}</span></div>
                <div className="lin-field-row"><span className="lin-field-label">Rows In → Out</span><span className="lin-field-value">{selected.execution.rows_in ?? 0} → {selected.execution.rows_out ?? 0}</span></div>
                {selected.execution.duration_ms != null && <div className="lin-field-row"><span className="lin-field-label">Duration</span><span className="lin-field-value">{selected.execution.duration_ms}ms</span></div>}
                {selected.execution.message && <div className="lin-field-row"><span className="lin-field-label">Message</span><span className="lin-field-value">{selected.execution.message}</span></div>}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
