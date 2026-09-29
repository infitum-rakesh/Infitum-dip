/**
 * Invoice Intelligence API service — wraps /api/invoices/*. A thin,
 * invoice-shaped view over the real Document Intelligence pipeline
 * (upload/OCR/AI-extraction/reprocess all flow through the same
 * app.services.document_service backend that documentApi.js talks to)
 * plus the genuinely new Invoice Intelligence layer: canonical
 * materialization, validation, the decision engine, ERP posting,
 * reconciliation, and learning/evaluation (INV5-INV13). Nothing here is
 * mocked — every call hits a real, live-verified backend route.
 */
import apiClient from './apiClient.js';

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

function cleanParams(params = {}) {
  return Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== 'All')
  );
}

/** Invoice status values — the REAL ApInvoiceStatus enum
 * (backend/app/models/invoice_intelligence/core.py), not a guessed
 * subset. Found live: this used to list ['NEEDS_REVIEW', 'COMPLETED',
 * 'REJECTED', 'FAILED'] — values that don't exist on the model at all
 * (except REJECTED) — so 3 of 4 status-filter options 400'd
 * ("Unknown status '…'") and the STATUS column's real values (e.g.
 * EXCEPTION, READY_TO_POST) had no color mapping. Kept here, with a
 * shared variant/label map, so every page in this module (Inbox, Detail,
 * Analytics) reads off one correct source instead of re-declaring its
 * own (now-corrected) copy. */
export const INVOICE_STATUSES = [
  'RECEIVED', 'PROCESSING', 'NORMALIZED', 'VALIDATING', 'REVIEW_REQUIRED',
  'APPROVING', 'READY_TO_POST', 'POSTING', 'POSTED', 'RECONCILED', 'REJECTED', 'EXCEPTION',
];
export const INVOICE_STATUS_LABEL = {
  RECEIVED: 'Received', PROCESSING: 'Processing', NORMALIZED: 'Normalized', VALIDATING: 'Validating',
  REVIEW_REQUIRED: 'Review Required', APPROVING: 'Approving', READY_TO_POST: 'Ready to Post',
  POSTING: 'Posting', POSTED: 'Posted', RECONCILED: 'Reconciled', REJECTED: 'Rejected', EXCEPTION: 'Exception',
};
export const INVOICE_STATUS_VARIANT = {
  RECEIVED: 'gray', PROCESSING: 'info', NORMALIZED: 'info', VALIDATING: 'info',
  REVIEW_REQUIRED: 'warning', APPROVING: 'warning', READY_TO_POST: 'info', POSTING: 'info',
  POSTED: 'success', RECONCILED: 'success', REJECTED: 'danger', EXCEPTION: 'danger',
  // Document-pipeline stages a document passes through BEFORE an
  // ApInvoice row exists at all (Document.status, not ApInvoice.status —
  // see documentApi.pollUntilDone()'s PROCESSING set). The Invoice Inbox
  // shows these as synthetic "in flight" rows while OCR/AI Extraction run
  // in the background; sharing this one map keeps their color consistent
  // with the real post-materialization states above.
  SCANNING: 'info', OCR_PROCESSING: 'info', AI_EXTRACTION: 'info', UPLOADED: 'gray',
  NEEDS_REVIEW: 'warning', COMPLETED: 'success', FAILED: 'danger',
};
export const INVOICE_DECISIONS = ['STRAIGHT_THROUGH', 'REVIEW_REQUIRED', 'REJECT', 'RETRY'];
export const ERP_STATUSES = ['NOT_SUBMITTED', 'PENDING', 'RETRYING', 'SUCCEEDED', 'FAILED', 'DEAD_LETTER', 'CANCELLED'];
export const RECONCILIATION_STATUSES = ['NOT_STARTED', 'RECONCILED', 'MISMATCH', 'FAILED'];

