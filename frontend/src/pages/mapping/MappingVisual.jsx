/**
 * MappingVisual — source/target column lists with curved SVG lines
 * connecting mapped pairs (spec section 35). Click a line (or either
 * endpoint pill) to open that row in the editor. A row with more than
 * one source column (a multi-column expression) draws one line per
 * source column into the same target — visually showing the fan-in.
 */
import { useMemo } from 'react';

const ROW_HEIGHT = 30;
const TOP_OFFSET = 10;
const STATUS_COLOR = {
  MAPPED: '#10B981',
  SUGGESTED: '#F59E0B',
  CONFLICT: '#EF4444',
  UNMAPPED: '#94A3B8',
};

export function MappingVisual({ sourceColumns, targetColumns, mappings, onSelectRow }) {
  const sourceIndex = useMemo(() => Object.fromEntries(sourceColumns.map((c, i) => [c.name, i])), [sourceColumns]);
  const targetIndex = useMemo(() => Object.fromEntries(targetColumns.map((c, i) => [c.name, i])), [targetColumns]);
  const mappedSourceNames = new Set(mappings.flatMap((m) => m.source_columns || []));
  const mappedTargetNames = new Set(mappings.map((m) => m.target_column));

  const height = Math.max(sourceColumns.length, targetColumns.length) * ROW_HEIGHT + TOP_OFFSET * 2;

  const lines = [];
  mappings.forEach((m) => {
    const tY = targetIndex[m.target_column];
    if (tY === undefined) return;
    (m.source_columns || []).forEach((sc) => {
      const sY = sourceIndex[sc];
      if (sY === undefined) return;
      lines.push({ rowId: m.id, status: m.status, sY: sY * ROW_HEIGHT + TOP_OFFSET + ROW_HEIGHT / 2, tY: tY * ROW_HEIGHT + TOP_OFFSET + ROW_HEIGHT / 2 });
    });
  });

  return (
    <div className="ocm-visual">
      <div className="ocm-visual-col">
        <div className="ocm-visual-col-title">Source</div>
        {sourceColumns.map((c) => (
          <div key={c.name} className={`ocm-visual-pill ${mappedSourceNames.has(c.name) ? 'mapped' : ''}`} style={{ height: ROW_HEIGHT }}>
            {c.name}
          </div>
        ))}
      </div>

      <div className="ocm-visual-svg-wrap" style={{ height }}>
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
          <div key={c.name} className={`ocm-visual-pill ${mappedTargetNames.has(c.name) ? 'mapped' : ''}`} style={{ height: ROW_HEIGHT }}>
            {c.name}
          </div>
        ))}
      </div>
    </div>
  );
}
