/**
 * SqlWorkspace — /app/sql-workspace
 * Real SQL editor against any connected database. Run executes the exact
 * text via backend/app/connectors/database/base.py's run_sql() — reads
 * AND writes, real commit/rollback (see api/sql_workspace.py). One
 * statement per Run; nothing here is simulated.
 *
 * Multi-tab shell (Phase 1 of the enterprise SQL Workspace build): each
 * tab is a fully independent buffer — its own connection, SQL text, row
 * limit, in-flight/result/error state and active schema. Running Tab 1
 * never touches Tab 2's state; they're separate entries in the `tabs`
 * array below, not a single shared buffer swapped in and out. The schema
 * tree and saved-queries panel in the left sidebar always reflect the
 * ACTIVE tab's connection. Deeper IDE features (Monaco editor, database/
 * object explorer with right-click actions, drag-to-SELECT, AI SQL
 * assistant, query history, background export, row inspector/pivot,
 * server-side pagination) are later phases — see the task tracker.
 */
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import {
  Play, Save, Trash2, Loader2, Clock, AlertTriangle, CheckCircle2,
  Plus, X, MoreVertical, ListTree, Sparkles, History, Star, Users2,
  Minus, Maximize2, Minimize2, CircleDot,
} from 'lucide-react';
import { Button, PermissionGate, EmptyState } from '@/components/common/index.js';
import { SQL_WORKSPACE_PERMISSIONS } from '@/utils/constants.js';
import { useAuth } from '@/hooks/useAuth.js';
import { connectionApi } from '@/services/api/connectionApi.js';
import { sqlWorkspaceApi } from '@/services/api/sqlWorkspaceApi.js';
import { useSqlWorkspaceTabs, makeTab } from '@/context/SqlWorkspaceContext.jsx';
import { pipelineFileApi } from '@/services/api/pipelineFileApi.js';
import { createNode, createEdge } from '@/components/pipeline/nodeTypes.js';
import DatabaseExplorer, { generateSelectSql } from './DatabaseExplorer.jsx';
import SqlEditor, { invalidateEditorMeta } from './SqlEditor.jsx';
import ResultGrid from './ResultGrid.jsx';
import AiAssistantPanel from './AiAssistantPanel.jsx';
import HistoryPanel from './HistoryPanel.jsx';
import { toCsv } from './exportUtils.js';
import './sql-workspace.css';

function apiErrorMessage(err, fallback) { return err?.message || fallback; }

// Mirrors Document Intelligence's StructuredDataset.jsx buildInitialPipelineFromDataset —
// same real upload endpoint, same Source(FILE)+Output Dataset node pair, same
// router-state handoff into Transformation.jsx. The CSV is whatever's already
// in the browser for THIS result set (bounded by the query's row limit, same
// data Export CSV would produce) — no separate large-result path here, see
// exportUtils.js's note on that being a distinct, not-yet-built increment.
async function buildInitialPipelineFromSqlResult(queryName, columns, rows) {
  const safeName = (queryName || 'sql_result').trim() || 'sql_result';
  const csv = toCsv(columns, rows);
  const file = new File([csv], `${safeName}.csv`, { type: 'text/csv' });

  const sourceNode = createNode('source', { x: 60, y: 160 });
  const targetNode = createNode('target', { x: 420, y: 160 });

  const uploaded = await pipelineFileApi.upload(file, sourceNode.id);

  sourceNode.config = { ...sourceNode.config, objectType: 'FILE', uploadedFileId: uploaded.id };
  sourceNode.label = `${safeName} (SQL result)`;
  targetNode.label = 'Write SQL result';

  return {
    name: `SQL Workspace — ${safeName}`,
    nodes: [sourceNode, targetNode],
    edges: [createEdge(sourceNode.id, targetNode.id)],
  };
}

const ELEVATED_ROLES = ['super_admin', 'org_admin'];
const SAVED_SCOPES = [
  { key: 'mine', label: 'My' },
  { key: 'shared', label: 'Shared' },
  { key: 'favorites', label: 'Favorites' },
];

const ROW_LIMITS = [100, 200, 500, 1000];
// Real server-side statement_timeout (Postgres) / MAX_EXECUTION_TIME (MySQL)
// seconds — see connectors' apply_statement_timeout(). '' → null → no limit
// (server clamps any value it does honor to TIMEOUT_MIN/MAX_SECONDS anyway).
const TIMEOUT_OPTIONS = [10, 30, 60, 120, 300];

// makeTab() itself now lives in SqlWorkspaceContext.jsx alongside the
// tabs/activeTabId state it initializes — see that file for why (state
// needs to survive route navigation, not just re-renders).

// ── Results grid ───────────────────────────────────────────────────

function ResultsPanel({ running, error, result, queryName, canExport, onSendToTransform, sendingToTransform, sendToTransformError, connectionId, sqlText, toolbarSlotEl, selectionSlotEl }) {
  if (running) {
    return <div className="sqlw-results-empty"><Loader2 size={16} className="sqlw-spin" /> Running…</div>;
  }
  if (error) {
    return (
      <div className="sqlw-banner error">
        <AlertTriangle size={15} />
        <span>{error}</span>
      </div>
    );
  }
  if (!result) {
    return <EmptyState icon={<Play size={28} />} title="No query run yet" description="Write SQL and hit Run (or Ctrl/Cmd + Enter)." />;
  }
  if (!result.is_result_set) {
    return (
      <div className="sqlw-banner success">
        <CheckCircle2 size={15} />
        <span>
          Statement executed successfully{result.rowcount != null ? ` — ${result.rowcount} row${result.rowcount === 1 ? '' : 's'} affected` : ''}.
          <span className="sqlw-elapsed"> ({result.elapsed_ms} ms)</span>
        </span>
      </div>
    );
  }
  return (
    <>
      {result.truncated && (
        <div className="sqlw-banner warning small" style={{ marginBottom: 6 }}>
          <AlertTriangle size={13} /> Truncated at {result.rows.length} rows — increase the row limit to see more.
        </div>
      )}
      <ResultGrid
        columns={result.columns}
        rows={result.rows}
        queryName={queryName}
        elapsedMs={result.elapsed_ms}
        canExport={canExport}
        onSendToTransform={onSendToTransform}
        sendingToTransform={sendingToTransform}
        sendToTransformError={sendToTransformError}
        connectionId={connectionId}
        sqlText={sqlText}
        toolbarSlotEl={toolbarSlotEl}
        selectionSlotEl={selectionSlotEl}
      />
    </>
  );
}