export const invoiceApi = {
  // ── KPIs / list ──────────────────────────────────────────────
  getStats: () => apiClient.get('/invoices/stats').then((r) => r.data),

  /** params: { q, status, decision, vendor, date_from, date_to, page,
   * page_size, sort_by, sort_dir } — see api/invoices.py's list_invoices(). */
  list: (params = {}) => apiClient.get('/invoices', { params: cleanParams(params) }).then((r) => r.data),

  getInvoice: (id) => apiClient.get(`/invoices/${id}`).then((r) => r.data),

  /** Poll target right after upload — the ApInvoice row only exists once
   * background OCR + AI Extraction + materialization finishes; 404 until then. */
  getInvoiceByDocument: (documentId) => apiClient.get(`/invoices/by-document/${documentId}`).then((r) => r.data),

  // ── Upload / processing ──────────────────────────────────────
  /** Uploads through the real Document Intelligence pipeline, pinned to
   * the INVOICE taxonomy from the first run. Returns {document_id,
   * filename, status, duplicate_of_id} immediately — background OCR/AI
   * Extraction/materialization follow; use pollForInvoice() below. */
  upload: (file, { onProgress, languageConfig } = {}) => {
    const form = new FormData();
    form.append('file', file);
    if (languageConfig) {
      form.append('language_mode', languageConfig.mode || 'auto');
      form.append('selected_languages', (languageConfig.selected_languages || []).join(','));
      form.append('primary_language', languageConfig.primary_language || 'auto');
    }
    return apiClient
      .post('/invoices/upload', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: onProgress
          ? (evt) => onProgress(evt.total ? Math.round((evt.loaded / evt.total) * 100) : 0)
          : undefined,
      })
      .then((r) => r.data);
  },

  /** Polls GET /by-document/{id} until the background pipeline has
   * actually materialized an ApInvoice row (or times out) — same
   * "poll a cheap GET instead of one long request" pattern
   * documentApi.pollUntilDone() uses, sized for OCR+AI Extraction.
   *
   * apiClient's response interceptor (see apiClient.js's normaliseError())
   * already flattens every rejected error to a plain {status, message,
   * data} object — never a raw axios error with `.response.status`. This
   * used to check `err?.response?.status`, which is always undefined on
   * that flattened shape, so it re-threw on the very FIRST 404 (i.e.
   * almost always, since materialization takes at least a few seconds)
   * instead of ever retrying — found live via the Invoice Inbox upload
   * modal, surfacing "No invoice has been materialized for this document
   * yet." immediately after a perfectly successful upload. */
  async pollForInvoice(documentId, { intervalMs = 1500, timeoutMs = 5 * 60 * 1000, onTick } = {}) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const invoice = await apiClient.get(`/invoices/by-document/${documentId}`).then((r) => r.data);
        onTick?.(invoice);
        return invoice;
      } catch (err) {
        if (err?.status !== 404) throw err;
        onTick?.(null);
        await delay(intervalMs);
      }
    }
    throw new Error('Processing is taking longer than expected — check back in a moment and re-open this invoice.');
  },

  reprocess: (id) => apiClient.post(`/invoices/${id}/reprocess`).then((r) => r.data),

  /** Cheap synchronous re-run of materialize -> validate -> decide from
   * the document's already-extracted data (no fresh OCR/AI call). */
  revalidate: (id) => apiClient.post(`/invoices/${id}/revalidate`).then((r) => r.data),

  // ── Detail sub-resources ─────────────────────────────────────
  getExtraction: (id) => apiClient.get(`/invoices/${id}/extraction`).then((r) => r.data),

  getValidation: (id) => apiClient.get(`/invoices/${id}/validation`).then((r) => r.data),

  getEvidence: (id) => apiClient.get(`/invoices/${id}/evidence`).then((r) => r.data),

  /** The narrow ApInvoiceAuditEvent trail (DECISION_MADE, ERP_SUBMITTED,
   * ERP_SUCCEEDED, RECONCILIATION_RUN, DUPLICATE_DETECTED, etc.) —
   * distinct from getAudit() below, which is field-correction history. */
  getLifecycleEvents: (id) => apiClient.get(`/invoices/${id}/lifecycle-events`).then((r) => r.data),

  getAudit: (id) => apiClient.get(`/invoices/${id}/audit`).then((r) => r.data),

  getHistory: (id) => apiClient.get(`/invoices/${id}/history`).then((r) => r.data),

  // ── Corrections ──────────────────────────────────────────────
  /** body: [{ field_name, corrected_value, reason }] */
  applyCorrections: (id, corrections) => apiClient.post(`/invoices/${id}/corrections`, { corrections }).then((r) => r.data),

  // ── ERP posting (INV9/INV18) ─────────────────────────────────
  /** Real dry-run/simulation mode — builds the exact payload the current
   * ERP mapping would produce for this invoice, without any network call
   * or DB write. Lets an admin verify a mapping before actually posting. */
  previewErpPost: (id, erpConnectionId) => apiClient.get(`/invoices/${id}/erp/preview`, { params: { erp_connection_id: erpConnectionId } }).then((r) => r.data),

  postToErp: (id, erpConnectionId) => apiClient.post(`/invoices/${id}/erp/post`, { erp_connection_id: erpConnectionId }).then((r) => r.data),

  listErpTransactions: (id) => apiClient.get(`/invoices/${id}/erp/transactions`).then((r) => r.data),

  getErpStatus: (id) => apiClient.get(`/invoices/${id}/erp/status`).then((r) => r.data),

  cancelErpPosting: (id) => apiClient.post(`/invoices/${id}/erp/cancel`).then((r) => r.data),

  // ── Reconciliation (INV10) ───────────────────────────────────
  listReconciliations: (id) => apiClient.get(`/invoices/${id}/reconciliation`).then((r) => r.data),

  runReconciliation: (id) => apiClient.post(`/invoices/${id}/reconciliation`).then((r) => r.data),

  // ── Learning / evaluation (INV13) ────────────────────────────
  getLearningMetrics: () => apiClient.get('/invoices/learning/metrics').then((r) => r.data),

  getRecurringCorrectionsByField: (limit = 10) =>
    apiClient.get('/invoices/learning/recurring-corrections/by-field', { params: { limit } }).then((r) => r.data),

  getRecurringCorrectionsByVendor: (limit = 10) =>
    apiClient.get('/invoices/learning/recurring-corrections/by-vendor', { params: { limit } }).then((r) => r.data),

  /** params: { field_name, vendor_id, approved_only, page, page_size } */
  listLearningRecords: (params = {}) => apiClient.get('/invoices/learning/records', { params: cleanParams(params) }).then((r) => r.data),

  approveLearningRecord: (recordId) => apiClient.post(`/invoices/learning/records/${recordId}/approve`).then((r) => r.data),

  // ── Validation rules (INV17) ─────────────────────────────────
  /** Also seeds the real default rule set on first access (idempotent) —
   * see api/invoice_rules.py's own docstring. */
  listRules: () => apiClient.get('/invoice-rules').then((r) => r.data),

  /** body: { name, category, severity, config, message, enabled }.
   * Publishes a new version of the named rule (never mutates one in place). */
  publishRule: (body) => apiClient.put('/invoice-rules', body).then((r) => r.data),

  toggleRule: (ruleId, enabled) => apiClient.post(`/invoice-rules/${ruleId}/toggle`, { enabled }).then((r) => r.data),

  // ── ERP field mapping (INV9/INV17) ───────────────────────────
  /** Seeds the real Oracle-field default mapping on first access for this
   * ERP connection (idempotent). */
  getMapping: (erpConnectionId) => apiClient.get('/invoice-mappings', { params: { erp_connection_id: erpConnectionId } }).then((r) => r.data),

  /** body: { erp_connection_id, rows: [{canonical_field, erp_field, is_line_mapping, default_value, transform_config}] }.
   * Publishes a brand-new version of the WHOLE mapping (full replace, not a per-row patch). */
  publishMapping: (body) => apiClient.put('/invoice-mappings', body).then((r) => r.data),
};
