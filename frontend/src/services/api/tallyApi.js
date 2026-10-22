/**
 * Tally Integration API service — wraps /api/tally/* (backend/app/api/tally.py).
 *
 * Phase A: company discovery against an already-saved TALLY_PRIME Connection.
 * Creating/editing/testing the connection itself goes through the existing
 * generic connectionApi — nothing Tally-specific there.
 *
 * TALLY-D (added TALLY-G): accounting mapping (ledger-name overrides,
 * versioned — see tally_mapping_service.py) + voucher preview/validation.
 * Every preview call only BUILDS and VALIDATES what an invoice would become
 * in Tally — nothing here ever writes to Tally (no post_voucher() exists
 * anywhere yet; see tally_voucher_builder.py's own docstring).
 *
 * TALLY-H: submitForApproval() seeks write-back authorization for a
 * READY preview via the generic Approval Workflow engine (entity_type=
 * TALLY_VOUCHER — see tally_approval_service.py) — still never writes to
 * Tally itself.
 */
import apiClient from './apiClient.js';

const tallyApi = {
  /** Real TDL Collection export against the live Tally gateway — never cached. */
  discoverCompanies: (connectionId) =>
    apiClient.post(`/tally/connections/${connectionId}/discover-companies`).then((r) => r.data),

  /** Current published mapping version — auto-seeds real defaults server-side if none exists yet. */
  getMapping: (connectionId) =>
    apiClient.get(`/tally/connections/${connectionId}/mapping`).then((r) => r.data),

  /** Publishes a brand-new version — never mutates a published row in place. */
  publishMapping: (connectionId, rows) =>
    apiClient.put(`/tally/connections/${connectionId}/mapping`, { rows }).then((r) => r.data),

  /** Builds (or rebuilds) the voucher + XML + deterministic validation for one invoice. */
  buildVoucherPreview: (connectionId, { invoiceId, company }) =>
    apiClient.post(`/tally/connections/${connectionId}/vouchers/preview`, {
      invoice_id: invoiceId, company,
    }).then((r) => r.data),

  listVoucherPreviews: (connectionId, params = {}) =>
    apiClient.get(`/tally/connections/${connectionId}/vouchers/previews`, { params }).then((r) => r.data),

  getVoucherPreview: (previewId) =>
    apiClient.get(`/tally/vouchers/previews/${previewId}`).then((r) => r.data),

  /** TALLY-H — the write-back approval gate's only entry point; only a READY preview may be submitted. */
  submitVoucherForApproval: (previewId) =>
    apiClient.post(`/tally/vouchers/previews/${previewId}/submit-for-approval`).then((r) => r.data),

  /** TALLY-I — the only call in this app that actually sends anything to Tally. Requires approval_status===APPROVED. */
  writeBackVoucher: (previewId) =>
    apiClient.post(`/tally/vouchers/previews/${previewId}/write-back`).then((r) => r.data),

  listWriteBackTransactions: (previewId) =>
    apiClient.get(`/tally/vouchers/previews/${previewId}/write-back/transactions`).then((r) => r.data),

  listWriteBackVerifications: (previewId) =>
    apiClient.get(`/tally/vouchers/previews/${previewId}/write-back/verifications`).then((r) => r.data),

  reverifyWriteBack: (previewId) =>
    apiClient.post(`/tally/vouchers/previews/${previewId}/write-back/verify`).then((r) => r.data),

  // ── Extraction (TALLY-B) ──
  startExtraction: (connectionId, body) =>
    apiClient.post(`/tally/connections/${connectionId}/extractions`, body).then((r) => r.data),

  listExtractions: (connectionId, params = {}) =>
    apiClient.get(`/tally/connections/${connectionId}/extractions`, { params }).then((r) => r.data),

  getExtraction: (runId) => apiClient.get(`/tally/extractions/${runId}`).then((r) => r.data),

  listExtractionRecords: (runId, params = {}) =>
    apiClient.get(`/tally/extractions/${runId}/records`, { params }).then((r) => r.data),

  // ── Canonical accounting model (TALLY-C) ──
  listCanonicalVouchers: (connectionId, params = {}) =>
    apiClient.get(`/tally/connections/${connectionId}/canonical/vouchers`, { params }).then((r) => r.data),

  listCanonicalParties: (connectionId, params = {}) =>
    apiClient.get(`/tally/connections/${connectionId}/canonical/parties`, { params }).then((r) => r.data),

  listCanonicalLedgers: (connectionId, params = {}) =>
    apiClient.get(`/tally/connections/${connectionId}/canonical/ledgers`, { params }).then((r) => r.data),

  // ── Reconciliation (TALLY-E) — manual, on-demand trigger. TALLY-J's
  // scheduled variant runs entirely server-side via the generic Schedule
  // engine (see scheduleApi.js's tally_config); these routes are what a
  // "Run Now" button and the Reconciliation results screen use. ──
  runReconciliation: (connectionId, { company, tolerance } = {}) =>
    apiClient.post(`/tally/connections/${connectionId}/reconciliation/runs`, { company, tolerance }).then((r) => r.data),

  listReconciliationRuns: (connectionId, params = {}) =>
    apiClient.get(`/tally/connections/${connectionId}/reconciliation/runs`, { params }).then((r) => r.data),

  getReconciliationRun: (runId) => apiClient.get(`/tally/reconciliation/runs/${runId}`).then((r) => r.data),

  listReconciliationExceptions: (runId, params = {}) =>
    apiClient.get(`/tally/reconciliation/runs/${runId}/exceptions`, { params }).then((r) => r.data),

  // ── Vendor -> Company routing (Invoice Auto-Sync) — drives which Tally
  // connection/company a STRAIGHT_THROUGH invoice automatically syncs to.
  // See tally_invoice_sync_service.py; gated by Settings' "Enable Tally
  // Auto-Sync" flag AND at least one of these rows existing. ──
  listVendorMappings: () => apiClient.get('/tally/vendor-mappings').then((r) => r.data),

  setVendorMapping: ({ vendorName, connectionId, companyName }) =>
    apiClient.put('/tally/vendor-mappings', {
      vendor_name: vendorName || null, connection_id: connectionId, company_name: companyName,
    }).then((r) => r.data),

  clearVendorMapping: (mappingId) =>
    apiClient.delete(`/tally/vendor-mappings/${mappingId}`).then((r) => r.data),
};

export default tallyApi;
