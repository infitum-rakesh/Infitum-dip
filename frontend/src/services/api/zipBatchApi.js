/**
 * ZIP Batch Upload API service (ZB10/ZB11) — wraps the real
 * /api/documents/zip-upload/*, /api/documents/zip-batches/* routes (and
 * their byte-for-byte mirror under /api/invoices/*, fixed to the
 * INVOICE_INTELLIGENCE module — see app/api/invoices.py). One ZIP upload =
 * one chunked upload session (BatchUpload) + one batch run (DocumentBatch)
 * + many bounded-parallel per-file tasks (DocumentBatchFile) that each call
 * the EXISTING document_service.process_document() — no second pipeline.
 *
 * `createZipBatchApi(basePath)` returns a client scoped to one module
 * ('documents' or 'invoices') since RBAC (`require_action(..., module=...)`)
 * is baked in server-side per-router, not per-request — see documents.py's
 * own ZB10 docstring for why this couldn't be one shared router.
 */
import apiClient from './apiClient.js';

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

/** Real enums — mirror models/document_batch.py exactly. */
export const BATCH_UPLOAD_STATUSES = ['INITIATED', 'UPLOADING', 'ASSEMBLING', 'COMPLETED', 'FAILED', 'CANCELLED'];

export const BATCH_STATUSES = [
  'UPLOADING', 'UPLOADED', 'VALIDATING', 'EXTRACTING', 'REGISTERING', 'QUEUED',
  'PROCESSING', 'PAUSED', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELLED',
];
export const BATCH_STATUS_LABEL = {
  UPLOADING: 'Uploading', UPLOADED: 'Uploaded', VALIDATING: 'Validating', EXTRACTING: 'Extracting',
  REGISTERING: 'Registering', QUEUED: 'Queued', PROCESSING: 'Processing', PAUSED: 'Paused',
  COMPLETED: 'Completed', COMPLETED_WITH_ERRORS: 'Completed with Errors', FAILED: 'Failed', CANCELLED: 'Cancelled',
};
export const BATCH_STATUS_VARIANT = {
  UPLOADING: 'info', UPLOADED: 'gray', VALIDATING: 'info', EXTRACTING: 'info', REGISTERING: 'info',
  QUEUED: 'gray', PROCESSING: 'info', PAUSED: 'warning', COMPLETED: 'success',
  COMPLETED_WITH_ERRORS: 'warning', FAILED: 'danger', CANCELLED: 'gray',
};
export const BATCH_ACTIVE_STATUSES = ['UPLOADING', 'UPLOADED', 'VALIDATING', 'EXTRACTING', 'REGISTERING', 'QUEUED', 'PROCESSING', 'PAUSED'];
export const BATCH_TERMINAL_STATUSES = ['COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELLED'];

export const FILE_STATUSES = [
  'DISCOVERED', 'EXTRACTED', 'REGISTERED', 'QUEUED', 'PROCESSING', 'SCANNING', 'LANGUAGE_DETECTION',
  'OCR_PROCESSING', 'AI_EXTRACTION', 'VALIDATING', 'REVIEW_REQUIRED', 'COMPLETED', 'FAILED',
  'RETRY_PENDING', 'STALE', 'SKIPPED_DUPLICATE', 'UNSUPPORTED', 'NESTED_ARCHIVE_NOT_SUPPORTED', 'CANCELLED',
];
export const FILE_STATUS_VARIANT = {
  DISCOVERED: 'gray', EXTRACTED: 'gray', REGISTERED: 'gray', QUEUED: 'gray', PROCESSING: 'info',
  SCANNING: 'info', LANGUAGE_DETECTION: 'info', OCR_PROCESSING: 'info', AI_EXTRACTION: 'info',
  VALIDATING: 'info', REVIEW_REQUIRED: 'warning', COMPLETED: 'success', FAILED: 'danger',
  RETRY_PENDING: 'warning', STALE: 'warning', SKIPPED_DUPLICATE: 'gray', UNSUPPORTED: 'gray',
  NESTED_ARCHIVE_NOT_SUPPORTED: 'gray', CANCELLED: 'gray',
};
export const DUPLICATE_POLICIES = ['SKIP_DUPLICATE', 'PROCESS_DUPLICATE', 'CREATE_NEW_VERSION'];
export const DUPLICATE_POLICY_LABEL = {
  SKIP_DUPLICATE: 'Skip duplicates (default)',
  PROCESS_DUPLICATE: 'Process duplicates anyway',
  CREATE_NEW_VERSION: 'Create a new version',
};

