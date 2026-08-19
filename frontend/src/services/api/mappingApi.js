/**
 * Mapping API service — wraps /api/mappings/* (Object & Column Mapping,
 * Phase 4 of the data-integration build). Auto Map / Validate / Preview
 * operate on an in-progress definition (source/target/mappings passed in
 * the request body) and work before anything is ever saved — only
 * create/update actually persist a ColumnMapping row.
 */
import apiClient from './apiClient.js';

export const mappingApi = {
  list: () => apiClient.get('/mappings').then((r) => r.data),

  get: (id) => apiClient.get(`/mappings/${id}`).then((r) => r.data),

  /** body: { name, source: {connection_id, schema_name, object_name}, target: {...}, mappings: [] } */
  create: (body) => apiClient.post('/mappings', body).then((r) => r.data),

  /** body: { name?, mappings? } */
  update: (id, body) => apiClient.put(`/mappings/${id}`, body).then((r) => r.data),

  remove: (id) => apiClient.delete(`/mappings/${id}`),

  /** body: { source, target, existing_mappings } -> { proposals, ai_assisted_available } */
  autoMap: (body) => apiClient.post('/mappings/auto-map', body).then((r) => r.data),

  /** body: { source, target, mappings } -> { issues, row_status, overall_status } */
  validate: (body) => apiClient.post('/mappings/validate', body).then((r) => r.data),

  /** body: { source, mappings, limit } -> { columns, rows, row_count, truncated } — runs real SQL */
  preview: (body) => apiClient.post('/mappings/preview', body).then((r) => r.data),
};
