/**
 * Mapping API service — wraps /api/mappings/* (Object & Column Mapping,
 * Phase 4 of the data-integration build; real immutable versioning +
 * publish lifecycle added by the Column Mapping <-> Pipeline Builder
 * integration build, CM2/CM3). Auto Map / Validate / Preview operate on
 * an in-progress definition (source/target/mappings passed in the
 * request body) and work before anything is ever saved — only
 * create/update actually persist a ColumnMapping row.
 *
 * IMPORTANT — editing an EXISTING mapping's rows never goes through
 * `update()` anymore (that's header-only: name). It creates a new
 * version via `createVersion()` instead — mirrors how Pipeline Builder's
 * own Save always creates a new PipelineVersion rather than overwriting.
 * See ObjectColumnMapping.jsx's runSave().
 */
import apiClient from './apiClient.js';

export const mappingApi = {
  /** params: { status? } — pass { status: 'PUBLISHED' } for the Pipeline
   * Builder "Select Existing Mapping" picker, which must only ever offer
   * mappings that actually have a published version. */
  list: (params) => apiClient.get('/mappings', { params }).then((r) => r.data),

  get: (id) => apiClient.get(`/mappings/${id}`).then((r) => r.data),

  /** body: { name, source: {connection_id, schema_name, object_name}, target: {...}, mappings: [] } */
  create: (body) => apiClient.post('/mappings', body).then((r) => r.data),

  /** Header-only edit — body: { name? }. Never touches mapping rows. */
  update: (id, body) => apiClient.put(`/mappings/${id}`, body).then((r) => r.data),

  remove: (id) => apiClient.delete(`/mappings/${id}`),

  /** body: { source, target, existing_mappings } -> { proposals, ai_assisted_available } */
  autoMap: (body) => apiClient.post('/mappings/auto-map', body).then((r) => r.data),

  /** body: { source, target, mappings } -> { issues, row_status, overall_status } — pre-save check on an in-progress definition */
  validate: (body) => apiClient.post('/mappings/validate', body).then((r) => r.data),

  /** body: { source, mappings, limit } -> { columns, rows, row_count, truncated } — runs real SQL */
  preview: (body) => apiClient.post('/mappings/preview', body).then((r) => r.data),

  // ── Versioning / publish lifecycle ──────────────────────────────

  listVersions: (id, params) => apiClient.get(`/mappings/${id}/versions`, { params }).then((r) => r.data),

  getVersion: (id, versionNumber) => apiClient.get(`/mappings/${id}/versions/${versionNumber}`).then((r) => r.data),

  /** body: { mappings, change_description?, unmapped_column_policy?, batch_size?, on_conversion_failure? } — always creates a NEW version, DRAFT status. */
  createVersion: (id, body) => apiClient.post(`/mappings/${id}/versions`, body).then((r) => r.data),

  /** Validates against LIVE re-fetched schema and persists the result on the version — -> { issues, row_status, overall_status } */
  validateVersion: (id, versionNumber) => apiClient.post(`/mappings/${id}/versions/${versionNumber}/validate`).then((r) => r.data),

  /** Requires the version to already be VALIDATED (no blocking errors). */
  publishVersion: (id, versionNumber, body = {}) => apiClient.post(`/mappings/${id}/versions/${versionNumber}/publish`, body).then((r) => r.data),

  /** body: { confirm? } — confirm=true required if the version is still attached to any pipeline. */
  retireVersion: (id, versionNumber, body = {}) => apiClient.post(`/mappings/${id}/versions/${versionNumber}/retire`, body).then((r) => r.data),

  /** -> { items: [{pipeline_id, pipeline_name, pipeline_version, node_id, mapping_version, ...}], total } — de-duplicated to latest per pipeline */
  getUsage: (id) => apiClient.get(`/mappings/${id}/usage`).then((r) => r.data),

  /** -> { stale, issues, source_column_count_then/now, target_column_count_then/now } */
  getStaleness: (id, versionNumber) => apiClient.get(`/mappings/${id}/staleness`, { params: { version: versionNumber } }).then((r) => r.data),
};
