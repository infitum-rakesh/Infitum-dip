/**
 * Document API service — wraps all /api/documents/* endpoints.
 * Mock implementations simulate processing with realistic latency.
 * Replace the mock bodies with real apiClient calls as the backend is built.
 */
import apiClient from './apiClient.js';

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// ── Helpers ────────────────────────────────────────────────────

function randomBetween(min, max) {
  return Math.round(Math.random() * (max - min) + min);
}

// ── Mock catalogue for a demo invoice document ────────────────

const MOCK_DOCUMENT = {
  id: 'doc_demo_001',
  filename: null, // set at upload time
  type: null,
  size: null,
  pageCount: 1,
  previewUrl: null, // set from uploaded file ObjectURL
  status: 'uploaded',
  uploadedAt: new Date().toISOString(),
};

const MOCK_SCAN = {
  quality: {
    resolution: '300 DPI',
    blurScore: 96,
    skewAngle: '0.4°',
    contrast: 87,
    overallScore: 94,
  },
  classification: {
    type: 'Invoice',
    confidence: 98.4,
    language: 'English',
    pageCount: 1,
  },
  enhancedPreviewUrl: null, // same as original in mock
};

// ── Service ────────────────────────────────────────────────────

export const documentApi = {
  /**
   * Upload a file.  Returns a document object with preview URL.
   * Real implementation: POST /api/documents (multipart/form-data)
   */
  async upload(file) {
    await delay(800);
    const previewUrl = URL.createObjectURL(file);
    return {
      ...MOCK_DOCUMENT,
      id: `doc_${Date.now()}`,
      filename: file.name,
      type: file.type,
      size: file.size,
      previewUrl,
    };
  },

  /**
   * Scan / enhance document.
   * Real implementation: POST /api/documents/{id}/scan
   */
  async scan(documentId, _options = {}) {
    await delay(1400);
    return {
      documentId,
      ...MOCK_SCAN,
      scannedAt: new Date().toISOString(),
    };
  },

  /**
   * List documents.
   * Real implementation: GET /api/documents?page=&page_size=
   * NOTE: unused by any page today (kept for backward compatibility with
   * this mock module). The real Documents repository list lives below at
   * `listDocuments`, backed by GET /api/documents.
   */
  async list({ page = 1, pageSize = 20 } = {}) {
    await delay(400);
    return { items: [], total: 0, page, pageSize };
  },

  // ── Documents repository (real backend, Documents module) ─────
  // Everything below wraps a real /api/documents/* endpoint — no mocking.
  // These call through Documents' processing seam (DocumentProcessingService)
  // rather than duplicating Image Scanner / OCR / AI Extraction logic.

  /** Header KPIs: total, processing, completed, needs review, failed, storage used */
  getStats: () => apiClient.get('/documents/stats').then((r) => r.data),

  /** Distinct uploaders + pipelines for filter dropdowns */
  getFacets: () => apiClient.get('/documents/facets').then((r) => r.data),

  /**
   * Paginated, searchable, filterable document list.
   * params: { q, doc_type, status, date_preset, date_from, date_to,
   *           owner, owner_user_id, page, page_size, sort_by, sort_dir }
   */
  listDocuments: (params = {}) =>
    apiClient.get('/documents', { params: cleanParams(params) }).then((r) => r.data),

  getDocument: (id) => apiClient.get(`/documents/${id}`).then((r) => r.data),

  /** Upload a document to the repository (multipart/form-data). */
  uploadDocument: (file, { onProgress } = {}) => {
    const form = new FormData();
    form.append('file', file);
    return apiClient
      .post('/documents/upload', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: onProgress
          ? (evt) => onProgress(evt.total ? Math.round((evt.loaded / evt.total) * 100) : 0)
          : undefined,
      })
      .then((r) => r.data);
  },

  /**
   * Uploads N files in ONE request and auto-starts real processing (OCR +
   * AI Extraction + Structured Dataset) for every one of them server-side
   * — no separate "Process" click needed. Returns
   * `{ items: [{filename, ok, document?, error?}, ...] }`; a per-file
   * failure (bad extension, empty file, ...) doesn't fail the whole
   * batch. Each successfully-queued document.status starts at SCANNING —
   * poll each one with pollUntilDone() to track/await completion.
   */
  uploadDocumentsBulk: (files, { onProgress } = {}) => {
    const form = new FormData();
    files.forEach((f) => form.append('files', f));
    return apiClient
      .post('/documents/upload-bulk', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 5 * 60 * 1000, // N files' bytes can take a while to upload on a slow link; the processing itself is backgrounded server-side and doesn't add to this
        onUploadProgress: onProgress
          ? (evt) => onProgress(evt.total ? Math.round((evt.loaded / evt.total) * 100) : 0)
          : undefined,
      })
      .then((r) => r.data);
  },

  updateDocument: (id, body) => apiClient.patch(`/documents/${id}`, body).then((r) => r.data),

  deleteDocument: (id) => apiClient.delete(`/documents/${id}`),

  /** Starts real OCR + AI Extraction as a backend background task and
   * returns immediately (document status flips to SCANNING) — does NOT
   * wait for the pipeline to finish. Follow with pollUntilDone() below. */
  processDocument: (id) => apiClient.post(`/documents/${id}/process`).then((r) => r.data),

  reprocessDocument: (id) => apiClient.post(`/documents/${id}/reprocess`).then((r) => r.data),

  /**
   * Polls GET /documents/{id} until status is a terminal value
   * (COMPLETED | NEEDS_REVIEW | FAILED). Real OCR (Tesseract) plus up to
   * two live LLM calls (AI Extraction's schema-detect + extract, each
   * with its own real 60-120s provider timeout — see backend/app/
   * connectors/ai/*) can genuinely take well over a minute; running that
   * inside one HTTP request used to blow past apiClient's 30s timeout and
   * surface as "No response from server. Check your connection." even on
   * a fully successful run. Polling a fast, cheap GET instead sidesteps
   * that entirely — see backend document_service.py's "Background
   * execution" section for the server side of this fix.
   */
  async pollUntilDone(id, { intervalMs = 1500, timeoutMs = 5 * 60 * 1000, onTick } = {}) {
    const PROCESSING = new Set(['SCANNING', 'OCR_PROCESSING', 'AI_EXTRACTION']);
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const doc = await apiClient.get(`/documents/${id}`).then((r) => r.data);
      onTick?.(doc);
      if (!PROCESSING.has(doc.status)) return doc;
      await delay(intervalMs);
    }
    throw new Error('Processing is taking longer than expected — check back in a moment and re-open this document.');
  },

  sendToReview: (id, { reviewerId, note } = {}) =>
    apiClient.post(`/documents/${id}/send-to-review`, { reviewer_id: reviewerId ?? null, note: note ?? null }).then((r) => r.data),

  /** Withdraws an open review request — only valid while status is NEEDS_REVIEW. */
  cancelReview: (id) => apiClient.post(`/documents/${id}/cancel-review`).then((r) => r.data),

  /**
   * Real bug found live (user-reported): a NEEDS_REVIEW document had a
   * "Cancel Review" button but nothing to actually reach the Review
   * Queue item — this backs the new "Review" button, returning
   * { review_id } (null if the review was already resolved/cancelled
   * elsewhere since the page loaded).
   */
  getReviewItem: (id) => apiClient.get(`/documents/${id}/review-item`).then((r) => r.data),

  /** Canonical document types available for the Run AI Extraction picker (e.g. ["INVOICE"]). */
  getDocumentTypes: () => apiClient.get('/documents/document-types').then((r) => r.data.document_types),

  /**
   * Explicit, user-triggered (re-)run of AI Extraction — the action
   * missing from the review flow. documentType is optional (e.g.
   * "INVOICE"): when set, extraction is seeded with a comprehensive
   * canonical field schema instead of an ad-hoc LLM guess. connectionId
   * is optional: picks a specific AI Provider connection instead of the
   * automatic "try every configured connection in order" behavior — lets
   * a user route around one that's genuinely broken (e.g. out of API
   * credit). Can take a while (multiple real LLM calls for a large
   * schema + gap-fill pass), so this uses a longer timeout than the
   * default.
   */
  runExtraction: (id, documentType, connectionId) =>
    apiClient.post(`/documents/${id}/run-extraction`, {
      document_type: documentType || null,
      connection_id: connectionId ? Number(connectionId) : null,
    }, { timeout: 5 * 60 * 1000 }).then((r) => r.data),

  getOcr: (id) => apiClient.get(`/documents/${id}/ocr`).then((r) => r.data),

  /**
   * Saves user-authored structured data — the fallback for when AI
   * Extraction found nothing (no AI connection configured, or the model
   * came back empty) but OCR text is available for a human to read and
   * transcribe by hand. Stored in the identical shape AI Extraction
   * produces, so it shows up on the Extraction/Dataset tabs exactly like
   * an AI-derived result would.
   * fields: [{name, value}, ...]
   */
  saveManualExtraction: (id, fields) =>
    apiClient.post(`/documents/${id}/manual-extraction`, { fields, tables: [] }).then((r) => r.data),

  getExtraction: (id) => apiClient.get(`/documents/${id}/extraction`).then((r) => r.data),

  getDataset: (id) => apiClient.get(`/documents/${id}/dataset`).then((r) => r.data),

  /** Fetch the raw file as a Blob — used by the in-page viewer (auth header required). */
  getFileBlob: (id) =>
    apiClient.get(`/documents/${id}/file`, { responseType: 'blob' }).then((r) => r.data),

  /** Fetch + trigger a browser download of the original file. */
  async downloadDocument(id, filename) {
    const blob = await apiClient.get(`/documents/${id}/download`, { responseType: 'blob' }).then((r) => r.data);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || `document_${id}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  /** Bulk action across selected documents: process | send_to_review | delete */
  bulkAction: (documentIds, action, { reviewerId, note } = {}) =>
    apiClient
      .post('/documents/bulk', { document_ids: documentIds, action, reviewer_id: reviewerId ?? null, note: note ?? null })
      .then((r) => r.data),

  /**
   * Multilingual Document Processing, Phase 11 — real Tesseract readiness
   * check for Administration > Document Intelligence > Languages: for
   * every language the org has opted into, whether its traineddata file
   * is actually installed on the server right now.
   */
  getOcrHealth: () => apiClient.get('/documents/ocr-health').then((r) => r.data),

  /**
   * Admin OCR sandbox — runs the real Tesseract pipeline against an
   * uploaded file without creating a Document row, so a language
   * configuration can be validated against a real sample before saving.
   * Scoped to the local Tesseract engine only (see backend docstring).
   */
  testOcr: (file, { supportedLanguages, primaryLanguage } = {}) => {
    const form = new FormData();
    form.append('file', file);
    if (supportedLanguages && supportedLanguages.length) form.append('supported_languages', supportedLanguages.join(','));
    if (primaryLanguage) form.append('primary_language', primaryLanguage);
    return apiClient
      .post('/documents/ocr-test', form, { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 60 * 1000 })
      .then((r) => r.data);
  },
};

/** Strips undefined/''/'All' values so they don't pollute the query string */
function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}
