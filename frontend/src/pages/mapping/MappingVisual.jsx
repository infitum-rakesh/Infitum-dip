/**
 * MappingVisual — source/target column lists with curved SVG lines
 * connecting mapped pairs (spec section 35). Click a line (or either
 * endpoint pill) to open that row in the editor. A row with more than
 * one source column (a multi-column expression) draws one line per
 * source column into the same target — visually showing the fan-in.
 *
 * Line endpoints are measured from the REAL rendered pill positions
 * (useLayoutEffect + getBoundingClientRect), not computed from a
 * hardcoded row height — pills have a real `margin-bottom: 4px` (see
 * mapping.css) on top of their `ROW_HEIGHT` inline height, and the
 * column title above the first pill isn't `TOP_OFFSET` tall either, so
 * a purely-arithmetic y-position drifts further off the real pill with
 * every row and was never right even for row 0. This is the same root
 * cause and fix pattern as PipelineCanvas.jsx's node/edge alignment.
 */
import { useLayoutEffect, useRef, useState } from 'react';

const ROW_HEIGHT = 30;
const TOP_OFFSET = 10;
const STATUS_COLOR = {
  MAPPED: '#10B981',
  SUGGESTED: '#F59E0B',
  CONFLICT: '#EF4444',
  UNMAPPED: '#94A3B8',
};

export function MappingVisual({ sourceColumns, targetColumns, mappings, onSelectRow }) {
  const wrapRef = useRef(null);
  const sourceRefs = useRef({});
  const targetRefs = useRef({});
  // {[columnName]: centerYRelativeToSvgWrapTop} — real, measured pill centers.
  const [sourceY, setSourceY] = useState({});
  const [targetY, setTargetY] = useState({});

  const mappedSourceNames = new Set(mappings.flatMap((m) => m.source_columns || []));
  const mappedTargetNames = new Set(mappings.map((m) => m.target_column));

  const height = Math.max(sourceColumns.length, targetColumns.length) * ROW_HEIGHT + TOP_OFFSET * 2;

  useLayoutEffect(() => {
    const wrapEl = wrapRef.current;
    if (!wrapEl) return;
    // .ocm-visual-svg-wrap is a flex sibling of both column divs (see
    // render below) — same top-aligned flex row, so its own top edge is
    // the correct zero-point for both source and target pill centers.
    const wrapTop = wrapEl.getBoundingClientRect().top;
    const measure = (refs) => {
      const next = {};
      Object.entries(refs.current).forEach(([name, el]) => {
        if (!el) return;
        const r = el.getBoundingClientRect();
        next[name] = (r.top + r.bottom) / 2 - wrapTop;
      });
      return next;
    };
    setSourceY(measure(sourceRefs));
    setTargetY(measure(targetRefs));
  }, [sourceColumns, targetColumns, mappings]);

  const lines = [];
  mappings.forEach((m) => {
    const tCenter = targetY[m.target_column];
    if (tCenter === undefined) return;
    (m.source_columns || []).forEach((sc) => {
      const sCenter = sourceY[sc];
      if (sCenter === undefined) return;
      lines.push({ rowId: m.id, status: m.status, sY: sCenter, tY: tCenter });
    });
  });

  return (
    <div className="ocm-visual">
      <div className="ocm-visual-col">
        <div className="ocm-visual-col-title">Source</div>
        {sourceColumns.map((c) => (
          <div
            key={c.name}
            ref={(el) => { sourceRefs.current[c.name] = el; }}
            className={`ocm-visual-pill ${mappedSourceNames.has(c.name) ? 'mapped' : ''}`}
            style={{ height: ROW_HEIGHT }}
          >
            {c.name}
          </div>
        ))}
      </div>

      <div ref={wrapRef} className="ocm-visual-svg-wrap" style={{ height }}>
        <svg width="100%" height={height} viewBox={`0 0 180 ${height}`} preserveAspectRatio="none" style={{ display: 'block' }}>
          {lines.map((l, i) => {
            return (
              <path
                key={i}
                d={`M 0 ${l.sY} C 90 ${l.sY}, 90 ${l.tY}, 180 ${l.tY}`}
                vectorEffect="non-scaling-stroke"
                fill="none"
                stroke={STATUS_COLOR[l.status] || STATUS_COLOR.MAPPED}
                strokeWidth={2}
                opacity={0.85}
                className="ocm-visual-line"
                onClick={() => onSelectRow?.(l.rowId)}
              />
            );
          })}
        </svg>
      </div>

      <div className="ocm-visual-col">
        <div className="ocm-visual-col-title">Target</div>
        {targetColumns.map((c) => (
          <div
            key={c.name}
            ref={(el) => { targetRefs.current[c.name] = el; }}
            className={`ocm-visual-pill ${mappedTargetNames.has(c.name) ? 'mapped' : ''}`}
            style={{ height: ROW_HEIGHT }}
          >
            {c.name}
          </div>
        ))}
      </div>
    </div>
  );
}
