/**
 * Connection API service — wraps all /api/connections/* endpoints.
 * Components must never call apiClient directly; go through this module.
 * Secret field values never round-trip through here in plaintext — the
 * backend masks them in every response (see connection_service.py).
 */
import apiClient from './apiClient.js';

/** Strips undefined/''/'All' values so they don't pollute the query string */
function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const connectionApi = {
  /** Header KPIs: total, active, inactive, errors, recently tested */
  getStats: () => apiClient.get('/connections/stats').then((r) => r.data),

  /** Field-schema catalogue (categories + per-type field definitions) —
   * single source of truth the New Connection wizard and dynamic form
   * render from. Fetch once and cache; it doesn't change per-request. */
  getTypes: () => apiClient.get('/connections/types').then((r) => r.data),

  /**
   * Paginated, searchable, filterable connection list.
   * params: { q, category, connection_type, status, environment,
   *           page, page_size, sort_by, sort_dir }
   */
  list: (params = {}) => apiClient.get('/connections', { params: cleanParams(params) }).then((r) => r.data),

  get: (id) => apiClient.get(`/connections/${id}`).then((r) => r.data),

  /** config may include plaintext secret values for fields not yet set — encrypted server-side. */
  create: (body) => apiClient.post('/connections', body).then((r) => r.data),

  /** Only include secret keys in `config` when the user actually typed a new value (rotation). */
  update: (id, body) => apiClient.put(`/connections/${id}`, body).then((r) => r.data),

  remove: (id) => apiClient.delete(`/connections/${id}`),

  test: (id) => apiClient.post(`/connections/${id}/test`).then((r) => r.data),

  /** Toggles disabled <-> re-enabled. */
  toggleDisabled: (id) => apiClient.post(`/connections/${id}/disable`).then((r) => r.data),

  clone: (id) => apiClient.post(`/connections/${id}/clone`).then((r) => r.data),

  // ── Schema/object browsing — Source Dataset node ─────────────────
  // All hit the live source. A 501 means this connection type has no
  // working connector yet (see connectors/registry.py UNVERIFIED) —
  // callers should surface that message as-is, not retry or hide it.
  //
  // BROWSE_TIMEOUT overrides apiClient's default 30s Axios timeout for
  // these five calls only. The backend itself imposes no timeout on
  // list_schemas/list_objects/get_columns/preview (see
  // connection_service.py — each just awaits asyncio.to_thread(...) with
  // no wait_for wrapped around it), so for a local Postgres/MySQL/SQL
  // Server 30s was already generous — but NetSuite's SuiteAnalytics
  // Connect ODBC driver (and, similarly, Salesforce/REST/SOAP) is a real
  // cloud round trip that can legitimately take well over 30s, especially
  // cursor.tables() scanning every standard + custom record type on the
  // first schema load. Without this override, Axios's client-side timeout
  // fires before NetSuite ever answers and apiClient.js's normaliseError()
  // reports it as "No response from server. Check your connection." —
  // indistinguishable in the UI from an actual network failure, even
  // though the backend was still working. Found live against a real
  // NetSuite account. 120s is generous without being unbounded, matching
  // the upper end of SQL Workspace's own configurable execution timeout.
  BROWSE_TIMEOUT: 120_000,

  getSchemas: (id) => apiClient.get(`/connections/${id}/schemas`, { timeout: connectionApi.BROWSE_TIMEOUT }).then((r) => r.data),

  /** params: { schema, q } */
  getObjects: (id, params = {}) =>
    apiClient.get(`/connections/${id}/objects`, { params: cleanParams(params), timeout: connectionApi.BROWSE_TIMEOUT }).then((r) => r.data),

  /** params: { schema, include_row_count } */
  getColumns: (id, objectName, params = {}) =>
    apiClient.get(`/connections/${id}/objects/${encodeURIComponent(objectName)}/columns`, { params: cleanParams(params), timeout: connectionApi.BROWSE_TIMEOUT }).then((r) => r.data),

  /** body: { schema_name, object_name, limit } OR { sql, parameters, limit } */
  preview: (id, body) => apiClient.post(`/connections/${id}/preview`, body, { timeout: connectionApi.BROWSE_TIMEOUT }).then((r) => r.data),

  /** Real REST API preview — body: { endpoint, method, query_params, headers, json_path, pagination, limit }.
   * See backend/app/connectors/api/rest.py — genuinely paginates. */
  previewApi: (id, body) => apiClient.post(`/connections/${id}/preview-api`, body, { timeout: connectionApi.BROWSE_TIMEOUT }).then((r) => r.data),

  // ── AI connection picker (AIC-P8) ──────────────────────────────────
  // Every "pick an AI connection to use" dropdown (AI Extraction stage,
  // Run AI Extraction control, SQL Workspace AI Assistant) must call
  // this instead of the plain list() — it's the only one that includes
  // seller-managed connections this user is actually entitled to use.

  listUsableAiConnections: () => apiClient.get('/connections/ai-usable').then((r) => r.data),

  // ── AI Connection access management (AIC-P5) ──────────────────────
  // Org-admin self-service: who in my organization can see/use which AI
  // connection, and (for connections my org owns) whether it's public.

  listAiAccess: () => apiClient.get('/connections/ai-access').then((r) => r.data),

  getAiAccessGrantees: () => apiClient.get('/connections/ai-access/grantees').then((r) => r.data),

  setAiAccessPublic: (id, isPublic) =>
    apiClient.put(`/connections/${id}/ai-access/public`, { is_public: isPublic }).then((r) => r.data),

  /** body: { grantee_type: 'USER'|'ROLE', grantee_id, can_view, can_use } */
  createAiAccessGrant: (id, body) => apiClient.post(`/connections/${id}/ai-access/grants`, body).then((r) => r.data),

  deleteAiAccessGrant: (id, grantId) => apiClient.delete(`/connections/${id}/ai-access/grants/${grantId}`),

  // ── Default AI connection selection (AIC-P6) ──────────────────────

  getAiDefaults: () => apiClient.get('/connections/ai-defaults').then((r) => r.data),

  setMyAiDefault: (connectionId) => apiClient.put('/connections/ai-defaults/mine', { connection_id: connectionId }).then((r) => r.data),

  clearMyAiDefault: () => apiClient.delete('/connections/ai-defaults/mine').then((r) => r.data),

  setOrgAiDefault: (connectionId) => apiClient.put('/connections/ai-defaults/org', { connection_id: connectionId }).then((r) => r.data),

  clearOrgAiDefault: () => apiClient.delete('/connections/ai-defaults/org').then((r) => r.data),
};
