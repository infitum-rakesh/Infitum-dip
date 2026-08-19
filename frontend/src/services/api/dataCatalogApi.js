/**
 * Data Catalog API service — wraps all /api/data-catalog/* endpoints.
 * Components must never call apiClient directly; go through this module.
 */
import apiClient from './apiClient.js';

/** Strips undefined/''/'All' values so they don't pollute the query string */
function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

export const dataCatalogApi = {
  /** Header KPIs: total assets, datasets, tables, files, quality score, recent assets */
  getStats: () =>
    apiClient.get('/data-catalog/stats').then((r) => r.data),

  /** Distinct values for every filter dropdown */
  getFacets: () =>
    apiClient.get('/data-catalog/facets').then((r) => r.data),

  /**
   * Paginated, searchable, filterable asset list.
   * params: { q, source_type, environment, database, schema, asset_type,
   *           owner, status, quality, tag, source, page, page_size, sort_by, sort_dir }
   */
  listAssets: (params = {}) =>
    apiClient.get('/data-catalog/assets', { params: cleanParams(params) }).then((r) => r.data),

  getAsset: (id) =>
    apiClient.get(`/data-catalog/assets/${id}`).then((r) => r.data),

  getColumns: (id) =>
    apiClient.get(`/data-catalog/assets/${id}/columns`).then((r) => r.data),

  getProfile: (id) =>
    apiClient.get(`/data-catalog/assets/${id}/profile`).then((r) => r.data),

  getLineage: (id) =>
    apiClient.get(`/data-catalog/assets/${id}/lineage`).then((r) => r.data),

  getQuality: (id) =>
    apiClient.get(`/data-catalog/assets/${id}/quality`).then((r) => r.data),

  getIncidents: (id) =>
    apiClient.get(`/data-catalog/assets/${id}/incidents`).then((r) => r.data),

  getUsage: (id) =>
    apiClient.get(`/data-catalog/assets/${id}/usage`).then((r) => r.data),
};