// ── Explain plan panel ─────────────────────────────────────────────

function ExplainPanel({ explaining, error, result }) {
  if (explaining) {
    return <div className="sqlw-results-empty"><Loader2 size={16} className="sqlw-spin" /> Explaining…</div>;
  }
  if (error) {
    return (
      <div className="sqlw-banner error">
        <AlertTriangle size={15} />
        <span>{error}</span>
      </div>
    );
  }
  if (!result) {
    return <EmptyState icon={<ListTree size={28} />} title="No plan yet" description="Hit Explain to see how the connected database would run this statement." />;
  }
  if (result.plan_text) {
    return (
      <div className="sqlw-grid-wrap">
        <div className="sqlw-grid-meta">
          <span>Execution plan</span>
          <span className="sqlw-elapsed"><Clock size={11} /> {result.elapsed_ms} ms</span>
        </div>
        <pre className="sqlw-plan-text">{result.plan_text}</pre>
      </div>
    );
  }
  return (
    <div className="sqlw-grid-wrap">
      <div className="sqlw-grid-meta">
        <span>Execution plan</span>
        <span className="sqlw-elapsed"><Clock size={11} /> {result.elapsed_ms} ms</span>
      </div>
      <div className="sqlw-grid-scroll">
        <table className="sqlw-grid">
          <thead><tr>{result.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>
            {result.rows.map((row, i) => (
              <tr key={i}>{row.map((cell, j) => <td key={j} className={cell === null ? 'sqlw-null' : ''}>{cell === null ? 'NULL' : String(cell)}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Messages panel — real non-tabular execution messages: the same
// success/error text the Results tab's banner shows, surfaced here too
// so switching to "Messages" always has something real to show rather
// than duplicating (or hiding) what Results already displayed. ────────

function MessagesPanel({ running, error, result }) {
  if (running) {
    return <div className="sqlw-results-empty"><Loader2 size={16} className="sqlw-spin" /> Running…</div>;
  }
  if (error) {
    return (
      <div className="sqlw-messages">
        <div className="sqlw-banner error"><AlertTriangle size={15} /><span>{error}</span></div>
      </div>
    );
  }
  if (!result) {
    return <EmptyState icon={<CircleDot size={28} />} title="No messages yet" description="Run a query to see success/error messages here." />;
  }
  const text = result.is_result_set
    ? `Query returned ${result.rows.length} row${result.rows.length === 1 ? '' : 's'}${result.truncated ? ' (truncated)' : ''} in ${result.elapsed_ms} ms.`
    : `Statement executed successfully${result.rowcount != null ? ` — ${result.rowcount} row${result.rowcount === 1 ? '' : 's'} affected` : ''} in ${result.elapsed_ms} ms.`;
  return (
    <div className="sqlw-messages">
      <div className="sqlw-banner success"><CheckCircle2 size={15} /><span>{text}</span></div>
    </div>
  );
}

// ── Statistics panel — real execution stats, all already-known values
// (nothing computed/estimated) from the same result the Results/Messages
// tabs already show. ───────────────────────────────────────────────────

function StatisticsPanel({ running, error, result, connection }) {
  if (running) {
    return <div className="sqlw-results-empty"><Loader2 size={16} className="sqlw-spin" /> Running…</div>;
  }
  if (!result && !error) {
    return <EmptyState icon={<ListTree size={28} />} title="No statistics yet" description="Run a query to see row/column counts and timing here." />;
  }
  const rows = [
    ['Status', error ? 'Failed' : 'Success'],
    ['Duration', result ? `${result.elapsed_ms} ms` : '—'],
  ];
  if (result?.is_result_set) {
    rows.push(['Rows returned', String(result.rows.length)], ['Columns', String(result.columns.length)]);
    if (result.truncated) rows.push(['Truncated', 'Yes — increase the row limit to see more']);
  } else if (result) {
    rows.push(['Rows affected', result.rowcount != null ? String(result.rowcount) : '—']);
  }
  rows.push(['Connection', connection?.name || '—'], ['Server', connection?.host_display || '—'], ['Database', connection?.database_display || '—']);
  return (
    <div className="sqlw-stats">
      <table className="sqlw-stats-table">
        <tbody>
          {rows.map(([label, value]) => <tr key={label}><th>{label}</th><td>{value}</td></tr>)}
        </tbody>
      </table>
    </div>
  );
}

// ── Footer execution status bar — always visible under the results
// panel once at least one run has happened this tab, real data only. ──

function ExecutionFooter({ running, error, result, connection, selectionSlotRef }) {
  if (running) {
    return (
      <div className="sqlw-exec-footer">
        <span className="sqlw-exec-status running"><Loader2 size={12} className="sqlw-spin" /> Running…</span>
      </div>
    );
  }
  if (!result && !error) return null;
  if (error) {
    return (
      <div className="sqlw-exec-footer">
        <span className="sqlw-exec-status failed"><CircleDot size={10} /> Failed</span>
        <span className="sqlw-exec-footer-error" title={error}>{error}</span>
      </div>
    );
  }
  return (
    <div className="sqlw-exec-footer">
      <span className="sqlw-exec-status success"><CircleDot size={10} /> Success</span>
      {result.is_result_set && <span>Rows: {result.rows.length}{result.truncated ? '+' : ''}</span>}
      {result.is_result_set && <span>Columns: {result.columns.length}</span>}
      {!result.is_result_set && result.rowcount != null && <span>Rows affected: {result.rowcount}</span>}
      <span>Duration: {result.elapsed_ms} ms</span>
      {connection && <span>Server: {connection.name}</span>}
      {connection?.database_display && <span>Database: {connection.database_display}</span>}
      {/* ResultGrid portals "N selected" text in here when rows are checked —
          see ResultGrid.jsx's selectionSlotEl usage. */}
      <span className="sqlw-exec-footer-selection" ref={selectionSlotRef} />
    </div>
  );
}

// ── Tab bar ──────────────────────────────────────────────────────

function TabBar({ tabs, activeTabId, onSelect, onNewTab, onCloseTab, onRename, onDuplicate, onCloseOthers, onCloseAll }) {
  const [renamingTabId, setRenamingTabId] = useState(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [menuTabId, setMenuTabId] = useState(null);
  const [menuCoords, setMenuCoords] = useState(null);

  // The tab bar itself scrolls horizontally (overflow-x: auto) when there
  // are many tabs — an absolutely-positioned dropdown anchored inside it
  // gets clipped by that same overflow, rendering "behind" the tab bar's
  // own bottom edge instead of floating over the page. Rendering the menu
  // through a portal at document.body, positioned from the button's real
  // screen coordinates, escapes that clipping entirely (same pattern as
  // Connections.jsx's row kebab menu).
  const toggleMenu = (e, tabId) => {
    e.stopPropagation();
    if (menuTabId === tabId) { setMenuTabId(null); return; }
    const r = e.currentTarget.getBoundingClientRect();
    const menuWidth = 130;
    setMenuCoords({
      top: r.bottom + 4,
      left: Math.min(r.left, window.innerWidth - menuWidth - 8),
    });
    setMenuTabId(tabId);
  };

  const startRename = (t) => { setRenamingTabId(t.id); setRenameDraft(t.name); };
  const commitRename = (id) => {
    const name = renameDraft.trim();
    if (name) onRename(id, name);
    setRenamingTabId(null);
  };

  return (
    <div className="sqlw-tabbar">
      {tabs.map((t) => (
        <div
          key={t.id}
          className={`sqlw-tab ${t.id === activeTabId ? 'active' : ''}`}
          onClick={() => onSelect(t.id)}
          title={t.name}
        >
          {renamingTabId === t.id ? (
            <input
              autoFocus
              className="sqlw-tab-rename"
              value={renameDraft}
              onChange={(e) => setRenameDraft(e.target.value)}
              onBlur={() => commitRename(t.id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitRename(t.id);
                if (e.key === 'Escape') setRenamingTabId(null);
              }}
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <span className="sqlw-tab-label" onDoubleClick={(e) => { e.stopPropagation(); startRename(t); }}>
              {t.name}
              {t.sqlText !== t.savedSqlText && <span className="sqlw-tab-dirty" title="Unsaved changes">●</span>}
            </span>
          )}

          <div className="sqlw-tab-menu-wrap">
            <button
              className="sqlw-tab-menu-btn"
              onClick={(e) => toggleMenu(e, t.id)}
              title="Tab actions"
            >
              <MoreVertical size={12} />
            </button>
            {menuTabId === t.id && menuCoords && createPortal(
              <>
                <div className="sqlw-tab-menu-backdrop" onClick={() => setMenuTabId(null)} />
                <div
                  className="sqlw-tab-menu"
                  style={{ position: 'fixed', top: menuCoords.top, left: menuCoords.left }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <button onClick={() => { startRename(t); setMenuTabId(null); }}>Rename</button>
                  <button onClick={() => { onDuplicate(t.id); setMenuTabId(null); }}>Duplicate</button>
                  <button onClick={() => { onCloseOthers(t.id); setMenuTabId(null); }}>Close Others</button>
                  <button onClick={() => { onCloseAll(); setMenuTabId(null); }}>Close All</button>
                </div>
              </>,
              document.body
            )}
          </div>

          <button className="sqlw-tab-close" onClick={(e) => { e.stopPropagation(); onCloseTab(t.id); }} title="Close tab">
            <X size={12} />
          </button>
        </div>
      ))}
      <button className="sqlw-tab-add" onClick={onNewTab} title="New Tab (a fresh, independent query buffer)">
        <Plus size={14} />
      </button>
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────

export default function SqlWorkspace() {
  const navigate = useNavigate();
  const [connections, setConnections] = useState([]);
  // Lives in SqlWorkspaceContext (mounted once at the app-shell level, see
  // providers.jsx) instead of component-local state — otherwise navigating
  // to another module and back unmounted/remounted this component and
  // silently threw away every tab's SQL text, in-flight run, and result.
  const { tabs, setTabs, activeTabId, setActiveTabId } = useSqlWorkspaceTabs();

  // Real DOM portal targets — filter/freeze/export live in the result-tabs
  // banner (right-aligned, alongside Results/Messages/Statistics/Execution
  // Plan) instead of their own toolbar row inside the grid; the "N
  // selected" message lives in the bottom execution-status footer instead
  // of that same removed row. ResultGrid still OWNS all the state/logic
  // (filters, freeze, export, selection) — these are just the real DOM
  // nodes it portals its buttons/text into via createPortal, set once each
  // slot mounts (callback refs, so ResultGrid always has a live target by
  // the time it renders, since both slots mount unconditionally above/
  // around it).
  const [gridToolbarEl, setGridToolbarEl] = useState(null);
  const [gridFooterSelectionEl, setGridFooterSelectionEl] = useState(null);

  // Results panel size — a workspace-wide UI preference (not per-tab):
  // 'normal' at a real, user-adjustable height (default 30% of the main
  // column, matching the spec default), 'maximized' at 85%, 'minimized'
  // collapses to just the tab bar. resultsHeightPct is the height used in
  // 'normal' mode — dragging the handle between the editor and the
  // results tabs adjusts it directly, clamped to a sane 15-85% range.
  const [resultsMode, setResultsMode] = useState('normal');
  const [resultsHeightPct, setResultsHeightPct] = useState(30);
  const mainColumnRef = useRef(null);
  const resizeRef = useRef(null);

  const startResultsResize = (e) => {
    e.preventDefault();
    const columnEl = mainColumnRef.current;
    if (!columnEl) return;
    const columnHeight = columnEl.getBoundingClientRect().height;
    // The editor (.sqlw-monaco-wrap) is flex:1 — it fills whatever space
    // the results panel doesn't take, which is what keeps the footer
    // pinned to the bottom of the column instead of drifting as you drag.
    // But the editor also has a real CSS min-height (140px): past that
    // floor it can't shrink further, so a flat 85% cap on the results
    // panel isn't safe on a short viewport — it can demand more room than
    // "column height minus everything else minus the editor's floor"
    // actually has, forcing the whole column to overflow and dragging the
    // footer out of place again. Measuring the fixed "chrome" (toolbar,
    // banners, hint text, resize handle, result tabs) once at drag-start
    // lets the clamp respect that floor for real, so the footer never
    // moves no matter how far the handle is dragged.
    const resultsEl = columnEl.querySelector('.sqlw-results');
    const editorEl = columnEl.querySelector('.sqlw-monaco-wrap');
    const EDITOR_MIN_PX = 140;
    const chromeHeight = columnHeight
      - (resultsEl?.getBoundingClientRect().height ?? 0)
      - (editorEl?.getBoundingClientRect().height ?? 0);
    const maxResultsPx = Math.max(0, columnHeight - chromeHeight - EDITOR_MIN_PX);
    const maxPct = columnHeight > 0 ? Math.min(85, (maxResultsPx / columnHeight) * 100) : 85;
    resizeRef.current = { startY: e.clientY, startPct: resultsHeightPct, columnHeight, maxPct };
    const onMove = (moveEvent) => {
      const ref = resizeRef.current;
      if (!ref || !ref.columnHeight) return;
      const deltaPx = ref.startY - moveEvent.clientY; // dragging UP grows the results panel
      const deltaPct = (deltaPx / ref.columnHeight) * 100;
      setResultsHeightPct(Math.min(ref.maxPct, Math.max(15, ref.startPct + deltaPct)));
    };
    const onUp = () => {
      resizeRef.current = null;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  const { user } = useAuth();
  const isElevated = ELEVATED_ROLES.includes(user?.role);

  const [leftTab, setLeftTab] = useState('schema');
  const [savedScope, setSavedScope] = useState('mine');
  const [savedQueries, setSavedQueries] = useState([]);
  const [loadingSaved, setLoadingSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0);
  // Real, per-user permission set (VIEW/EXECUTE/WRITE/DELETE/EXPORT/
  // AI_ASSISTANT/DML/DDL/PRODUCTION_ACCESS — see role_service.
  // effective_sql_permissions). Drives which actions this UI even offers;
  // every one of them is independently re-checked server-side regardless,
  // so an empty/stale set here only ever hides a button early, never
  // grants anything the backend wouldn't have granted anyway.
  const [permissions, setPermissions] = useState(null);
  const can = (action) => permissions === null || permissions.includes(action); // null = still loading, don't flash "hidden" before the real answer arrives

  useEffect(() => {
    // DATABASE connections all support real SQL execution. Of the
    // APPLICATION-category connections, only NETSUITE_SUITEQL and
    // SALESFORCE genuinely do too (real SuiteQL / SOQL run_sql() — see
    // connectors/database/netsuite_suiteql.py and connectors/application/
    // salesforce.py); NETSUITE_SOAP (saved-search execution, not ad-hoc
    // querying) doesn't, so it's filtered back out here rather than
    // listed and left to fail with an unsupported-operation error at
    // run time.
    Promise.all([
      connectionApi.list({ category: 'DATABASE', page_size: 200 }),
      connectionApi.list({ category: 'APPLICATION', page_size: 200 }),
    ]).then(([db, app]) => {
      const sqlCapableApp = (app.items || []).filter((c) => ['NETSUITE_SUITEQL', 'SALESFORCE'].includes(c.connection_type));
      setConnections([...(db.items || []), ...sqlCapableApp]);
    }).catch(() => {});
    sqlWorkspaceApi.getPermissions().then((r) => setPermissions(r.actions)).catch(() => setPermissions([]));
  }, []);

  // Re-fetch whenever the My/Shared/Favorites sub-tab changes — each is a
  // real, separately-filtered server query (scope param), not a client-
  // side filter over one fetched-once list.
  useEffect(() => { refreshSavedQueries(savedScope); }, [savedScope]); // eslint-disable-line react-hooks/exhaustive-deps

  const refreshSavedQueries = (scope) => {
    setLoadingSaved(true);
    sqlWorkspaceApi.listQueries(scope).then((r) => setSavedQueries(r.items)).catch(() => setSavedQueries([])).finally(() => setLoadingSaved(false));
  };

  const activeTab = tabs.find((t) => t.id === activeTabId) ?? tabs[0];
  const isDirty = (t) => t.sqlText !== t.savedSqlText;

  const patchTab = (id, patch) => setTabs((ts) => ts.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  const patchActiveTab = (patch) => patchTab(activeTabId, patch);

  // ── Tab management ──
  const newTab = () => {
    const t = makeTab();
    setTabs((ts) => [...ts, t]);
    setActiveTabId(t.id);
    setSaveError('');
  };

  const closeTab = (id) => {
    const t = tabs.find((x) => x.id === id);
    if (t && isDirty(t) && !window.confirm(`"${t.name}" has unsaved SQL. Close it anyway?`)) return;
    const idx = tabs.findIndex((x) => x.id === id);
    const remaining = tabs.filter((x) => x.id !== id);
    const next = remaining.length ? remaining : [makeTab()]; // always keep at least one tab open
    setTabs(next);
    if (activeTabId === id) setActiveTabId(next[Math.min(idx, next.length - 1)].id);
  };

  const closeOthers = (id) => {
    const dirtyOthers = tabs.filter((t) => t.id !== id && isDirty(t));
    if (dirtyOthers.length && !window.confirm(`${dirtyOthers.length} other tab${dirtyOthers.length === 1 ? '' : 's'} have unsaved SQL. Close them anyway?`)) return;
    setTabs((ts) => ts.filter((t) => t.id === id));
    setActiveTabId(id);
  };

  const closeAll = () => {
    const anyDirty = tabs.some(isDirty);
    if (anyDirty && !window.confirm('Some tabs have unsaved SQL. Close all tabs anyway?')) return;
    const t = makeTab();
    setTabs([t]);
    setActiveTabId(t.id);
  };

  const renameTab = (id, name) => patchTab(id, { name });

  const duplicateTab = (id) => {
    const src = tabs.find((t) => t.id === id);
    if (!src) return;
    const copy = makeTab({
      name: `${src.name} copy`,
      connectionId: src.connectionId,
      sqlText: src.sqlText,
      savedSqlText: '', // the duplicate is its own unsaved buffer, even if the source was saved
      rowLimit: src.rowLimit,
      activeSchemaName: src.activeSchemaName,
    });
    setTabs((ts) => {
      const idx = ts.findIndex((t) => t.id === id);
      return [...ts.slice(0, idx + 1), copy, ...ts.slice(idx + 1)];
    });
    setActiveTabId(copy.id);
  };

  // ── Editor actions (act on the active tab) ──

  // Core execution primitive — takes explicit (tabId, connectionId, sqlText)
  // rather than reading from `activeTab` so a caller that just generated
  // fresh SQL (e.g. previewObject() below) doesn't have to wait a render
  // cycle for patchTab()'s state update to land before running it.
  const executeSql = async (tabId, connectionId, sqlText, rowLimit, timeoutSeconds) => {
    if (!connectionId || !sqlText.trim()) return;
    // A client-generated id, not a server one — Cancel needs something to
    // target WHILE this request is still pending, before any response
    // (with a server-assigned id) could ever arrive.
    const runId = crypto.randomUUID();
    patchTab(tabId, { running: true, runId, canceling: false, cancelNote: '', error: '', result: null });
    try {
      const r = await sqlWorkspaceApi.run(connectionId, {
        sql: sqlText, limit: rowLimit, client_run_id: runId, timeout_seconds: timeoutSeconds,
      });
      // Any successful statement (not just DDL) may have changed what's in
      // the schema — a write's target table might be new since the tree
      // was last expanded. Bust Monaco autocomplete's table/column cache
      // so it doesn't offer stale suggestions (silent — it just re-fetches
      // next time completion is requested, no visible effect). The
      // DatabaseExplorer tree deliberately does NOT get busted here — see
      // its own comment for why an eager cache-clear there just blanked
      // the visible tree instead of refreshing it. Uses the functional
      // setState form (not patchTab) so this is correct even if the user
      // edited another tab while this one ran.
      invalidateEditorMeta(connectionId);
      setTabs((ts) => ts.map((x) => (x.id === tabId ? { ...x, result: r, running: false, runId: null } : x)));
    } catch (err) {
      // A real cancellation surfaces here too — Postgres/MySQL both raise
      // a genuine DB error when pg_cancel_backend/KILL QUERY interrupts a
      // statement, so it flows through the same real-error path as any
      // other failure, not a separate fabricated "cancelled" state.
      const message = apiErrorMessage(err, 'The statement failed to execute.');
      setTabs((ts) => ts.map((x) => (x.id === tabId ? { ...x, error: message, running: false, runId: null } : x)));
    } finally {
      // Every real /run attempt — success or failure — is logged server-
      // side (sql_workspace_service._log_execution); bump this so an open
      // History panel picks up the new entry without a manual refresh.
      setHistoryRefreshKey((k) => k + 1);
    }
  };

  const runSql = () => {
    const t = activeTab;
    patchTab(t.id, { resultView: 'results' });
    executeSql(t.id, t.connectionId, t.sqlText, t.rowLimit, t.timeoutSeconds);
  };

  // Fires a SEPARATE request while /run above is still pending — only
  // meaningful once the server has actually captured a real session id
  // for this run (see sql_workspace_service.run_sql's on_session_ready),
  // and only genuinely effective for PostgreSQL/MySQL connections today
  // (see connectors' supports_query_cancel). A "couldn't cancel" response
  // is real and honest, not a fabricated success — the run just keeps
  // going, surfaced via cancelNote rather than the main error banner
  // (which would incorrectly imply the run itself already finished).
  const cancelSql = async () => {
    const t = activeTab;
    if (!t.runId || !t.connectionId) return;
    patchTab(t.id, { canceling: true });
    try {
      const r = await sqlWorkspaceApi.cancelRun(t.connectionId, t.runId);
      patchTab(t.id, r.cancelled ? { canceling: false } : { canceling: false, cancelNote: r.message });
    } catch (err) {
      patchTab(t.id, { canceling: false, cancelNote: apiErrorMessage(err, 'Could not send the cancel request.') });
    }
  };

  // Uploads the CURRENT view of the result grid (respecting whatever sort/
  // filter/column-order the user has applied — same scope as Export CSV)
  // as a real file, then hands a ready-to-run Source(FILE)→Output Dataset
  // pipeline to Transformation Workspace via router state. Mirrors
  // Document Intelligence's "Continue to Transformation"
  // (StructuredDataset.jsx) — same real upload endpoint, same node shape,
  // same navigation contract; nothing here is a placeholder link.
  const sendResultToTransform = async (tabId, queryName, columns, rows) => {
    setTabs((ts) => ts.map((x) => (x.id === tabId ? { ...x, sendingToTransform: true, sendToTransformError: '' } : x)));
    try {
      const initialPipeline = await buildInitialPipelineFromSqlResult(queryName, columns, rows);
      navigate('/app/transform', { state: { initialPipeline } });
    } catch (err) {
      const message = apiErrorMessage(err, 'Could not send this result to Transform Workspace.');
      setTabs((ts) => ts.map((x) => (x.id === tabId ? { ...x, sendingToTransform: false, sendToTransformError: message } : x)));
    }
  };

  const explainSql = async () => {
    const t = activeTab;
    if (!t.connectionId || !t.sqlText.trim()) return;
    patchTab(t.id, { resultView: 'explain', explaining: true, explainError: '', explainResult: null });
    try {
      const r = await sqlWorkspaceApi.explain(t.connectionId, { sql: t.sqlText });
      setTabs((ts) => ts.map((x) => (x.id === t.id ? { ...x, explainResult: r, explaining: false } : x)));
    } catch (err) {
      const message = apiErrorMessage(err, 'Could not generate an execution plan for this statement.');
      setTabs((ts) => ts.map((x) => (x.id === t.id ? { ...x, explainError: message, explaining: false } : x)));
    }
  };

  const insertSql = (sql) => {
    const current = activeTab?.sqlText || '';
    if (!current.trim()) {
      patchActiveTab({ sqlText: sql });
    } else {
      patchActiveTab({ sqlText: `${current.trim()}\n\n${sql}` });
    }
  };

  // "Preview Data" from the explorer's context menu — generates a real
  // SELECT from the object's real columns and runs it immediately rather
  // than just inserting it, matching the spec's "Preview Data" action.
  const previewObject = (schemaName, objectName, columns) => {
    const t = activeTab;
    const activeConn = connections.find((c) => c.id === t.connectionId);
    const sql = generateSelectSql(schemaName, objectName, columns, { includeColumns: true, addLimit: true }, activeConn?.connection_type);
    patchTab(t.id, { sqlText: sql, activeSchemaName: schemaName || '' });
    executeSql(t.id, t.connectionId, sql, t.rowLimit);
  };

  const loadSavedQuery = (q) => {
    patchActiveTab({
      activeQueryId: q.id,
      name: q.name,
      sqlText: q.sql_text,
      savedSqlText: q.sql_text,
      isShared: !!q.is_shared,
      activeSchemaName: q.schema_name || '',
      connectionId: q.connection_id,
      result: null, error: '',
    });
    setSaveError('');
  };

  // Loads a History entry's connection + SQL text back into the active
  // tab — deliberately does NOT re-run it, same human-in-the-loop
  // principle as the AI Assistant's "Insert into Editor" (see
  // HistoryPanel.jsx docstring).
  const loadHistoryEntry = (entry) => {
    patchActiveTab({
      connectionId: entry.connection_id,
      sqlText: entry.sql_text,
      activeQueryId: null,
      result: null, error: '', resultView: 'results',
    });
  };

  const saveQuery = async () => {
    const t = activeTab;
    if (!t.connectionId) { setSaveError('Select a connection first.'); return; }
    setSaving(true); setSaveError('');
    try {
      if (t.activeQueryId) {
        await sqlWorkspaceApi.updateQuery(t.activeQueryId, { name: t.name, sql_text: t.sqlText, is_shared: t.isShared });
        patchTab(t.id, { savedSqlText: t.sqlText });
      } else {
        const r = await sqlWorkspaceApi.createQuery({
          name: t.name, connection_id: t.connectionId, schema_name: t.activeSchemaName || null,
          sql_text: t.sqlText, is_shared: t.isShared,
        });
        patchTab(t.id, { activeQueryId: r.id, savedSqlText: t.sqlText });
      }
      // Real refetch (not a client-side splice) — a save can change which
      // scope(s) this query now belongs in (e.g. just made it Shared).
      refreshSavedQueries(savedScope);
    } catch (err) {
      setSaveError(apiErrorMessage(err, 'Could not save this query.'));
    } finally {
      setSaving(false);
    }
  };

  const deleteQuery = async (id, e) => {
    e.stopPropagation();
    if (!window.confirm('Delete this saved query?')) return;
    try {
      await sqlWorkspaceApi.deleteQuery(id);
      setSavedQueries((qs) => qs.filter((q) => q.id !== id));
      setTabs((ts) => ts.map((t) => (t.activeQueryId === id ? { ...t, activeQueryId: null } : t)));
    } catch (err) {
      setSaveError(apiErrorMessage(err, 'Could not delete this query.'));
    }
  };

  const toggleQueryFavorite = async (id, e) => {
    e.stopPropagation();
    try {
      const r = await sqlWorkspaceApi.toggleFavorite(id);
      if (savedScope === 'favorites' && !r.is_favorite) {
        setSavedQueries((qs) => qs.filter((q) => q.id !== id)); // unfavorited while viewing Favorites — no longer belongs here
      } else {
        setSavedQueries((qs) => qs.map((q) => (q.id === id ? { ...q, is_favorite: r.is_favorite } : q)));
      }
    } catch { /* leave as-is; user can retry */ }
  };

  const selectedConnection = connections.find((c) => c.id === activeTab.connectionId);

  return (
    <PermissionGate {...SQL_WORKSPACE_PERMISSIONS.SQL_VIEW} showReason>
      <div className="sqlw-page">
        <div className="sqlw-header">
          <h1>SQL Workspace</h1>
          <p className="sqlw-subtitle">Write, execute and save SQL against any connected database — real commits, real rollbacks.</p>
        </div>

        <TabBar
          tabs={tabs}
          activeTabId={activeTabId}
          onSelect={setActiveTabId}
          onNewTab={newTab}
          onCloseTab={closeTab}
          onRename={renameTab}
          onDuplicate={duplicateTab}
          onCloseOthers={closeOthers}
          onCloseAll={closeAll}
        />

        <div className="sqlw-layout">
          {/* ── Sidebar ── */}
          <aside className="sqlw-sidebar">
            <div className="sqlw-sidebar-tabs">
              <button className={leftTab === 'schema' ? 'active' : ''} onClick={() => setLeftTab('schema')}>Schema</button>
              <button className={leftTab === 'saved' ? 'active' : ''} onClick={() => setLeftTab('saved')}>
                Saved {savedQueries.length > 0 && <span className="sqlw-count">{savedQueries.length}</span>}
              </button>
              {can('AI_ASSISTANT') && (
                <button className={leftTab === 'ai' ? 'active' : ''} onClick={() => setLeftTab('ai')}>
                  <Sparkles size={12} /> AI
                </button>
              )}
              <button className={leftTab === 'history' ? 'active' : ''} onClick={() => setLeftTab('history')}>
                <History size={12} /> History
              </button>
            </div>
            {leftTab === 'schema' && (
              <DatabaseExplorer
                connectionId={activeTab.connectionId}
                connectionType={selectedConnection?.connection_type}
                onInsertSql={insertSql}
                onPreviewObject={previewObject}
              />
            )}
            {leftTab === 'saved' && (
              <div className="sqlw-saved-wrap">
                <div className="sqlw-saved-scopes">
                  {SAVED_SCOPES.map((s) => (
                    <button key={s.key} className={savedScope === s.key ? 'active' : ''} onClick={() => setSavedScope(s.key)}>
                      {s.label}
                    </button>
                  ))}
                </div>
                <div className="sqlw-saved-list">
                  {loadingSaved && <div className="sqlw-tree-empty"><Loader2 size={14} className="sqlw-spin" /> Loading…</div>}
                  {!loadingSaved && savedQueries.length === 0 && (
                    <div className="sqlw-tree-empty">
                      {savedScope === 'mine' && 'No saved queries yet.'}
                      {savedScope === 'shared' && 'No queries shared with the organization yet.'}
                      {savedScope === 'favorites' && 'No favorites yet — star a query to pin it here.'}
                    </div>
                  )}
                  {!loadingSaved && savedQueries.map((q) => {
                    const isOwner = q.created_by === user?.id;
                    return (
                      <button key={q.id} className={`sqlw-saved-row ${activeTab.activeQueryId === q.id ? 'active' : ''}`} onClick={() => loadSavedQuery(q)}>
                        <Star
                          size={13}
                          className={`sqlw-saved-fav ${q.is_favorite ? 'active' : ''}`}
                          fill={q.is_favorite ? 'currentColor' : 'none'}
                          onClick={(e) => toggleQueryFavorite(q.id, e)}
                          title={q.is_favorite ? 'Remove from favorites' : 'Add to favorites'}
                        />
                        <span className="sqlw-saved-name">{q.name}</span>
                        {q.is_shared && <Users2 size={12} className="sqlw-saved-shared" title={`Shared by ${q.created_by_name || 'a teammate'}`} />}
                        {can('DELETE') && (isOwner || isElevated) && (
                          <Trash2 size={13} className="sqlw-saved-delete" onClick={(e) => deleteQuery(q.id, e)} />
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
            {leftTab === 'ai' && can('AI_ASSISTANT') && (
              <AiAssistantPanel
                connectionId={activeTab.connectionId}
                schemaName={activeTab.activeSchemaName}
                currentSql={activeTab.sqlText}
                onInsertSql={insertSql}
              />
            )}
            {leftTab === 'history' && (
              <HistoryPanel refreshKey={historyRefreshKey} onLoadEntry={loadHistoryEntry} />
            )}
          </aside>

          {/* ── Main ── */}
          <main className="sqlw-main" ref={mainColumnRef}>
            <div className="sqlw-toolbar">
              <input className="sqlw-name-input" value={activeTab.name} onChange={(e) => patchActiveTab({ name: e.target.value })} />
              <select className="sqlw-select" value={activeTab.connectionId ?? ''} onChange={(e) => patchActiveTab({ connectionId: e.target.value ? Number(e.target.value) : null })}>
                <option value="">Select connection…</option>
                {connections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.connection_type})</option>)}
              </select>
              <select className="sqlw-select sqlw-select-narrow" value={activeTab.rowLimit} onChange={(e) => patchActiveTab({ rowLimit: Number(e.target.value) })} title="Max rows returned">
                {ROW_LIMITS.map((n) => <option key={n} value={n}>{n} rows</option>)}
              </select>
              <select
                className="sqlw-select sqlw-select-narrow"
                value={activeTab.timeoutSeconds ?? ''}
                onChange={(e) => patchActiveTab({ timeoutSeconds: e.target.value ? Number(e.target.value) : null })}
                title="Server-side statement timeout — real on PostgreSQL/MySQL connections, no-op on others"
              >
                {TIMEOUT_OPTIONS.map((n) => <option key={n} value={n}>{n}s timeout</option>)}
                <option value="">No limit</option>
              </select>
              <div className="sqlw-toolbar-spacer" />
              {can('WRITE') && (
                <label className="sqlw-share-toggle" title="Visible to the whole organization in the Shared tab">
                  <input type="checkbox" checked={activeTab.isShared} onChange={(e) => patchActiveTab({ isShared: e.target.checked })} />
                  <Users2 size={12} /> Share
                </label>
              )}
              {can('WRITE') && (
                <Button variant="secondary" size="sm" icon={<Save size={14} />} loading={saving} onClick={saveQuery}>Save</Button>
              )}
              <PermissionGate {...SQL_WORKSPACE_PERMISSIONS.SQL_RUN} fallback={null}>
                <Button variant="secondary" size="sm" icon={<ListTree size={14} />} loading={activeTab.explaining} disabled={!activeTab.connectionId || !activeTab.sqlText.trim()} onClick={explainSql}>
                  Explain
                </Button>
                {activeTab.running ? (
                  <Button variant="danger" size="sm" icon={<X size={14} />} loading={activeTab.canceling} onClick={cancelSql}>
                    Cancel
                  </Button>
                ) : (
                  <Button variant="primary" size="sm" icon={<Play size={14} />} loading={activeTab.running} disabled={!activeTab.connectionId || !activeTab.sqlText.trim()} onClick={runSql}>
                    Run
                  </Button>
                )}
              </PermissionGate>
            </div>
            {saveError && <div className="sqlw-banner error small"><AlertTriangle size={13} /> {saveError}</div>}
            {selectedConnection?.status === 'UNSUPPORTED' && (
              <div className="sqlw-banner warning small"><AlertTriangle size={13} /> This connection type doesn't support live execution yet.</div>
            )}
            {/* A failed/declined cancel attempt — the run itself is still going, so this is
                deliberately separate from the error banner (which implies the run ended). */}
            {activeTab.cancelNote && (
              <div className="sqlw-banner warning small"><Clock size={13} /> {activeTab.cancelNote}</div>
            )}

            <SqlEditor
              key={activeTab.id}
              value={activeTab.sqlText}
              onChange={(sql) => patchActiveTab({ sqlText: sql })}
              onRun={runSql}
              onSave={saveQuery}
              connectionId={activeTab.connectionId}
            />
            <div className="sqlw-editor-hint">One statement at a time · Ctrl/Cmd + Enter to run · Ctrl/Cmd + S to save · reads and writes both commit for real · each tab is an independent buffer · drag a table in to generate a SELECT</div>

            {/* Drag handle — grabbing it and moving up/down resizes the
                results panel below (15-85% of the main column's height);
                only meaningful in 'normal' mode, hidden while minimized/
                maximized since those are fixed sizes. */}
            {resultsMode === 'normal' && <div className="sqlw-results-resize-handle" onMouseDown={startResultsResize} />}
            <div
              className={`sqlw-result-tabs ${resultsMode === 'minimized' ? 'sqlw-result-tabs-minimized' : ''}`}
              onDoubleClick={() => setResultsMode((m) => (m === 'minimized' ? 'normal' : 'minimized'))}
            >
              <button className={activeTab.resultView === 'results' ? 'active' : ''} onClick={() => { patchActiveTab({ resultView: 'results' }); if (resultsMode === 'minimized') setResultsMode('normal'); }}>Results</button>
              <button className={activeTab.resultView === 'messages' ? 'active' : ''} onClick={() => { patchActiveTab({ resultView: 'messages' }); if (resultsMode === 'minimized') setResultsMode('normal'); }}>Messages</button>
              <button className={activeTab.resultView === 'statistics' ? 'active' : ''} onClick={() => { patchActiveTab({ resultView: 'statistics' }); if (resultsMode === 'minimized') setResultsMode('normal'); }}>Statistics</button>
              <button className={activeTab.resultView === 'explain' ? 'active' : ''} onClick={() => { patchActiveTab({ resultView: 'explain' }); if (resultsMode === 'minimized') setResultsMode('normal'); }}>Execution Plan</button>
              <div className="sqlw-result-tabs-spacer" />
              {/* Filter/Freeze/Export buttons portal in here from ResultGrid
                  (only meaningful — and only rendered by ResultGrid — while
                  the Results tab is showing an actual grid). */}
              {activeTab.resultView === 'results' && (
                <div className="sqlw-grid-toolbar-slot" ref={setGridToolbarEl} />
              )}
              <div className="sqlw-panel-controls">
                <button
                  className="sqlw-panel-control"
                  title={resultsMode === 'minimized' ? 'Restore' : 'Minimize'}
                  onClick={() => setResultsMode((m) => (m === 'minimized' ? 'normal' : 'minimized'))}
                >
                  <Minus size={13} />
                </button>
                <button
                  className="sqlw-panel-control"
                  title={resultsMode === 'maximized' ? 'Restore' : 'Maximize'}
                  onClick={() => setResultsMode((m) => (m === 'maximized' ? 'normal' : 'maximized'))}
                >
                  {resultsMode === 'maximized' ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
                </button>
              </div>
            </div>
            {resultsMode !== 'minimized' && (
              <div className="sqlw-results" style={{ flexBasis: `${resultsMode === 'maximized' ? 85 : resultsHeightPct}%` }}>
                <div className="sqlw-results-scroll">
                  {activeTab.resultView === 'explain' ? (
                    <ExplainPanel explaining={activeTab.explaining} error={activeTab.explainError} result={activeTab.explainResult} />
                  ) : activeTab.resultView === 'messages' ? (
                    <MessagesPanel running={activeTab.running} error={activeTab.error} result={activeTab.result} />
                  ) : activeTab.resultView === 'statistics' ? (
                    <StatisticsPanel running={activeTab.running} error={activeTab.error} result={activeTab.result} connection={selectedConnection} />
                  ) : (
                    <ResultsPanel
                      running={activeTab.running}
                      error={activeTab.error}
                      result={activeTab.result}
                      queryName={activeTab.name}
                      canExport={can('EXPORT')}
                      onSendToTransform={(cols, rows) => sendResultToTransform(activeTab.id, activeTab.name, cols, rows)}
                      sendingToTransform={activeTab.sendingToTransform}
                      sendToTransformError={activeTab.sendToTransformError}
                      connectionId={activeTab.connectionId}
                      sqlText={activeTab.sqlText}
                      toolbarSlotEl={gridToolbarEl}
                      selectionSlotEl={gridFooterSelectionEl}
                    />
                  )}
                </div>
                {activeTab.resultView === 'results' && (
                  <ExecutionFooter running={activeTab.running} error={activeTab.error} result={activeTab.result} connection={selectedConnection} selectionSlotRef={setGridFooterSelectionEl} />
                )}
              </div>
            )}
          </main>
        </div>
      </div>
    </PermissionGate>
  );
}
