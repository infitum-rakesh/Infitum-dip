/**
 * ResultGrid — the interactive table for a SQL Workspace result set.
 * Everything here operates on rows already in the browser (bounded by
 * the query's row limit — see ROW_LIMITS in SqlWorkspace.jsx, max 1000):
 * real client-side sort/filter (no server round-trip needed since the
 * bounded set is already local), real column resize/reorder/freeze,
 * real row selection, real clipboard copy, real row inspector. Nothing
 * here silently truncates further — `rows` is the full set this grid
 * ever sees.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUp, ArrowDown, Filter, Pin, Download, Workflow, Loader2, AlertTriangle, X } from 'lucide-react';
import RowInspector from './RowInspector.jsx';
import ExportsPanel from './ExportsPanel.jsx';
import { exportResult } from './exportUtils.js';

function compareValues(a, b) {
  if (a === null && b === null) return 0;
  if (a === null) return -1;
  if (b === null) return 1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  const na = Number(a), nb = Number(b);
  if (!Number.isNaN(na) && !Number.isNaN(nb) && a !== '' && b !== '') return na - nb;
  return String(a).localeCompare(String(b));
}

// Both export popovers used to be position:absolute inside .sqlw-results-
// scroll, which is overflow:auto so the results panel itself can scroll —
// that clips an absolutely-positioned descendant the moment the results
// panel is short, which is exactly the "popup bottom hidden, can't scroll
// inside it" report. position:fixed, computed from the button's real
// screen rect, escapes that clipping the same way .sqlw-grid-ctxmenu
// already does elsewhere in this file — and capping/flipping against the
// actual viewport guarantees the popover is always fully reachable.
function computeFloatingMenuPos(rect, { width = 220, minHeight = 160, margin = 8 } = {}) {
  const spaceBelow = window.innerHeight - rect.bottom - margin;
  const spaceAbove = rect.top - margin;
  const left = Math.max(margin, Math.min(rect.right - width, window.innerWidth - width - margin));
  if (spaceBelow >= minHeight || spaceBelow >= spaceAbove) {
    return { left, top: rect.bottom + 4, maxHeight: Math.max(spaceBelow, minHeight) };
  }
  return { left, bottom: window.innerHeight - rect.top + 4, maxHeight: Math.max(spaceAbove, minHeight) };
}

export default function ResultGrid({
  columns, rows, queryName, canExport = true,
  onSendToTransform, sendingToTransform = false, sendToTransformError = '',
  connectionId, sqlText,
}) {
  const [colOrder, setColOrder] = useState(() => columns.map((_, i) => i));
  const [colWidths, setColWidths] = useState({});
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState('asc');
  const [filters, setFilters] = useState({});
  const [showFilters, setShowFilters] = useState(false);
  const [frozenFirst, setFrozenFirst] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [menu, setMenu] = useState(null); // {x,y,kind:'cell'|'row'|'column'|'header', rowIdx, colIdx}
  const [inspecting, setInspecting] = useState(null); // original row index
  const [exportOpen, setExportOpen] = useState(false);
  const [exportMenuPos, setExportMenuPos] = useState(null);
  const [bgExportOpen, setBgExportOpen] = useState(false); // background-export popover (Parquet + full-history CSV/JSON/Excel/Parquet jobs)
  const [bgExportPos, setBgExportPos] = useState(null);
  const [bgAutoFormat, setBgAutoFormat] = useState(null);
  const exportBtnRef = useRef(null);
  const resizeRef = useRef(null);

  // A fresh result set (new columns array from the API response) resets
  // all client-side view state — a stale sort/filter/selection from the
  // PREVIOUS query silently carrying over would misrepresent this one.
  useEffect(() => {
    setColOrder(columns.map((_, i) => i));
    setColWidths({});
    setSortCol(null);
    setFilters({});
    setSelected(new Set());
    setInspecting(null);
  }, [columns]);

  useEffect(() => {
    if (!menu && !exportOpen) return;
    const close = () => { setMenu(null); setExportOpen(false); };
    window.addEventListener('click', close);
    return () => window.removeEventListener('click', close);
  }, [menu, exportOpen]);
  // The background-export popover stays open on outside clicks (it's a
  // multi-step flow — pick a format, watch a job run, download) — only
  // its own close button dismisses it.

  const displayIndices = useMemo(() => {
    let idx = rows.map((_, i) => i);
    const activeFilters = Object.entries(filters).filter(([, v]) => v);
    if (activeFilters.length) {
      idx = idx.filter((i) => activeFilters.every(([colIdxStr, needle]) => {
        const v = rows[i][Number(colIdxStr)];
        return String(v ?? '').toLowerCase().includes(needle.toLowerCase());
      }));
    }
    if (sortCol !== null) {
      idx = [...idx].sort((a, b) => compareValues(rows[a][sortCol], rows[b][sortCol]) * (sortDir === 'asc' ? 1 : -1));
    }
    return idx;
  }, [rows, filters, sortCol, sortDir]);

  const toggleSort = (colIdx) => {
    if (sortCol !== colIdx) { setSortCol(colIdx); setSortDir('asc'); }
    else if (sortDir === 'asc') setSortDir('desc');
    else { setSortCol(null); setSortDir('asc'); }
  };

  // ── Resize ──
  const startResize = (e, colIdx) => {
    e.preventDefault(); e.stopPropagation();
    const startX = e.clientX;
    const startWidth = colWidths[colIdx] || e.target.closest('th').offsetWidth;
    resizeRef.current = { colIdx, startX, startWidth };
    const onMove = (ev) => {
      // Snapshot the ref's fields into local consts INSIDE this call —
      // React's functional setState updater can run after this handler
      // returns (e.g. on the next render), by which point a same-tick
      // mouseup may have already nulled resizeRef.current. Closing over
      // these locals (not resizeRef.current itself) inside the updater
      // keeps this specific move event's math correct regardless.
      const ref = resizeRef.current;
      if (!ref) return;
      const delta = ev.clientX - ref.startX;
      const newWidth = Math.max(60, ref.startWidth + delta);
      setColWidths((w) => ({ ...w, [ref.colIdx]: newWidth }));
    };
    const onUp = () => { resizeRef.current = null; window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  // ── Reorder ──
  const onHeaderDragStart = (e, pos) => e.dataTransfer.setData('text/plain', `col:${pos}`);
  const onHeaderDrop = (e, targetPos) => {
    e.preventDefault();
    const data = e.dataTransfer.getData('text/plain');
    if (!data.startsWith('col:')) return;
    const sourcePos = Number(data.slice(4));
    if (sourcePos === targetPos) return;
    setColOrder((order) => {
      const next = [...order];
      const [moved] = next.splice(sourcePos, 1);
      next.splice(targetPos, 0, moved);
      return next;
    });
  };

  // ── Selection ──
  const toggleRow = (origIdx) => setSelected((s) => { const n = new Set(s); n.has(origIdx) ? n.delete(origIdx) : n.add(origIdx); return n; });
  const toggleAll = () => setSelected((s) => (s.size === displayIndices.length ? new Set() : new Set(displayIndices)));

  // ── Copy ──
  const copy = (text) => navigator.clipboard?.writeText(text).catch(() => {});
  const rowText = (origIdx) => colOrder.map((c) => (rows[origIdx][c] === null ? '' : rows[origIdx][c])).join('\t');
  const copyCell = (origIdx, colIdx) => copy(rows[origIdx][colIdx] === null ? '' : String(rows[origIdx][colIdx]));
  const copyRow = (origIdx) => copy(rowText(origIdx));
  const copyColumn = (colIdx) => copy([columns[colIdx], ...displayIndices.map((i) => (rows[i][colIdx] === null ? '' : rows[i][colIdx]))].join('\n'));
  const copySelected = () => copy(displayIndices.filter((i) => selected.has(i)).map(rowText).join('\n'));
  const copyResult = () => {
    const cellCount = displayIndices.length * columns.length;
    if (cellCount > 5000 && !window.confirm(`Copy all ${displayIndices.length} rows × ${columns.length} columns to the clipboard?`)) return;
    const header = colOrder.map((c) => columns[c]).join('\t');
    copy([header, ...displayIndices.map(rowText)].join('\n'));
  };

  const exportName = (queryName || 'query_result').trim() || 'query_result';
  // Current on-screen view — respects whatever sort/filter/column-reorder
  // the user has applied. Shared by Export and Send to Transform Workspace
  // so both operate on the same data the user is actually looking at.
  const currentView = () => ({
    cols: colOrder.map((c) => columns[c]),
    rows: displayIndices.map((i) => colOrder.map((c) => rows[i][c])),
  });
  const doExport = (format) => {
    const { cols, rows: viewRows } = currentView();
    exportResult(format, exportName, cols, viewRows);
    setExportOpen(false);
  };
  const doSendToTransform = () => {
    if (!onSendToTransform) return;
    const { cols, rows: viewRows } = currentView();
    setExportOpen(false);
    onSendToTransform(cols, viewRows);
  };
  // Parquet is a binary columnar format — there's no honest way to build
  // one client-side in plain JS the way CSV/JSON/TXT/XLSX are built here.
  // It's only ever produced by the real streaming writer on the backend
  // (services/sql_export_writers.py), so "Export Parquet" opens the same
  // real background-export job panel used for any full/unbounded export,
  // right here in the grid's own corner — no separate sidebar tab.
  const doBackgroundExport = () => {
    setExportOpen(false);
    if (exportBtnRef.current) {
      setBgExportPos(computeFloatingMenuPos(exportBtnRef.current.getBoundingClientRect(), { width: 340, minHeight: 220 }));
    }
    setBgAutoFormat('PARQUET');
    setBgExportOpen(true);
  };

  return (
    <div className="sqlw-grid-wrap" onContextMenu={(e) => e.preventDefault()}>
      <div className="sqlw-grid-meta">
        <span>{selected.size > 0 && `${selected.size} selected`}</span>
        <div className="sqlw-grid-toolbar">
          <button className={`sqlw-grid-tool ${showFilters ? 'active' : ''}`} onClick={() => setShowFilters((s) => !s)} title="Toggle column filters">
            <Filter size={13} />
          </button>
          <button className={`sqlw-grid-tool ${frozenFirst ? 'active' : ''}`} onClick={() => setFrozenFirst((f) => !f)} title="Freeze first column">
            <Pin size={13} />
          </button>
          {canExport && (
            <div className="sqlw-grid-export-wrap">
              <button
                ref={exportBtnRef}
                className="sqlw-grid-tool"
                onClick={(e) => {
                  e.stopPropagation();
                  setBgExportOpen(false);
                  if (!exportOpen) setExportMenuPos(computeFloatingMenuPos(e.currentTarget.getBoundingClientRect(), { width: 210, minHeight: 200 }));
                  setExportOpen((o) => !o);
                }}
                title="Export"
              >
                <Download size={13} />
              </button>
              {exportOpen && exportMenuPos && (
                <div
                  className="sqlw-grid-export-menu"
                  style={{ position: 'fixed', ...exportMenuPos, overflowY: 'auto' }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <button onClick={() => doExport('csv')}>Export CSV</button>
                  <button onClick={() => doExport('json')}>Export JSON</button>
                  <button onClick={() => doExport('xlsx')}>Export Excel</button>
                  <button onClick={() => doExport('txt')}>Export TXT</button>
                  <button onClick={doBackgroundExport} title="Parquet is generated server-side — this starts a real background export job">
                    Export Parquet (background)
                  </button>
                  {onSendToTransform && rows.length > 0 && (
                    <>
                      <hr />
                      <button onClick={doSendToTransform} disabled={sendingToTransform}>
                        {sendingToTransform ? <Loader2 size={12} className="sqlw-spin" /> : <Workflow size={12} />}
                        {sendingToTransform ? 'Sending…' : 'Send to Transform Workspace'}
                      </button>
                    </>
                  )}
                </div>
              )}
              {bgExportOpen && bgExportPos && (
                <div
                  className="sqlw-grid-export-menu sqlw-grid-bgexport-panel"
                  style={{ position: 'fixed', ...bgExportPos }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <div className="sqlw-grid-bgexport-header">
                    <span>Background Export</span>
                    <button className="sqlw-panel-control" onClick={() => setBgExportOpen(false)} title="Close"><X size={13} /></button>
                  </div>
                  <ExportsPanel
                    connectionId={connectionId}
                    sqlText={sqlText}
                    queryName={queryName}
                    autoExportFormat={bgAutoFormat}
                    onAutoExportHandled={() => setBgAutoFormat(null)}
                  />
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {sendToTransformError && (
        <div className="sqlw-banner error small" style={{ margin: '0 0 6px' }}>
          <AlertTriangle size={13} /> {sendToTransformError}
        </div>
      )}

      <div className="sqlw-grid-body">
      {rows.length === 0 ? (
        <div className="sqlw-results-empty">Query returned no rows.</div>
      ) : (
        <div className="sqlw-grid-scroll">
          <table className="sqlw-grid sqlw-grid-interactive">
            <thead>
              <tr>
                <th className="sqlw-grid-selectcol"><input type="checkbox" checked={selected.size > 0 && selected.size === displayIndices.length} onChange={toggleAll} /></th>
                {colOrder.map((c, pos) => (
                  <th
                    key={c}
                    className={frozenFirst && pos === 0 ? 'sqlw-frozen' : ''}
                    style={{ width: colWidths[c], minWidth: colWidths[c] }}
                    draggable
                    onDragStart={(e) => onHeaderDragStart(e, pos)}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => onHeaderDrop(e, pos)}
                    onClick={() => toggleSort(c)}
                    onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); setMenu({ x: e.clientX, y: e.clientY, kind: 'column', colIdx: c }); }}
                  >
                    <span className="sqlw-th-label">
                      {columns[c]}
                      {sortCol === c && (sortDir === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
                    </span>
                    <span className="sqlw-th-resize" onMouseDown={(e) => startResize(e, c)} onClick={(e) => e.stopPropagation()} />
                  </th>
                ))}
              </tr>
              {showFilters && (
                <tr className="sqlw-grid-filterrow">
                  <th className="sqlw-grid-selectcol" />
                  {colOrder.map((c) => (
                    <th key={c} style={{ width: colWidths[c], minWidth: colWidths[c] }}>
                      <input
                        placeholder="Filter…"
                        value={filters[c] || ''}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => setFilters((f) => ({ ...f, [c]: e.target.value }))}
                      />
                    </th>
                  ))}
                </tr>
              )}
            </thead>
            <tbody>
              {displayIndices.map((origIdx) => (
                <tr key={origIdx} className={selected.has(origIdx) ? 'sqlw-row-selected' : ''}>
                  <td className="sqlw-grid-selectcol">
                    <input type="checkbox" checked={selected.has(origIdx)} onChange={() => toggleRow(origIdx)} onClick={(e) => e.stopPropagation()} />
                  </td>
                  {colOrder.map((c, pos) => (
                    <td
                      key={c}
                      className={`${rows[origIdx][c] === null ? 'sqlw-null' : ''} ${frozenFirst && pos === 0 ? 'sqlw-frozen' : ''}`}
                      style={{ width: colWidths[c], minWidth: colWidths[c] }}
                      onDoubleClick={() => setInspecting(origIdx)}
                      onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); setMenu({ x: e.clientX, y: e.clientY, kind: 'cell', rowIdx: origIdx, colIdx: c }); }}
                    >
                      {rows[origIdx][c] === null ? 'NULL' : String(rows[origIdx][c])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {inspecting !== null && (
        <RowInspector
          rowNumber={displayIndices.indexOf(inspecting) + 1}
          columns={colOrder.map((c) => columns[c])}
          row={colOrder.map((c) => rows[inspecting][c])}
          onClose={() => setInspecting(null)}
        />
      )}
      </div>

      {menu && (
        <div className="sqlw-grid-ctxmenu" style={{ left: menu.x, top: menu.y }} onClick={(e) => e.stopPropagation()}>
          {menu.kind === 'cell' && (
            <>
              <button onClick={() => { copyCell(menu.rowIdx, menu.colIdx); setMenu(null); }}>Copy Cell</button>
              <button onClick={() => { copyRow(menu.rowIdx); setMenu(null); }}>Copy Row</button>
              <button onClick={() => { setInspecting(menu.rowIdx); setMenu(null); }}>Inspect Row</button>
              {selected.size > 0 && <button onClick={() => { copySelected(); setMenu(null); }}>Copy Selected Rows ({selected.size})</button>}
              <div className="sqlw-exp-menu-sep" />
              <button onClick={() => { copyColumn(menu.colIdx); setMenu(null); }}>Copy Column</button>
              <button onClick={() => { copyResult(); setMenu(null); }}>Copy Entire Result</button>
            </>
          )}
          {menu.kind === 'column' && (
            <>
              <button onClick={() => { copyColumn(menu.colIdx); setMenu(null); }}>Copy Column</button>
              <button onClick={() => { copyResult(); setMenu(null); }}>Copy Entire Result</button>
            </>
          )}
        </div>
      )}

    </div>
  );
}
