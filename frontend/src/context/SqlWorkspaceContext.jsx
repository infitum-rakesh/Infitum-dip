/**
 * SqlWorkspaceContext — keeps SQL Workspace's tab state (SQL text, active
 * connection, in-flight/result/error per tab) alive across route
 * navigation, not just across re-renders.
 *
 * Found live: SqlWorkspace.jsx used to hold `tabs`/`activeTabId` in its
 * own component-local useState. React Router unmounts a page component
 * the moment you navigate away from its route — so leaving SQL Workspace
 * mid-query (or with unsaved SQL typed) and coming back rebuilt the
 * component from scratch with a single blank "Untitled Query" tab. The
 * REAL query execution was never actually interrupted (nothing in this
 * file ever aborted the in-flight run on unmount — the backend request
 * runs to completion regardless of whether the page is mounted); the bug
 * was purely that the frontend had nowhere to land the result once it
 * came back, because the state holding it had already been destroyed.
 *
 * Fix: hoist just the tabs array + which one is active into a provider
 * mounted once at the app-shell level (see providers.jsx), so it never
 * unmounts as long as the browser tab itself is open. SqlWorkspace.jsx's
 * pending `await sqlWorkspaceApi.run(...)` calls setTabs against this
 * context (a stable reference, not tied to the page component's
 * lifecycle) — so a query that finishes while you're on another page
 * still lands its result here, and reopening SQL Workspace shows it
 * immediately, exactly as if you'd never left.
 */
import { createContext, useContext, useState } from 'react';

const SqlWorkspaceContext = createContext(null);

let _nextTabId = 1;

export function makeTab(overrides = {}) {
  return {
    id: _nextTabId++,
    name: 'Untitled Query',
    connectionId: null,
    sqlText: '',
    savedSqlText: '',      // text as of last save/load — dirty = sqlText !== savedSqlText
    rowLimit: 200,
    timeoutSeconds: 30,     // real server-side cutoff on PostgreSQL/MySQL; null = no limit
    runId: null,            // set while `running` — lets Cancel target this specific in-flight run
    running: false,
    canceling: false,
    result: null,
    error: '',
    activeQueryId: null,
    isShared: false,        // shares this saved query org-wide once saved — see Saved panel's "Shared" tab
    cancelNote: '',         // transient message when a Cancel attempt itself couldn't be honored (query already finished, unsupported connection type, ...) — the run keeps going
    sendingToTransform: false,
    sendToTransformError: '',
    activeSchemaName: '',
    resultView: 'results',   // 'results' | 'messages' | 'statistics' | 'explain'
    explaining: false,
    explainResult: null,
    explainError: '',
    ...overrides,
  };
}

export function SqlWorkspaceProvider({ children }) {
  const [tabs, setTabs] = useState(() => [makeTab()]);
  const [activeTabId, setActiveTabId] = useState(() => tabs[0].id);

  return (
    <SqlWorkspaceContext.Provider value={{ tabs, setTabs, activeTabId, setActiveTabId }}>
      {children}
    </SqlWorkspaceContext.Provider>
  );
}

export function useSqlWorkspaceTabs() {
  const ctx = useContext(SqlWorkspaceContext);
  if (!ctx) throw new Error('useSqlWorkspaceTabs must be used inside <SqlWorkspaceProvider>');
  return ctx;
}
