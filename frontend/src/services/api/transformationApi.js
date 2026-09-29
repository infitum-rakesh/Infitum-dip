/**
 * Data Format Transformation Engine API service — wraps /api/transformation.
 * Converts data between JSON/TABLE/CSV/Excel/XML/Parquet in any direction.
 */
import apiClient from './apiClient.js';

export const transformationApi = {
  list: (params = {}) => apiClient.get('/transformation/jobs', { params }).then((r) => r.data),

  create: (body) => apiClient.post('/transformation/jobs', body).then((r) => r.data),

  get: (id) => apiClient.get(`/transformation/jobs/${id}`).then((r) => r.data),

  cancel: (id) => apiClient.post(`/transformation/jobs/${id}/cancel`).then((r) => r.data),

  remove: (id) => apiClient.delete(`/transformation/jobs/${id}`),

  /** Streams the converted file to the browser as a real download —
   * same blob-then-object-URL pattern sqlWorkspaceApi.downloadExport() uses. */
  async download(id, filename) {
    const blob = await apiClient.get(`/transformation/jobs/${id}/download`, { responseType: 'blob' }).then((r) => r.data);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || `conversion_${id}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },
};
