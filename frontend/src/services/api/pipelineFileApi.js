/**
 * Pipeline source file API — wraps /api/pipeline-files/* (Transform
 * Workspace Source Dataset "Upload File" option). Real upload, real
 * delimiter/sheet detection — see backend/app/services/pipeline_file_service.py.
 */
import apiClient from './apiClient.js';

export const pipelineFileApi = {
  /** nodeId required; pipelineId omitted for a not-yet-saved pipeline. */
  upload: (file, nodeId, pipelineId, { onProgress } = {}) => {
    const form = new FormData();
    form.append('file', file);
    form.append('node_id', nodeId);
    if (pipelineId) form.append('pipeline_id', pipelineId);
    return apiClient
      .post('/pipeline-files/upload', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: onProgress
          ? (evt) => onProgress(evt.total ? Math.round((evt.loaded / evt.total) * 100) : 0)
          : undefined,
      })
      .then((r) => r.data);
  },

  /** Returns null (not an error) if this node has no upload yet. */
  getByNode: (nodeId) =>
    apiClient.get(`/pipeline-files/by-node/${nodeId}`).then((r) => r.data).catch((err) => {
      if (err?.status === 404) return null;
      throw err;
    }),

  /** Links an upload to the pipeline it belongs to — called right after a
   * brand-new pipeline's first Save assigns it a real id. */
  attach: (fileId, pipelineId) =>
    apiClient.post(`/pipeline-files/${fileId}/attach`, { pipeline_id: pipelineId }).then((r) => r.data),

  setSheet: (fileId, sheetName) =>
    apiClient.put(`/pipeline-files/${fileId}/sheet`, { sheet_name: sheetName }).then((r) => r.data),

  remove: (fileId) => apiClient.delete(`/pipeline-files/${fileId}`),
};
