/**
 * RowInspector — right-side panel showing one result row transposed into
 * a COLUMN / VALUE list ("pivot" view), the natural way to read a wide
 * row without horizontal scrolling. Real data straight from the already-
 * fetched result set — no separate fetch.
 *
 * Expand/Collapse: any value long enough to be unwieldy in a narrow panel
 * (long text, JSON blobs, multi-line strings) starts clamped to a few
 * lines with a per-field toggle to reveal the rest — plus an Expand All /
 * Collapse All header toggle for wide rows with several long values.
 */
import { useState } from 'react';
import { X, Copy, ChevronDown, ChevronUp, ChevronsDownUp, ChevronsUpDown } from 'lucide-react';

const LONG_VALUE_THRESHOLD = 160; // chars — beyond this (or any newline), a value starts clamped

function isLongValue(value) {
  if (value === null || value === undefined) return false;
  const s = String(value);
  return s.length > LONG_VALUE_THRESHOLD || s.includes('\n');
}

export default function RowInspector({ rowNumber, columns, row, onClose }) {
  const [expanded, setExpanded] = useState(() => new Set());

  const copy = (text) => { navigator.clipboard?.writeText(text).catch(() => {}); };
  const copyRow = () => copy(columns.map((c, i) => `${c}\t${row[i] === null ? '' : row[i]}`).join('\n'));

  const longIndices = columns.map((_, i) => i).filter((i) => isLongValue(row[i]));
  const allExpanded = longIndices.length > 0 && longIndices.every((i) => expanded.has(i));

  const toggleField = (i) => setExpanded((s) => {
    const next = new Set(s);
    next.has(i) ? next.delete(i) : next.add(i);
    return next;
  });
  const expandAll = () => setExpanded(new Set(longIndices));
  const collapseAll = () => setExpanded(new Set());

  return (
    <aside className="sqlw-inspector">
      <div className="sqlw-inspector-header">
        <span>Row #{rowNumber}</span>
        <div className="sqlw-inspector-actions">
          {longIndices.length > 0 && (
            <button onClick={allExpanded ? collapseAll : expandAll} title={allExpanded ? 'Collapse all long values' : 'Expand all long values'}>
              {allExpanded ? <ChevronsDownUp size={13} /> : <ChevronsUpDown size={13} />}
            </button>
          )}
          <button onClick={copyRow} title="Copy row"><Copy size={13} /></button>
          <button onClick={onClose} title="Close"><X size={13} /></button>
        </div>
      </div>
      <div className="sqlw-inspector-body">
        {columns.map((c, i) => {
          const raw = row[i];
          const long = isLongValue(raw);
          const isExpanded = expanded.has(i);
          const display = raw === null ? 'NULL' : String(raw);
          return (
            <div key={c} className="sqlw-inspector-field">
              <div className="sqlw-inspector-field-name">{c}</div>
              <div className="sqlw-inspector-field-value-row">
                <span className={`sqlw-inspector-field-value ${raw === null ? 'null' : ''} ${long && !isExpanded ? 'sqlw-inspector-clamped' : ''}`}>
                  {display}
                </span>
                <div className="sqlw-inspector-field-actions">
                  {long && (
                    <button
                      className="sqlw-inspector-toggle"
                      onClick={() => toggleField(i)}
                      title={isExpanded ? 'Collapse' : 'Expand'}
                    >
                      {isExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                    </button>
                  )}
                  <Copy size={12} className="sqlw-inspector-copy" onClick={() => copy(raw === null ? '' : String(raw))} title="Copy value" />
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}
