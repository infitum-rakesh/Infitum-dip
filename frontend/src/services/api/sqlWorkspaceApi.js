/**
 * SQL Workspace API service — wraps /api/sql-workspace/* (Phase 9 of
 * the data-integration build). run() executes real SQL — reads AND
 * writes — against a live connection with a real commit/rollback; see
 * backend/app/connectors/database/base.py's run_sql() and
 * backend/app/services/sql_workspace_service.py. Nothing here is mocked.
 */
import apiClient from './apiClient.js';

export const sqlWorkspaceApi = {
  /**
   * scope: "mine" (default — created by the caller, regardless of
   * sharing), "shared" (is_shared=true, org-wide — includes the caller's
   * own shared queries too), or "favorites" (the caller's own favorited
   * queries, any owner). Each item includes real is_shared/is_favorite.
   */
  listQueries: (scope = 'mine') => apiClient.get('/sql-workspace/queries', { params: { scope } }).then((r) => r.data),

  getQuery: (id) => apiClient.get(`/sql-workspace/queries/${id}`).then((r) => r.data),

  /** body: { name, connection_id, schema_name, sql_text, is_shared? } */
  createQuery: (body) => apiClient.post('/sql-workspace/queries', body).then((r) => r.data),

  /** body: { name?, sql_text?, is_shared? } — only the query's owner (or an elevated role) may update/delete it */
  updateQuery: (id, body) => apiClient.put(`/sql-workspace/queries/${id}`, body).then((r) => r.data),

  deleteQuery: (id) => apiClient.delete(`/sql-workspace/queries/${id}`),

  /**
   * Toggles the CALLER's own favorite on a query they can see (their own
   * or a shared one) — a personal bookmark, independent of edit rights.
   * Returns: { is_favorite: boolean }
   */
  toggleFavorite: (id) => apiClient.post(`/sql-workspace/queries/${id}/favorite`).then((r) => r.data),

  /**
   * Executes one SQL statement against the given connection.
   * body: { sql, parameters?, limit?, client_run_id?, timeout_seconds? }
   * client_run_id (a client-generated uuid) lets a concurrent cancelRun()
   * call target this specific in-flight run. timeout_seconds is a REAL
   * server-side cutoff on PostgreSQL/MySQL connections — a no-op (never
   * a fabricated client-side timer) for connection types that don't have
   * it wired; see connectors' supports_query_timeout.
   * Returns: { is_result_set, columns, rows, truncated, rowcount, elapsed_ms }
   */
  run: (connectionId, body) => apiClient.post(`/sql-workspace/connections/${connectionId}/run`, body).then((r) => r.data),

  /**
   * Real cancellation of an in-flight run — a separate HTTP request sent
   * WHILE the /run request above is still pending. Only works for
   * connection types with real cancellation wired (PostgreSQL/MySQL
   * today — see connectors' supports_query_cancel); returns
   * { cancelled: false, message } rather than throwing when it can't
   * (run already finished, unsupported connection type, ...).
   * Returns: { cancelled: boolean, message: string }
   */
  cancelRun: (connectionId, clientRunId) => apiClient.post(`/sql-workspace/connections/${connectionId}/cancel`, { client_run_id: clientRunId }).then((r) => r.data),

  /**
   * Real EXPLAIN via the connection's own dialect (see connectors/database/base.py).
   * body: { sql, parameters? }
   * Returns: { plan_text, columns, rows, elapsed_ms } — plan_text set for
   * line-oriented dialects (Postgres), columns/rows for tabular ones (MySQL).
   * A connection type without a verified EXPLAIN syntax yet returns a
   * clear 400, never a fabricated plan.
   */
  explain: (connectionId, body) => apiClient.post(`/sql-workspace/connections/${connectionId}/explain`, body).then((r) => r.data),

  /**
   * AI Assistant — Natural Language → SQL. Calls whichever AI Provider
   * connection (OpenAI/Anthropic/Ollama) the caller picks, given real
   * schema context introspected from `connectionId` server-side (see
   * backend/app/services/sql_ai_service.py). The AI never executes SQL —
   * this only proposes a statement for the caller to review and
   * explicitly Insert/Run themselves.
   * body: { ai_connection_id, question, schema_name?, current_sql? }
   * Returns: { sql, explanation, warnings, statement_command,
   *            statement_category, model, elapsed_ms }
   */
  generateSql: (connectionId, body) => apiClient.post(`/sql-workspace/connections/${connectionId}/ai/generate-sql`, body).then((r) => r.data),

  /**
   * Real execution history — every entry is a genuine past /run call
   * (see backend/app/services/sql_workspace_service.py's _log_execution),
   * logged on success AND failure. Non-elevated roles only ever see
   * their own history server-side; org admins/super admins default to
   * the whole org and can pass { scope: 'mine' } to narrow it.
   * params: { connection_id?, status?, scope?, page?, page_size? }
   * Returns: { items: [...], total }
   */
  listHistory: (params) => apiClient.get('/sql-workspace/history', { params }).then((r) => r.data),

  /**
   * The caller's own real, computed SQL Workspace permission set (see
   * backend/app/services/role_service.py's effective_sql_permissions) —
   * VIEW/EXECUTE/WRITE/DELETE/EXPORT/AI_ASSISTANT/DML/DDL/
   * PRODUCTION_ACCESS. Used to hide/disable actions the backend would
   * reject anyway — every action is still independently re-checked
   * server-side, this is UI convenience, not the enforcement boundary.
   * Returns: { actions: string[] }
   */
  getPermissions: () => apiClient.get('/sql-workspace/permissions').then((r) => r.data),

  /**
   * Background export — the FULL query result, not bounded by the
   * browser row limit the way Export CSV/JSON/Excel on the Result Grid
   * is (see ResultGrid.jsx's doExport). Streams server-side straight to
   * a real file (backend/app/execution/sql_export_executor.py) and is
   * tracked as a job the caller polls, since a large export can run well
   * past a single request/response cycle.
   * body: { sql, format } — format: 'CSV' | 'JSON' | 'XLSX'
   */
  createExport: (connectionId, body) => apiClient.post(`/sql-workspace/connections/${connectionId}/export`, body).then((r) => r.data),

  listExports: (params) => apiClient.get('/sql-workspace/exports', { params }).then((r) => r.data),

  getExport: (jobId) => apiClient.get(`/sql-workspace/exports/${jobId}`).then((r) => r.data),

  cancelExport: (jobId) => apiClient.post(`/sql-workspace/exports/${jobId}/cancel`).then((r) => r.data),

  /** Fetch + trigger a browser download of a SUCCEEDED export's real file. */
  async downloadExport(jobId, filename) {
    const blob = await apiClient.get(`/sql-workspace/exports/${jobId}/download`, { responseType: 'blob' }).then((r) => r.data);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || `export_${jobId}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },
};
