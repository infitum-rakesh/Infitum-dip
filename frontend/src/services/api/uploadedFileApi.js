/**
 * Uploaded dataset file API — wraps /api/uploaded-files/* (Validation
 * and Reconciliation's "Upload File" object source, an alternative to
 * picking a real DB connection+object). Real upload, real column/row
 * sniffing via a sandboxed LocalFileConnector — see
 * backend/app/services/uploaded_file_service.py. Distinct from
 * pipelineFileApi.js — that one is pipeline-node-scoped (Transform
 * Workspace); this one is generic, reusable by any module that can
 * compare/validate a file instead of a DB object.
 */
import apiClient from './apiClient.js';

export const uploadedFileApi = {
  upload: (file, { onProgress } = {}) => {
    const form = new FormData();
    form.append('file', file);
    return apiClient
      .post('/uploaded-files/upload', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: onProgress
          ? (evt) => onProgress(evt.total ? Math.round((evt.loaded / evt.total) * 100) : 0)
          : undefined,
      })
      .then((r) => r.data);
  },

  get: (fileId) => apiClient.get(`/uploaded-files/${fileId}`).then((r) => r.data),

  getColumns: (fileId) => apiClient.get(`/uploaded-files/${fileId}/columns`).then((r) => r.data),

  remove: (fileId) => apiClient.delete(`/uploaded-files/${fileId}`),
};