/** basePath: 'documents' | 'invoices' */
export function createZipBatchApi(basePath) {
  return {
    // ── Chunked upload session ────────────────────────────────
    initUpload: (body) => apiClient.post(`/${basePath}/zip-upload/init`, body).then((r) => r.data),

    uploadChunk: (uploadId, chunkNumber, blob, { onProgress } = {}) => {
      const form = new FormData();
      form.append('chunk_number', String(chunkNumber));
      form.append('file', blob, `chunk_${chunkNumber}`);
      return apiClient
        .post(`/${basePath}/zip-upload/${uploadId}/chunk`, form, {
          headers: { 'Content-Type': 'multipart/form-data' },
          timeout: 5 * 60 * 1000,
          onUploadProgress: onProgress ? (evt) => onProgress(evt.total ? evt.loaded / evt.total : 0) : undefined,
        })
        .then((r) => r.data);
    },

    getUploadStatus: (uploadId) => apiClient.get(`/${basePath}/zip-upload/${uploadId}/status`).then((r) => r.data),

    /** body: { expected_sha256?, auto_process, duplicate_policy } -> { upload, is_duplicate_upload, duplicate_of_upload_id, batch } */
    completeUpload: (uploadId, body = {}) => apiClient.post(`/${basePath}/zip-upload/${uploadId}/complete`, body).then((r) => r.data),

    cancelUpload: (uploadId) => apiClient.post(`/${basePath}/zip-upload/${uploadId}/cancel`).then((r) => r.data),

    // ── Batches ───────────────────────────────────────────────
    /** params: { status, page, page_size } */
    listBatches: (params = {}) => apiClient.get(`/${basePath}/zip-batches`, { params: cleanParams(params) }).then((r) => r.data),

    getBatch: (batchId) => apiClient.get(`/${basePath}/zip-batches/${batchId}`).then((r) => r.data),

    startBatch: (batchId) => apiClient.post(`/${basePath}/zip-batches/${batchId}/start`).then((r) => r.data),
    pauseBatch: (batchId) => apiClient.post(`/${basePath}/zip-batches/${batchId}/pause`).then((r) => r.data),
    resumeBatch: (batchId) => apiClient.post(`/${basePath}/zip-batches/${batchId}/resume`).then((r) => r.data),
    cancelBatch: (batchId) => apiClient.post(`/${basePath}/zip-batches/${batchId}/cancel`).then((r) => r.data),
    retryFailed: (batchId) => apiClient.post(`/${basePath}/zip-batches/${batchId}/retry-failed`).then((r) => r.data),
    retrySelected: (batchId, fileIds) => apiClient.post(`/${basePath}/zip-batches/${batchId}/retry-selected`, { file_ids: fileIds }).then((r) => r.data),

    /** params: { status, page, page_size } */
    listFiles: (batchId, params = {}) => apiClient.get(`/${basePath}/zip-batches/${batchId}/files`, { params: cleanParams(params) }).then((r) => r.data),

    /** since_id log-drawer paging — mirrors JobLogDrawer.jsx / bulkMigrationApi.js's convention. */
    listEvents: (batchId, sinceId = 0, limit = 200) =>
      apiClient.get(`/${basePath}/zip-batches/${batchId}/events`, { params: { since_id: sinceId, limit } }).then((r) => r.data),

    getMetrics: (batchId) => apiClient.get(`/${basePath}/zip-batches/${batchId}/metrics`).then((r) => r.data),

    /** params: { page, page_size } */
    listErrors: (batchId, params = {}) => apiClient.get(`/${basePath}/zip-batches/${batchId}/errors`, { params: cleanParams(params) }).then((r) => r.data),

    /** Triggers a browser download of the errors CSV. */
    async downloadErrors(batchId, filenameFallback = `batch-${batchId}-errors.csv`) {
      const res = await apiClient.get(`/${basePath}/zip-batches/${batchId}/errors/download`, { responseType: 'blob' });
      const filename = /filename="?([^"]+)"?/.exec(res.headers['content-disposition'] || '')?.[1] || filenameFallback;
      const url = URL.createObjectURL(res.data);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
  };
}

export const documentZipBatchApi = createZipBatchApi('documents');
export const invoiceZipBatchApi = createZipBatchApi('invoices');
